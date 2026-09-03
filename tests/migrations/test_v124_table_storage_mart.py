"""Locks for V124 — per-table storage mart (perf audit 2026-09-02).

NEW MART_TABLE_STORAGE_DAILY + SP_LOAD_TABLE_STORAGE_MART + daily 07:10 task snapshot the
per-table TABLE_STORAGE_METRICS state (active/time-travel/fail-safe/clone bytes + retention
+ 90d LAST_DML + COMPANY_FOR_DATABASE company). storage_waste + table_storage_breakdown read
it mart-first with the live scan as fallback, so the Spend + Optimize storage panels stop
re-scanning TABLE_STORAGE_METRICS live. NEW proc/task — NOT a re-derivation of the byte-locked
SP_LOAD_MARTS_V27 (V046/V035/V036 standalone-loader precedent)."""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app.data import insights_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V124__table_storage_mart.sql").read_text(encoding="utf-8")


def _cols(sql: str) -> list[str]:
    return [c for c in sqlglot.parse_one(sql, read="snowflake").named_selects if c != "*"]


def test_v124_guard_shape_and_chain():
    assert "EXCEPTION (-20124" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                       # the V035 lesson holds
    assert "IF (v < 123) THEN" in _MIG and "SELECT 124 AS VERSION" in _MIG
    assert "WHERE VERSION = 124" in _MIG
    assert "$_" not in _MIG                                      # $$-only dollar quoting (V089 lesson)


def test_v124_is_a_standalone_mart_not_a_loader_rederivation():
    # additive: a NEW table + NEW standalone proc + NEW task; never re-derives the byte-locked
    # main loader, and never provisions compute.
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_TABLE_STORAGE_MART" in _MIG
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_TABLE_STORAGE" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_TABLE_STORAGE_MART(14);" in _MIG    # first fill
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_TABLE_STORAGE RESUME;" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" not in _MIG  # no re-derivation
    assert "CREATE WAREHOUSE" not in _MIG and "ALTER WAREHOUSE" not in _MIG


def test_v124_snapshot_reads_the_shared_storage_views_and_stamps_company():
    # the snapshot is the SAME scan the live builders do (TSM + 90d DML LAST + TABLES retention),
    # with company stamped by COMPANY_FOR_DATABASE so the reader scopes on a pre-computed column.
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m" in _MIG
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY" in _MIG
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in _MIG    # 90d LAST_DML window
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(m.TABLE_CATALOG)" in _MIG
    assert "SOURCE_FRESHNESS_STATE" in _MIG                      # loader-owned freshness


def test_mart_readers_match_the_live_builders_column_for_column():
    # run_mart_first can only swap the mart in if the columns are identical to the live builder.
    assert _cols(mart_sql.table_storage_waste_mart("ALFA", 1.0)) == _cols(insights_sql.storage_waste("ALFA", 1.0))
    assert _cols(mart_sql.table_storage_breakdown_mart("Trexis", "ALFA_EDW_PRD", 50)) \
        == _cols(insights_sql.table_storage_breakdown("Trexis", "ALFA_EDW_PRD", 50))
    # readers parse for every company + read the latest snapshot; ALL adds no company filter
    for comp in ("ALFA", "Trexis", "UNKNOWN", "ALL"):
        sqlglot.parse_one(mart_sql.table_storage_waste_mart(comp), read="snowflake")
        sqlglot.parse_one(mart_sql.table_storage_breakdown_mart(comp, "", 50), read="snowflake")
    assert "MAX(DAY)" in mart_sql.table_storage_waste_mart("ALFA")
    assert "COMPANY =" not in mart_sql.table_storage_waste_mart("ALL")
    assert "COMPANY = 'ALFA'" in mart_sql.table_storage_waste_mart("ALFA")


def test_storage_panels_read_the_mart_first():
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    spend = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    # both storage drills serve the mart with the live builder as the fallback leg
    assert "mart_sql.table_storage_breakdown_mart(company" in opt and "mart_sql.table_storage_breakdown_mart(company" in spend
    assert "insights_sql.table_storage_breakdown(company" in opt and "insights_sql.table_storage_breakdown(company" in spend
    assert "mart_sql.table_storage_waste_mart(company)" in opt      # the storage-waste fallback path too


def test_validate_floor_and_docs_track_v124():
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V125 applied" in val and "VERSION BETWEEN 1 AND 125) = 125" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V124__table_storage_mart.sql" in (_ROOT / rel).read_text(encoding="utf-8")
