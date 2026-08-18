"""rec#26: data-quality row-volume monitor — robust-z on the most recent load,
scoped to registered products. Locks the pure scorer (logic/dq.py), the builder
shape, and the Operations panel wiring.
"""

from pathlib import Path

import pandas as pd

from app.data import dq_sql
from app.logic.dq import (
    ANOMALY_COLUMNS,
    row_volume_anomalies,
    summarize_row_volume,
)

_ROOT = Path(__file__).resolve().parents[2]
_OPS = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")


def _dense(fqn, base, latest, n=28, product="P", owner="O", crit="STANDARD"):
    """A flat baseline (mad==0) plus a chosen latest load."""
    days = pd.date_range("2024-01-01", periods=n, freq="D")
    vals = [base] * (n - 1) + [latest]
    return pd.DataFrame({"FQN": fqn, "DAY": days, "ROWS_ADDED": vals,
                         "DATA_PRODUCT": product, "OWNER_NAME": owner, "CRITICALITY": crit})


def _noisy(fqn, latest, n=28):
    """A varied baseline (900/1000/1100 -> median 1000, mad>0) plus a latest load."""
    days = pd.date_range("2024-01-01", periods=n, freq="D")
    vals = [*([900, 1000, 1100] * n)[: n - 1], latest]
    return pd.DataFrame({"FQN": fqn, "DAY": days, "ROWS_ADDED": vals,
                         "DATA_PRODUCT": "P", "OWNER_NAME": "O", "CRITICALITY": "STANDARD"})


# --- row_volume_anomalies --------------------------------------------------

def test_empty_or_none_returns_empty_schema():
    for frame in (None, pd.DataFrame()):
        out = row_volume_anomalies(frame)
        assert out.empty
        assert list(out.columns) == list(ANOMALY_COLUMNS)


def test_steady_normal_is_not_flagged():
    out = row_volume_anomalies(_noisy("T", latest=1000))
    assert len(out) == 1
    assert out.loc[0, "DIRECTION"] == "NORMAL"
    assert not bool(out.loc[0, "FLAGGED"])
    assert out.loc[0, "LATEST_DAY"] == "2024-01-28"


def test_spike_and_partial_load_flag_on_robust_z():
    spike = row_volume_anomalies(_noisy("T", latest=5000))
    assert spike.loc[0, "DIRECTION"] == "SPIKE" and bool(spike.loc[0, "FLAGGED"])
    assert spike.loc[0, "ROBUST_Z"] > 0
    partial = row_volume_anomalies(_noisy("T", latest=50))     # a thin/partial load
    assert partial.loc[0, "DIRECTION"] == "DROP" and bool(partial.loc[0, "FLAGGED"])
    assert partial.loc[0, "ROBUST_Z"] < 0


def test_flat_baseline_uses_scale_floor_not_infinite_z():
    # mad==0 baseline: dispersion is floored at 15% of the median, so a big change
    # scores a finite z (not the old +/-99.9 blow-up) and a steady load is NORMAL.
    drop = row_volume_anomalies(_dense("T", base=1000, latest=1))
    assert drop.loc[0, "DIRECTION"] == "DROP"
    assert drop.loc[0, "ROBUST_Z"] == round((1 - 1000) / (0.15 * 1000), 1)   # ~-6.7, finite
    steady = row_volume_anomalies(_dense("T", base=1000, latest=1000))
    assert steady.loc[0, "DIRECTION"] == "NORMAL" and steady.loc[0, "ROBUST_Z"] == 0.0


def test_majority_modal_baseline_does_not_false_alarm_on_jitter():
    # THE skeptic's P1: MAD collapses to 0 when >50% of loads share the modal value,
    # even with legit backfills in range. A within-range jitter must NOT flag.
    days = pd.date_range("2024-01-01", periods=23, freq="D")
    vals = [1000] * 20 + [3000, 2500, 4000]   # median 1000, MAD raw 0, but range 1000-4000
    frame = pd.DataFrame({"FQN": "T", "DAY": days, "ROWS_ADDED": vals,
                          "DATA_PRODUCT": "P", "OWNER_NAME": "O", "CRITICALITY": "C"})
    # latest 1050 is normal jitter -> with the 15% floor, z=(1050-1000)/150=0.33 -> NORMAL
    frame.loc[frame.index[-1], "ROWS_ADDED"] = 1050
    out = row_volume_anomalies(frame)
    assert out.loc[0, "DIRECTION"] == "NORMAL" and not bool(out.loc[0, "FLAGGED"])


def test_extreme_ratio_is_still_capped_for_display():
    out = row_volume_anomalies(_dense("T", base=1000, latest=1_000_000))
    assert out.loc[0, "ROBUST_Z"] == 99.9 and out.loc[0, "DIRECTION"] == "SPIKE"


def test_stale_latest_load_is_marked():
    healthy = _dense("HEALTHY", base=1000, latest=1000, n=28)     # loads through 2024-01-28
    stale = _dense("STALE", base=1000, latest=1000, n=24)         # last load 2024-01-24
    out = row_volume_anomalies(pd.concat([healthy, stale], ignore_index=True)).set_index("FQN")
    assert out.loc["HEALTHY", "DAYS_STALE"] == 0
    assert out.loc["STALE", "DAYS_STALE"] == 4                    # 28 - 24
    assert summarize_row_volume(out.reset_index())["stale"] == 1


def test_business_day_table_is_not_falsely_flagged():
    # Only load days count, so a weekday-only table scored against its own loads is
    # NORMAL — it can't be dragged to a false DROP by a weekend zero (the bug this
    # design avoids). ~20 business days of steady loads.
    days = pd.bdate_range("2024-01-01", periods=20)
    frame = pd.DataFrame({"FQN": "WD", "DAY": days, "ROWS_ADDED": [1000] * 20,
                          "DATA_PRODUCT": "P", "OWNER_NAME": "O", "CRITICALITY": "C"})
    out = row_volume_anomalies(frame)
    assert len(out) == 1 and out.loc[0, "DIRECTION"] == "NORMAL"


def test_zero_row_days_are_not_loads():
    # a day present in TABLE_DML_HISTORY with 0 rows added (update-only) is not a load
    # and never becomes the scored "latest" — that's the freshness panel's concern.
    days = pd.date_range("2024-01-01", periods=28, freq="D")
    vals = [1000] * 27 + [0]        # newest day added no rows
    frame = pd.DataFrame({"FQN": "T", "DAY": days, "ROWS_ADDED": vals,
                          "DATA_PRODUCT": "P", "OWNER_NAME": "O", "CRITICALITY": "C"})
    out = row_volume_anomalies(frame)
    # scored against the 27 real loads (all 1000); latest load is day 27, NORMAL
    assert out.loc[0, "DIRECTION"] == "NORMAL"
    assert out.loc[0, "LATEST_DAY"] == "2024-01-27"


def test_too_few_loads_is_skipped_not_called_healthy():
    days = pd.date_range("2024-01-01", periods=28, freq="D")
    vals = [1000] * 6 + [0] * 22     # only 6 loads -> below min_baseline_loads + 1
    frame = pd.DataFrame({"FQN": "T", "DAY": days, "ROWS_ADDED": vals,
                          "DATA_PRODUCT": "P", "OWNER_NAME": "O", "CRITICALITY": "C"})
    assert row_volume_anomalies(frame).empty


def test_tiny_table_below_median_floor_is_skipped():
    assert row_volume_anomalies(_dense("T", base=50, latest=50)).empty       # median 50 < 100
    assert row_volume_anomalies(_dense("T", base=50, latest=9999)).empty     # still no basis


def test_sorted_by_absolute_z_and_metadata_carried():
    frame = pd.concat([_noisy("SMALL", latest=1500), _noisy("BIG", latest=9000)],
                      ignore_index=True)
    out = row_volume_anomalies(frame)
    assert list(out["FQN"]) == ["BIG", "SMALL"]     # larger |z| first
    assert out.iloc[0]["OWNER_NAME"] == "O" and out.iloc[0]["DATA_PRODUCT"] == "P"


def test_threshold_tunable():
    frame = _noisy("T", latest=1300)     # ~2 robust-z above
    assert row_volume_anomalies(frame, z_threshold=2.0).loc[0, "FLAGGED"]
    assert not row_volume_anomalies(frame, z_threshold=5.0).loc[0, "FLAGGED"]


# --- summarize_row_volume --------------------------------------------------

def test_summarize_counts():
    frame = pd.concat([
        _noisy("A", latest=9000),   # spike
        _noisy("B", latest=1),      # partial load -> drop
        _noisy("C", latest=1000),   # normal
    ], ignore_index=True)
    stats = summarize_row_volume(row_volume_anomalies(frame))
    assert stats["monitored"] == 3
    assert stats["flagged"] == 2
    assert stats["spikes"] == 1 and stats["drops"] == 1
    assert stats["products"] == 1   # all product "P"


def test_summarize_empty():
    assert summarize_row_volume(pd.DataFrame())["monitored"] == 0
    assert summarize_row_volume(None)["flagged"] == 0


# --- SQL builder -----------------------------------------------------------

def test_builder_scopes_to_catalog_and_excludes_today():
    sql = dq_sql.product_row_volume(28)
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY" in sql
    assert "ENTITY_CATALOG" in sql
    assert "ROWS_ADDED" in sql and "DATA_PRODUCT" in sql and "OWNER_NAME" in sql
    # registered products only, resolved object-then-database like the cost catalog
    assert "WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL" in sql
    # excludes today's partial day and the SNOWFLAKE db
    assert "d.START_TIME < CURRENT_DATE()" in sql
    assert "UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'" in sql


def test_builder_bounds_window():
    assert "DATEADD('day', -90," in dq_sql.product_row_volume(99999)
    assert "-99999" not in dq_sql.product_row_volume(99999)


# --- panel wiring ----------------------------------------------------------

def test_panel_wired_on_operations():
    assert "def _dq_row_volume_panel(" in _OPS
    assert "_dq_row_volume_panel()" in _OPS
    assert "row_volume_anomalies(" in _OPS
    assert "product_row_volume(" in _OPS
    assert "Row-volume anomalies" in _OPS
