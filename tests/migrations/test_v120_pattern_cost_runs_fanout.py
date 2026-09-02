"""Locks for V120 — SP_LOAD_PATTERN_COST run-count fan-out fix (loader-01, round-7 hunt).

QUERY_ATTRIBUTION_HISTORY emits multiple rows per QUERY_ID for an hour-spanning query, so
the old direct a x q join counted attribution rows as RUNS (inflated) and halved
CREDITS_PER_RUN. V120 pre-aggregates QAH to one row per QUERY_ID before the join."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V120__pattern_cost_runs_fanout_fix.sql").read_text(encoding="utf-8")


def test_v120_guard_shape_and_chain():
    assert "EXCEPTION (-20120" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                 # the V035 lesson holds
    assert "IF (v < 119) THEN" in _MIG and "SELECT 120 AS VERSION" in _MIG
    assert "$_" not in _MIG                                # $$-only dollar quoting (V089 lesson)
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(" in _MIG


def test_qah_is_pre_aggregated_per_query_id_before_the_join():
    # the fan-out fix: QAH collapsed to one row per QUERY_ID (SUM credits) BEFORE joining QH.
    assert "FROM (\n                    SELECT QUERY_ID," in _MIG
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n" in _MIG
    assert "GROUP BY QUERY_ID\n                ) a" in _MIG
    assert "JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q\n                  ON q.QUERY_ID = a.QUERY_ID" in _MIG
    # RUNS still COUNT(*) but now counts query executions (one row per query post-collapse);
    # credits come from the pre-aggregated per-QUERY_ID column, not a raw QAH slice.
    assert "COUNT(*) AS RUNS" in _MIG
    assert "SUM(a.CREDITS_ATTRIBUTED) AS CREDITS_ATTRIBUTED" in _MIG
    # the old un-aggregated raw-slice sum is gone
    assert "SUM(a.CREDITS_ATTRIBUTED_COMPUTE + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0))" not in _MIG


def test_migration_tail_reruns_the_loader_and_keeps_the_freshness_stamp():
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(90);" in _MIG   # re-stamp inflated rows
    assert "SOURCE_FRESHNESS_STATE" in _MIG                                  # V068 freshness stamp preserved
    assert "MART_PATTERN_COST_DAILY" in _MIG


def test_validate_floor_and_docs_track_v120():
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V121 applied" in val and "VERSION BETWEEN 1 AND 121) = 121" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V120__pattern_cost_runs_fanout_fix.sql" in (_ROOT / rel).read_text(encoding="utf-8")
