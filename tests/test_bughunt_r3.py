"""Round-3 (close-out) bug-hunt regression locks (new sections, last-twin fixes)."""

from __future__ import annotations

from datetime import date

from app.data import insights_sql, ops_sql
from app.logic.failure_advisor import fix_for
from app.logic.query_advisor import advise


# --- #3: compile_bound guarded by COMPILE_RUN_PCT on the fingerprint grain ------
def test_advise_compile_bound_guarded_by_typical_share():
    row = {"COMPILE_SEC": 2.0, "ELAPSED_SEC": 3.0}   # per-run ratio 0.67 > 0.5, elapsed >= 1s
    # per-QUERY grain (no COMPILE_RUN_PCT): the per-run ratio is the truth -> fires
    assert any(f.code == "compile_bound" for f in advise(row)[0])
    # fingerprint grain, compile-heavy on only 10% of runs (one storm inflated AVG) -> no fire
    assert not any(f.code == "compile_bound" for f in advise({**row, "COMPILE_RUN_PCT": 0.1})[0])
    # fingerprint grain, typically compile-heavy -> fires
    assert any(f.code == "compile_bound" for f in advise({**row, "COMPILE_RUN_PCT": 0.8})[0])


def test_fingerprint_builder_emits_compile_run_pct():
    assert "COMPILE_RUN_PCT" in ops_sql.query_opportunity_fingerprints(30)


# --- #1 / #2: the two remaining builders honor the 'Last month' window ----------
def test_clustering_by_table_threads_bounds():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    assert insights_sql.clustering_by_table(30, bounds=b) != insights_sql.clustering_by_table(30)
    assert "2026-08-01" in insights_sql.clustering_by_table(30, bounds=b)


def test_proc_cost_trend_threads_bounds():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = insights_sql.proc_cost_trend("SP_DEMO", 31, bounds=b)
    assert bounded != insights_sql.proc_cost_trend("SP_DEMO", 31)
    assert "2026-08-01" in bounded and "2026-09-01" in bounded


# --- #5: cortex_code rule no longer shadows generic existence errors ------------
def test_cortex_code_view_existence_error_routes_to_generic_grant_fix():
    # an existence/grant error on OVERWATCH's own ACCOUNT_USAGE.CORTEX_CODE_* views must NOT
    # route to the "enable Cortex Code database" remediation (it contains the token cortex_code)
    msg = ("Object 'SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY' "
           "does not exist or not authorized")
    fix = fix_for("", msg)
    assert "Cortex Code" not in fix
    assert "role can't see it" in fix          # the generic does-not-exist/grant fix
    # ...but a genuine MISSING CORTEX_CODE *database* still routes to the Cortex Code fix
    genuine = fix_for("002003", "SQL compilation error: Database 'CORTEX_CODE' does not exist or not authorized.")
    assert "Cortex Code" in genuine
