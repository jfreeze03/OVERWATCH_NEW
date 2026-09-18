"""Round-2 bug-hunt regression locks (new sections, deeper pass).

Pins the verified-real R2 defects: the grain-threshold root fix (advise's queued/zero_result
gates now guarded by typical-run signals on the fingerprint grain) and the window/bounds
threading on the two remaining un-bounded builders.
"""

from __future__ import annotations

from datetime import date

from app.data import cortex_sql, insights_sql, ops_sql
from app.logic.query_advisor import advise


# --- #2: queued fires on the fingerprint grain only when queueing is TYPICAL ---
def test_advise_queued_guarded_by_typical_share():
    row = {"QUEUED_SEC": 2.0, "ELAPSED_SEC": 3.0}   # per-run ratio 0.67 > 0.5 gate
    # per-QUERY grain (no QUEUED_RUN_PCT): the per-run ratio is the truth -> fires
    assert any(f.code == "queued" for f in advise(row)[0])
    # fingerprint grain, queueing on only 10% of runs (one storm inflated AVG) -> does NOT fire
    assert not any(f.code == "queued" for f in advise({**row, "QUEUED_RUN_PCT": 0.1})[0])
    # fingerprint grain, queued on most runs -> fires
    assert any(f.code == "queued" for f in advise({**row, "QUEUED_RUN_PCT": 0.8})[0])


# --- #6: zero_result fires on the fingerprint grain only when NO run returned rows ---
def test_advise_zero_result_guarded_by_max_rows():
    row = {"GB_SCANNED": 20.0, "ROWS_PRODUCED": 0.0}   # AVG rows rounded to 0
    # per-QUERY grain (no MAX_ROWS_PRODUCED): rows_produced==0 is the truth -> fires
    assert any(f.code == "zero_result" for f in advise(row)[0])
    # fingerprint grain where some run DID return rows (MAX>0) -> does NOT fire
    assert not any(f.code == "zero_result" for f in advise({**row, "MAX_ROWS_PRODUCED": 1000.0})[0])
    # fingerprint grain, genuinely always-empty -> fires
    assert any(f.code == "zero_result" for f in advise({**row, "MAX_ROWS_PRODUCED": 0.0})[0])


def test_fingerprint_builder_emits_typical_run_guards():
    sql = ops_sql.query_opportunity_fingerprints(30)
    assert "QUEUED_RUN_PCT" in sql and "MAX_ROWS_PRODUCED" in sql


# --- #3 / #5: the two remaining builders now honor the 'Last month' window ------
def test_repeat_query_fingerprints_threads_bounds():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = insights_sql.repeat_query_fingerprints(31, bounds=b)
    trailing = insights_sql.repeat_query_fingerprints(31)
    assert bounded != trailing
    assert "2026-08-01" in bounded and "2026-09-01" in bounded


def test_cortex_source_costs_threads_bounds():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = cortex_sql.cortex_source_costs(31, bounds=b)
    trailing = cortex_sql.cortex_source_costs(31)
    assert bounded != trailing
    assert "2026-08-01" in bounded and "2026-09-01" in bounded
    # the no-bounds path is unchanged (still the shared trailing combined CTE)
    assert "CURRENT_TIMESTAMP()" in trailing
