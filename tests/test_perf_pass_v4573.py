"""Locks for the v4.573.0 performance pass (usage_sim-grounded): mart-first + read batching.

Four wins — Overview Last-month mart-first daily-spend, Operations discarded-activity gate,
Decision Studio scorecard batch, and Overview score_inputs folded into the score batch — pinned so
a regression re-fails here. Also asserts the profiler shows Overview 'Last month' at 0 AU scans.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_fact_warehouse_daily_honors_bounds():
    # bounded calendar window (Last month) — the day-grain mart can express [start, end)
    from datetime import date

    aug = (date(2026, 8, 1), date(2026, 9, 1))
    sql = mart_sql.fact_warehouse_daily(31, "ALFA", bounds=aug)
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in sql
    # trailing (bounds=None) predicate stays byte-identical to the pre-pass form
    assert "DAY >= DATEADD('day', -180, CURRENT_DATE())" in mart_sql.fact_warehouse_daily(180, "ALFA")


def test_overview_daily_spend_is_mart_first():
    ov = _read("app/ui/pages/overview.py")
    # _live_fallback_daily now reads FACT_WAREHOUSE_DAILY first, live scan as the fallback leg
    assert "run_mart_first(" in ov and "mart_sql.fact_warehouse_daily(days, company, bounds=bounds)" in ov
    assert "cost_sql.warehouse_daily_credits(days, company, bounds=bounds)" in ov  # fallback leg
    # score_inputs folded into the score batch + consumed via preloaded
    assert '{"key": "score_inputs", "sql": mart27_sql.platform_score_inputs(30)' in ov
    assert 'preloaded=_score_pf.get("score_inputs")' in ov


def test_operations_skips_discarded_activity_read():
    src = _read("app/ui/pages/operations.py")
    # the fetch is gated behind _spark_ok so a filtered interaction doesn't fetch-then-discard it
    assert "activity = None\n        if _spark_ok:" in src


def test_decision_studio_batches_scorecard_reads():
    src = _read("app/ui/decision_studio.py")
    assert "from app.core.query import execute_statement, run, run_batch" in src
    assert '_sc_pf = run_batch([' in src
    assert '_q = _sc_pf.get("sc_quarter") or run(' in src
    assert '_ac = _sc_pf.get("sc_appcost") or run(' in src
    assert '_acc = _sc_pf.get("sc_accept") or run(' in src
    # the probe read stays a separate run() to keep probe=True
    assert 'key="sc_precision",' in src and "probe=True" in src


def test_profiler_shows_overview_last_month_zero_account_usage():
    # end-to-end: the headless simulator should now report 0 AU scans on Overview under Last month
    import usage_sim

    report = usage_sim.simulate(pages=["Overview"], scopes={"last_month": {"flt_days": "LAST_MONTH"}},
                                measure_rerun=False)
    ov = next(f for f in report["flows"] if f["page"] == "Overview" and f["scope"] == "last_month")
    assert ov["error"] == "", ov["error"]
    assert ov["account_usage"] == 0, f"Overview Last-month still issues {ov['account_usage']} AU scan(s)"
