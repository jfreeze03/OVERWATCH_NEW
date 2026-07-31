"""Bug round 6 regression locks (the 10 app-only findings).

Behavioural tests for the logic-layer fixes (anomaly recency, triage RAISED_AT, the
security window); source-locks for the UI-layer fixes that need a live Streamlit runtime.
The 5 migration-proc findings ship in V066 (see tests/test_v066_*.py)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #5 — stale spend anomalies must age out (carry the day; stamp RAISED_AT)
# ---------------------------------------------------------------------------
def test_anomaly_summary_carries_the_day():
    from app.logic.anomaly import anomaly_summary, flag_anomalies
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["W"] * 8,
        "DAY": pd.date_range("2026-07-01", periods=8, freq="D"),
        "USD": [100, 110, 95, 105, 98, 102, 100, 900],  # spike on the last day
    })
    flagged = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME")
    rows = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")
    assert rows and str(rows[0]["day"]).startswith("2026-07-08")   # the spike's own day


def test_anomaly_summary_day_none_without_day_column():
    from app.logic.anomaly import anomaly_summary, flag_anomalies
    df = pd.DataFrame({"W": ["A"] * 8, "USD": [10, 10, 11, 9, 10, 10, 10, 300]})
    rows = anomaly_summary(flag_anomalies(df, "USD", group_col="W"), "W", "USD")
    assert rows and rows[0]["day"] is None            # no DAY column -> None, not a crash


def test_triage_queue_anomaly_stamps_its_day():
    from app.logic.actions import triage_queue
    q = triage_queue(None, None, [{"label": "WH", "value": 900.0, "z": 6.0, "day": "2026-07-30"}])
    row = q[q["KIND"] == "Spend anomaly"].iloc[0]
    assert row["RAISED_AT"] == "2026-07-30"           # was hardcoded None -> dateless HIGH


def test_triage_queue_anomaly_without_day_stays_blank():
    # back-compat: an anomaly with no day still normalizes to arrow-safe empty text
    from app.logic.actions import triage_queue
    q = triage_queue(None, None, [{"label": "WH", "value": 1.0, "z": 6.0}])
    assert q.iloc[0]["RAISED_AT"] == ""


def test_control_room_filters_anomalies_to_latest_day():
    cr = _src("app/ui/pages/control_room.py")
    assert '_latest = str(_wh_complete["DAY"].max())' in cr
    assert 'str(a.get("day") or "") == _latest' in cr


# ---------------------------------------------------------------------------
# #13 — failed-login reasons must share the failed-logins 30d cap
# ---------------------------------------------------------------------------
def test_failed_login_reasons_capped_to_30d():
    from app.data import security_sql
    assert "-30, CURRENT_TIMESTAMP())" in security_sql.failed_login_reasons(90)
    assert "-30, CURRENT_TIMESTAMP())" in security_sql.failed_logins(90)   # already 30


# ---------------------------------------------------------------------------
# UI-layer source-locks (need a live Streamlit runtime to exercise behaviourally)
# ---------------------------------------------------------------------------
def test_bug4_health_read_distinguishes_error_from_empty():
    m = _src("app/main.py")
    assert "def _health_values() -> dict[str, tuple[str, str]] | None:" in m
    assert "if not res.ok:\n        return None" in m
    assert 'if vals is None:   # r6-bug4' in m           # callers render "unavailable"


def test_bug7_spend_trend_pace_excludes_partial_day():
    ch = _src("app/ui/charts.py")
    assert 'complete = data[~data["PROVISIONAL"]]' in ch
    assert "complete[\"USD\"].tail(7).mean()" in ch


def test_bug8_12_chargeback_pool_window_and_grain():
    cb = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert 'wh_usd = _pool_df.groupby("WAREHOUSE_NAME")["USD"].sum().to_dict()' in cb   # #12
    assert "days > MAX_LIVE_WINDOW_DAYS" in cb                                          # #8


def test_bug9_jump_to_db_carries_company():
    m = _src("app/main.py")
    assert '"company": _co, "database": name' in m


def test_bug10_operations_drill_acts_on_new_selection():
    ops = _src("app/ui/pages/operations.py")
    assert 'sel_q != st.session_state.get("_ops_top_sel_last")' in ops


def test_bug14_15_neutral_delta_color_on_cost_cards():
    assert '"delta_color": "off",' in _src("app/ui/pages/cost_parts/contract.py").split('"Consumed"', 1)[1][:500]
    assert '"delta_color": "off",' in _src("app/ui/pages/cost_parts/spend.py").split("Storage MTD", 1)[1][:500]
