"""Regression locks for the round-2 bug hunt (v4.417.0)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.data import cost_sql, mart27_sql, mart_sql
from app.logic import rca
from app.logic.date_windows import window_bounds

_ROOT = Path(__file__).resolve().parents[1]
_AUG = window_bounds("LAST_MONTH", date(2026, 9, 17))   # (2026-08-01, 2026-09-01)


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- MF1: alloc_xdim coverage gate honors the bounded window --------------------
def test_alloc_xdim_coverage_gate_uses_bounded_window():
    lm = mart27_sql.alloc_xdim_attribution(31, "USER", "ALL", bounds=_AUG)
    # the interior-day count AND the FIRST_DAY gate both use the calendar range
    assert "CASE WHEN DAY >= '2026-08-01' AND DAY < '2026-09-01' THEN DAY END" in lm
    assert "(SELECT FIRST_DAY FROM cov) <= '2026-08-01'" in lm
    assert "-31, CURRENT_DATE()" not in lm            # no trailing anchor leaks into the gate
    # trailing is byte-identical (session-anchored) when unbounded
    tr = mart27_sql.alloc_xdim_attribution(30, "USER", "ALL")
    assert "(SELECT FIRST_DAY FROM cov) <= DATEADD('day', -30, CURRENT_DATE())" in tr


# --- CSMC-2: savings quarter anchored on the account clock ----------------------
def test_savings_summary_quarter_anchors_on_account_time():
    sql = mart_sql.savings_summary_quarter()
    assert "DATE_TRUNC('quarter', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)" in sql
    assert "DATE_TRUNC('quarter', CURRENT_DATE())" not in sql   # session-tz anchor gone


# --- DTB-1: storage calendar month anchored on the account clock ----------------
def test_storage_calendar_uses_account_tz_month():
    for prior in (False, True):
        for sql in (cost_sql.storage_by_database_calendar("ALL", prior=prior),
                    cost_sql.storage_by_database_calendar_live("ALL", prior=prior)):
            assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())" in sql
            assert "CURRENT_DATE()" not in sql


# --- RCA-1: a day-grain spend anomaly is stamped end-of-day ---------------------
def test_rca_daily_anomaly_stamped_end_of_day_not_midnight():
    cands = rca.candidates_from_anomalies([{"label": "WH_X", "z": 5.0, "value": 1000, "day": "2026-08-15"}])
    assert len(cands) == 1
    w = cands[0]["when"]
    assert (w.hour, w.minute, w.second) == (23, 59, 59)
    assert w.date().isoformat() == "2026-08-15"
    # a same-day onset (14:00) now reads the spike as AFTER onset (symptom), not preceding
    from datetime import datetime

    from app.logic.rca import _proximity
    onset = datetime(2026, 8, 15, 14, 0, 0)
    assert _proximity(w, onset) <= 0.2   # _AFTER_ONSET_PROX — weak, LOW-capped


# --- capacity: FORECAST row's current index is the driver channel ---------------
def test_capacity_forecast_reports_driver_channel_current():
    src = _src("app/logic/capacity.py")
    fc = src.split('"STATUS": "FORECAST"', 1)[1].split("})", 1)[0]
    assert '"CURRENT_PRESSURE_INDEX": round(driver_current, 3)' in fc


# --- DFC-1 / DFC-2: higher-is-worse deltas colored by fixed polarity ------------
def test_compare_and_wh_change_deltas_use_fixed_polarity():
    cmp_src = _src("app/ui/pages/cost_parts/compare.py")
    assert '"inverse" if a_usd > b_usd else "normal"' not in cmp_src
    assert '"inverse" if a_rate > b_rate else "normal"' not in cmp_src
    assert '"inverse" if ab > bb else "normal"' not in cmp_src
    ops = _src("app/ui/pages/operations.py")
    assert 'else "normal" if d["direction"] == "better" else "off"' not in ops
    assert '"inverse" if d["direction"] in ("worse", "better")' in ops


# --- CSMC-1: Brief verdict uses the uncapped incident count ---------------------
def test_brief_verdict_uses_uncapped_incident_count():
    brief = _src("app/ui/pages/brief.py")
    assert "if _inc.ok and _n_inc > 0:" in brief
    assert 'f"{_n_inc} open incident(s)"' in brief
    assert "len(_inc.df)} open incident(s)" not in brief   # the capped form is gone


# --- SWI-1 / SWI-2: write CALLs use a STABLE content-signature request_key ------
def test_operator_writes_use_stable_idempotency_key():
    wb = _src("app/ui/workbench.py")
    assert 'idempotency_key(\n                "ui_action"' in wb
    assert "request_key=f\"ui:{action_id}:{uuid4()}\"" not in wb    # fresh-uuid form gone
    ds = _src("app/ui/decision_studio.py")
    assert 'idempotency_key(\n                "ui_experiment"' in ds
