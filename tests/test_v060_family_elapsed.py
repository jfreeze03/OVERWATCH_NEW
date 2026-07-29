"""Locks for V060 — triage #5/#11 + the CS-alert pseudo-warehouse guard.

One additive column + two proc re-derivations: MART_QUERY_FAMILY_DAILY gains
TOTAL_ELAPSED_SEC (so COMPILE_PCT is bounded 0-100), FACT_QUERY_SCHEMA_HOURLY's
QUEUED_SEC includes provisioning time (the FACT_QUERY_HOURLY convention), and
COST_CLOUD_SVC_RATIO's metering subquery excludes the CLOUD_SERVICES_ONLY
pseudo-warehouse via WAREHOUSE_ID > 0.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V60 = (_MIG / "V060__family_elapsed_queued_alert_guard.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v060_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v060.py")],
                       env={**os.environ, "V060_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V60, (
        "V060 drifted from outputs/gen_v060.py — regenerate, never hand-edit.")


def test_v060_guard_and_shape():
    assert "EXCEPTION (-20060" in _V60 and "IF (v < 59) THEN" in _V60 and "SELECT 60 AS VERSION" in _V60
    assert ("ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY\n"
            "    ADD COLUMN IF NOT EXISTS TOTAL_ELAPSED_SEC NUMBER(18,1);") in _V60
    assert _V60.count("CREATE OR REPLACE PROCEDURE") == 2
    for name in ("SP_LOAD_MARTS_V27", "SP_ALERT_SCAN"):
        assert f"-- >>> derived:{name}" in _V60
    assert "CREATE TABLE" not in _V60 and "CREATE TASK" not in _V60          # additive only


def test_v060_qfam_stores_wall_clock():
    """#5: the qfam arm stores TOTAL_ELAPSED_SEC in SELECT, UPDATE and INSERT."""
    marts = _proc(_V60, "SP_LOAD_MARTS_V27")
    assert "ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC," in marts
    assert "TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC," in marts
    assert "TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S," in marts           # INSERT columns
    assert "s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S," in marts     # VALUES


def test_v060_schema_queued_includes_provisioning():
    """#11: FACT_QUERY_SCHEMA_HOURLY's queued expression matches the
    FACT_QUERY_HOURLY convention (OVERLOAD + PROVISIONING)."""
    marts = _proc(_V60, "SP_LOAD_MARTS_V27")
    assert ("ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + "
            "COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,") in marts
    # the overload-only form is gone from the schema arm
    assert "ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC," not in marts


def test_v060_alert_scan_excludes_pseudo_warehouse():
    """Verify-round guard: the CS-ratio alert's metering subquery filters
    WAREHOUSE_ID > 0 so CLOUD_SERVICES_ONLY (ratio = 100%) can never fire it."""
    alert = _proc(_V60, "SP_ALERT_SCAN")
    assert alert.count("AND WAREHOUSE_ID > 0") == 1
    i = alert.index("COST_CLOUD_SVC_RATIO: cloud-services share")
    assert "AND WAREHOUSE_ID > 0" in alert[i:i + 1600]                       # in THAT arm


def test_v060_derived_from_latest_plus_only_the_edits():
    """Byte-restore: V060's marts proc == V059's + exactly the five #5/#11 edits;
    its alert proc == V056's + exactly the one guard edit."""
    v59 = _proc((_MIG / "V059__task_graph_root_credits.sql").read_text(encoding="utf-8"),
                "SP_LOAD_MARTS_V27")
    marts = _proc(_V60, "SP_LOAD_MARTS_V27")
    rederived = (v59
                 .replace("ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,",
                          "ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,\n"
                          "                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,")
                 .replace("TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,",
                          "TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, "
                          "MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,")
                 .replace("TOTAL_EXEC_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)",
                          "TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)")
                 .replace("s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,",
                          "s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,")
                 .replace("ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,",
                          "ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,"))
    assert marts == rederived, "V060 marts proc changed beyond the five enumerated edits"
    v56a = _proc((_MIG / "V056__loader_reconcile_alert_fixes.sql").read_text(encoding="utf-8"),
                 "SP_ALERT_SCAN")
    alert = _proc(_V60, "SP_ALERT_SCAN")
    rederived_a = v56a.replace(
        "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY\n"
        "            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())",
        "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY\n"
        "            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n"
        "              AND WAREHOUSE_ID > 0")
    assert alert == rederived_a, "V060 alert proc changed beyond the one guard edit"


def test_v060_prior_fixes_survive():
    marts = _proc(_V60, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4   # V057
    assert marts.count("loaded := loaded || 'task_node ';") == 1               # V058
    assert marts.count("a ON a.ROOT_ID = h.QUERY_ID") == 1                     # V059
    assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts


def test_v060_app_reader_uses_elapsed_with_degrade():
    """#5 app half: family_compile_heavy averages and bounds COMPILE_PCT on the
    wall-clock column, degrading to exec time for pre-V060 rows (never re-loaded
    beyond the trailing 2 days)."""
    from app.data import mart27_sql
    sql = mart27_sql.family_compile_heavy(30, "ALFA")
    assert sql.count("COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)") == 2   # AVG_TOTAL_S + COMPILE_PCT denom
    assert "/ NULLIF(SUM(f.TOTAL_EXEC_SEC), 0)" not in sql                     # exec-only denominator gone


def test_v060_no_new_teardown_needed():
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "MART_QUERY_FAMILY_DAILY" in td and "SP_ALERT_SCAN" in td           # both pre-exist


def test_v060_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V60):
        sqlglot.parse(stmt, dialect="snowflake")
