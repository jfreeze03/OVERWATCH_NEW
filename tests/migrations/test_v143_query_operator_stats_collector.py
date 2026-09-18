"""Locks for V143 — QOIE Slice 2: the operator-level query-profile collector.

FACT_QUERY_OPERATOR_STATS_DAILY + SP_LOAD_QUERY_OPERATOR_STATS + a daily task. The collector
loops the recent (2-day) expensive query_ids from QUERY_HISTORY (query_optimization_triage's
filter set; incremental) and calls GET_QUERY_OPERATOR_STATS per id, landing one row per operator.
These locks pin the ordering guard, the collector's load-bearing semantics (per-id exception
isolation, the incremental skip, the documented OPERATOR_STATISTICS JSON paths, suffix-typed
columns), and the migration-lockstep sites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V143 = (_MIG / "V143__query_operator_stats_collector.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v143_guarded_and_ordered():
    assert "EXCEPTION (-20143" in _V143 and "IF (v < 142) THEN" in _V143
    assert "SELECT 143 AS VERSION" in _V143 and "WHERE VERSION = 143)" in _V143


def test_v143_creates_the_fact_proc_and_task():
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY" in _V143
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS" in _V143
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_QUERY_OPERATOR_STATS" in _V143
    # created suspended then RESUMEd; runs on the admin warehouse (repo task convention)
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_QUERY_OPERATOR_STATS RESUME" in _V143
    assert "WAREHOUSE = WH_ALFA_ADMIN" in _V143
    # first-fill so the mart-first reader serves immediately
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(" in _V143


def test_v143_calls_the_operator_stats_function_per_id_and_is_exception_isolated():
    # the collector is per-query-id (no bulk form exists); the table-function arg cannot be
    # bound, so it is built with EXECUTE IMMEDIATE, and each id is wrapped in EXCEPTION WHEN
    # OTHER so an aged/utility/unauthorized id is skipped, never aborting the run.
    assert "TABLE(GET_QUERY_OPERATOR_STATS(" in _V143
    assert "EXECUTE IMMEDIATE :ins" in _V143
    assert "FOR q IN c_qids DO" in _V143
    assert "EXCEPTION" in _V143 and "WHEN OTHER THEN" in _V143


def test_v143_candidate_window_stays_inside_the_14_day_operator_stats_reach():
    # GET_QUERY_OPERATOR_STATS only resolves queries completed in the past 14 days; the
    # candidate window must be well inside it (a 365-day QUERY_HISTORY window would feed ids
    # the function cannot resolve). A tight 2-day window is used.
    assert "DATEADD('day', -2, CURRENT_TIMESTAMP())" in _V143


def test_v143_reuses_the_triage_self_noise_filters_and_is_incremental():
    # same self-noise + inefficiency gate as query_optimization_triage, so the two surfaces
    # never contradict; NOT EXISTS makes re-runs incremental (skip already-collected ids).
    for clause in (
        "EXECUTION_STATUS = 'SUCCESS'",
        "qh.QUERY_TYPE <> 'CALL'",
        "NOT LIKE 'EXECUTE STREAMLIT%'",
        "NOT LIKE '%OVERWATCH_APP%'",
        "COALESCE(qh.QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
        "BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0",
    ):
        assert clause in _V143, clause
    assert "NOT EXISTS (" in _V143
    assert "FACT_QUERY_OPERATOR_STATS_DAILY f" in _V143


def test_v143_extracts_the_documented_operator_statistics_paths():
    # the three pathologies Slice 2 unlocks, from the documented VARIANT sub-keys.
    for path in (
        "OPERATOR_STATISTICS:input_rows",
        "OPERATOR_STATISTICS:output_rows",
        "OPERATOR_STATISTICS:spilling:bytes_spilled_remote_storage",
        "OPERATOR_STATISTICS:spilling:bytes_spilled_local_storage",
        "OPERATOR_STATISTICS:pruning:partitions_scanned",
        "OPERATOR_STATISTICS:pruning:partitions_total",
        "OPERATOR_STATISTICS:io:bytes_scanned",
        "EXECUTION_TIME_BREAKDOWN:overall_percentage",
        "PARENT_OPERATORS[0]",   # ARRAY column (BCR-1175): store the first parent
    ):
        assert path in _V143, path


def test_v143_columns_carry_the_humanize_suffixes():
    # owner's duration/byte/pct-humanize naming rule (the styled_table auto-format contract).
    for col in ("QUERY_ELAPSED_SEC", "REMOTE_SPILL_GB", "LOCAL_SPILL_GB", "GB_SCANNED",
                "SCAN_PCT", "OP_TIME_PCT", "INPUT_ROWS", "OUTPUT_ROWS", "LOAD_TS"):
        assert col in _V143, col
    # the fingerprint join key back to QOIE Slice 1, and the scope axis
    assert "QUERY_PARAMETERIZED_HASH" in _V143
    assert "COMPANY_FOR_WAREHOUSE" in _V143


def test_v143_enriches_set_based_and_registers_freshness():
    assert "UPDATE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f" in _V143
    assert "f.QUERY_DAY IS NULL" in _V143    # scopes the enrich to just-landed rows
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" in _V143
    assert "'FACT_QUERY_OPERATOR_STATS_DAILY'" in _V143


def test_validate_and_docs_track_v143():
    val = _read("snowflake/validate.sql")
    assert "V001..V145 applied" in val and "VERSION BETWEEN 1 AND 145) = 145" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V143__query_operator_stats_collector.sql" in _read(rel)


def test_v143_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 143 in _EXPECTED_MIGRATIONS


def test_v143_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V143):
        sqlglot.parse(statement, dialect="snowflake")
