"""Locks for V040 + the r13 perf batch (v4.31.0).

Fleet evidence drove this batch (screenshots 2026-07-11): 1.5-3.4% cache
hits on the top pages with ONE viewer = TTL exhaustion; steering and the
contract-consumed rescan sat in the slow-key list."""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V040__freshness_state.sql").read_text(encoding="utf-8")


def test_v040_guard_and_pieces():
    assert "EXCEPTION (-20040" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG
    assert "IF (v < 39) THEN" in _MIG and "SELECT 40 AS VERSION" in _MIG
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SNAPSHOT_FRESHNESS();" in _MIG   # seeded on apply
    assert "USING CRON */10" in _MIG                       # 10-min snapshot cadence
    assert "FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS" in _MIG      # view stays the writer
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS;" in td
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_SNAPSHOT_FRESHNESS();" in td
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE;" in td


def test_freshness_reads_are_lookups_with_the_view_as_fallback():
    state = mart_sql.source_freshness_state()
    assert "SOURCE_FRESHNESS_STATE" in state and "HOURS_SINCE_LOAD" in state
    strip = mart_sql.health_strip()
    assert "SOURCE_FRESHNESS_STATE" in strip and "MART_SOURCE_FRESHNESS" not in strip
    for page in ("control_room", "admin"):
        src = (_ROOT / "app" / "ui" / "pages" / f"{page}.py").read_text(encoding="utf-8")
        assert "source_freshness_state" in src             # state-first
        assert "pre-V040 fallback" in src                  # view still serves old deploys


def test_hourly_tier_exists_and_mart_first_uses_it():
    from app.core.query import CACHE_TTLS, STATEMENT_TIMEOUTS
    assert CACHE_TTLS["hourly"] == 3600 and "hourly" in STATEMENT_TIMEOUTS
    import inspect

    from app.ui.components import run_mart_first
    sig = inspect.signature(run_mart_first)
    assert sig.parameters["mart_tier"].default == "hourly"  # r13 #3: sources load hourly


def test_contract_consumed_is_fact_first_with_coverage_guard():
    sql = mart_sql.fact_contract_consumed("2026-01-01")
    assert "FACT_METERING_DAILY" in sql and "FIRST_DAY" in sql
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "fact_contract_consumed" in ct
    assert "mart_accept" in ct                             # coverage predicate gates trust
    assert "coverage fallback" in ct


def test_steering_is_mart_first():
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    seg = ct.split("steer_idle", 1)[1]
    assert "eff_idle_analysis" in ct and "pattern_cost(30" in ct
    assert "live fallback" in seg[:1200]                   # live builders remain the fallback


def test_compare_adjacent_windows_prune_as_one_range():
    a = mart27_sql.compare_activity("2026-06-01", "2026-07-01", "2026-05-01", "2026-06-01", "ALFA")
    assert "HOUR_TS >= '2026-05-01' AND HOUR_TS < '2026-07-01'" in a   # contiguous
    gap = mart27_sql.compare_billed("2026-07-01", "2026-07-12", "2026-06-01", "2026-06-12")
    assert ") OR (" in gap                                 # non-adjacent keeps two ranges
    p = mart27_sql.compare_pattern_costs("2026-06-01", "2026-07-01", "2026-05-01", "2026-06-01", "ALFA")
    assert "GREATEST" in p and "LEAST" in p                # sample subquery bounded both ends


def test_exports_are_lazy_for_big_frames_and_styler_bounded():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    from app.ui.components import STYLER_MAX_ROWS
    assert STYLER_MAX_ROWS == 400
    assert "ow_dlprep_" in comp                            # two-step export path exists
    seg = comp.split("ow_dlprep_", 1)[0].split("every real table is exportable", 1)[1]
    assert "len(df) <= 200" in seg                         # small frames stay one-click
