"""Locks for V055 — persisted per-query cloud-services credits + drill-down.

Owner ask 2026-07-28: drill the COST_CLOUD_SVC_RATIO alert into the query
shapes / users driving the cloud-services credits. V055 adds
CREDITS_USED_CLOUD_SERVICES to the QH extract, re-derives SP_LOAD_QH_EXTRACT to
fill it and cascade a new SP_LOAD_CLOUD_SVC_MART, and ships MART_CLOUD_SVC_DAILY
at (day, company, warehouse, user, role, query_type, parameterized_hash) grain.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V55 = (_MIG / "V055__cloud_services_breakdown.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v055_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v055.py")],
                       env={**os.environ, "V055_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V55, (
        "V055 drifted from outputs/gen_v055.py — regenerate, never hand-edit.")


def test_v055_guard_version_house_rules():
    assert "EXCEPTION (-20055" in _V55 and "RAISE not_ready;" in _V55
    assert "IF (v < 54) THEN" in _V55 and "SELECT 55 AS VERSION" in _V55


def test_v055_additive_column_and_new_objects():
    assert "ADD COLUMN IF NOT EXISTS CREDITS_USED_CLOUD_SERVICES FLOAT" in _V55
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY" in _V55
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART" in _V55
    # three procs: the new loader + the two re-derived ones
    assert _V55.count("CREATE OR REPLACE PROCEDURE") == 3
    assert "-- >>> derived:SP_LOAD_QH_EXTRACT" in _V55
    assert "-- >>> derived:SP_PURGE_FACTS" in _V55


def test_v055_extract_fills_and_cascades_no_new_scan():
    """The extract re-derivation adds the CS column to its single QUERY_HISTORY
    scan (no second scan) and cascades an ISOLATED call to the mart loader."""
    extract = _proc(_V55, "SP_LOAD_QH_EXTRACT")
    # exactly one QUERY_HISTORY scan remains (the extract's own)
    assert extract.count("ACCOUNT_USAGE.QUERY_HISTORY") == 1
    assert "CREDITS_USED_CLOUD_SERVICES)" in extract           # INSERT column list
    assert "LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)" in extract
    # cascade is isolated: its own BEGIN/EXCEPTION, logs and continues
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();" in extract
    assert "'cloud_svc_mart_failed'" in extract


def test_v055_extract_is_v042_plus_only_the_cs_edits():
    v042 = _proc(_read("snowflake/migrations/V042__codex_r22.sql"), "SP_LOAD_QH_EXTRACT")
    v055 = _proc(_V55, "SP_LOAD_QH_EXTRACT")
    # the only removed lines are the two extended column lists
    removed = [ln for ln in v042.splitlines() if ln not in v055.splitlines()]
    assert removed == ["         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT)",
                       "           LEFT(QUERY_TEXT, 200)"], removed


def test_v055_purge_is_v054_plus_only_the_mart_delete():
    v054 = _proc(_read("snowflake/migrations/V054__exec_board_window_history.sql"), "SP_PURGE_FACTS")
    v055 = _proc(_V55, "SP_PURGE_FACTS")
    # The WHERE and `total := ...` lines are byte-identical to every other daily
    # DELETE already in the proc, so the ONLY line unique to V055 is the mart
    # DELETE target itself — proof nothing else changed.
    added = [ln for ln in v055.splitlines() if ln not in v054.splitlines()]
    assert added == ["    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY"], added


def test_v055_mart_loader_reads_extract_cs_positive_only():
    loader = _proc(_V55, "SP_LOAD_CLOUD_SVC_MART")
    assert "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT" in loader   # not QUERY_HISTORY
    assert "ACCOUNT_USAGE" not in loader
    assert "COALESCE(CREDITS_USED_CLOUD_SERVICES, 0) > 0" in loader
    assert "COMPANY_FOR_WAREHOUSE" in loader                       # company via the UDF


def test_v055_teardown_covers_new_objects():
    td = _read("snowflake/teardown.sql")
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY;" in td
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();" in td


def test_v055_app_builders_read_the_mart_not_account_usage():
    from app.data import mart_sql
    for sql in (mart_sql.cloud_svc_top_shapes(7, "ALFA", "WH_X"),
                mart_sql.cloud_svc_by_user(7, "ALFA", "WH_X")):
        assert "MART_CLOUD_SVC_DAILY" in sql
        assert "ACCOUNT_USAGE" not in sql          # mart-only, no live scan
        assert "WAREHOUSE_NAME = 'WH_X'" in sql    # warehouse-scoped
    # both are registered in the canary
    can = _read("app/data/canary.py")
    assert "mart.cloud_svc_top_shapes" in can and "mart.cloud_svc_by_user" in can


def test_v055_recheck_is_warehouse_aware():
    """The CS-ratio recheck now matches the alert: per-warehouse
    WAREHOUSE_METERING_HISTORY, CS / total credits (was account-wide)."""
    from app.data import recheck_sql
    assert recheck_sql.RECHECKABLE["COST_CLOUD_SVC_RATIO"][0] is True   # needs warehouse
    sql = recheck_sql.recheck_sql("COST_CLOUD_SVC_RATIO", "WH_X")
    assert sql and "WAREHOUSE_METERING_HISTORY" in sql
    assert "UPPER(WAREHOUSE_NAME) = 'WH_X'" in sql
    assert "ACCOUNT_USAGE.METERING_HISTORY" not in sql                 # not the account-wide table
    # no warehouse extracted -> no recheck (rather than an account-wide answer)
    assert recheck_sql.recheck_sql("COST_CLOUD_SVC_RATIO", "") is None


def test_v055_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V55):
        sqlglot.parse(stmt, dialect="snowflake")
