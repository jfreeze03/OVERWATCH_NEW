"""Locks for V056 — audit Batch B loader/reconcile/alert correctness fixes.

Six proc re-derivations (no schema change): #6 FACT_QUERY_DAILY partial-freeze,
#7 nightly-reconcile D-3 damage, #14 ops-diag hour double-count, #4/#12 alert
dedupe keys, #13 cloud-services-ratio company classification.
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
_V56 = (_MIG / "V056__loader_reconcile_alert_fixes.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v056_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v056.py")],
                       env={**os.environ, "V056_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V56, (
        "V056 drifted from outputs/gen_v056.py — regenerate, never hand-edit.")


def test_v056_guard_and_shape():
    assert "EXCEPTION (-20056" in _V56 and "IF (v < 55) THEN" in _V56 and "SELECT 56 AS VERSION" in _V56
    assert _V56.count("CREATE OR REPLACE PROCEDURE") == 5
    for name in ("SP_LOAD_QH_EXTRACT", "SP_LOAD_MARTS_V27", "SP_NIGHTLY_RECONCILE",
                 "SP_LOAD_OPS_DIAG", "SP_ALERT_SCAN"):
        assert f"-- >>> derived:{name}" in _V56
    assert "CREATE TABLE" not in _V56 and "CREATE TASK" not in _V56          # no new objects


def test_v056_fact_query_daily_is_day_aligned():
    """#6: the FACT_QUERY_DAILY MERGE reads a DAY-aligned complete-day window so
    an aging day freezes complete, not partial."""
    extract = _proc(_V56, "SP_LOAD_QH_EXTRACT")
    daily = extract[extract.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY"):]
    daily = daily[:daily.index("END;")]
    assert "WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())" in daily
    assert "DATEADD('day', -3, CURRENT_TIMESTAMP())" not in daily            # the rolling window is gone


def test_v056_extract_arms_cap_at_two_days():
    """#7: only the FOUR extract-fed day arms of SP_LOAD_MARTS_V27 cap at
    LEAST(:d, 2) — the extract holds at most the last 2 whole days complete. The
    normal hourly call is d=2, so this is a no-op there; only the reconcile's d=3
    is bounded. Full-retention / hour-grain arms are NOT capped."""
    marts = _proc(_V56, "SP_LOAD_MARTS_V27")
    assert marts.count("DATEADD('day', -LEAST(:d, 2), CURRENT_DATE())") == 4
    # the metering-efficiency and hour-grain query arms keep the full -:d window
    assert marts.count("DATEADD('day', -:d, CURRENT_DATE())") >= 4


def test_v056_reconcile_extract_marts_2d_full_retention_3d():
    """#7: the reconcile deletes the FIVE extract-fed day marts at -2d and keeps
    the d=3 call, so the full-retention marts (efficiency, task-graph — which
    genuinely need d=3 for late attribution), the hour-grain marts, and the
    primary metering facts still rebuild D-3 complete."""
    r = _proc(_V56, "SP_NIGHTLY_RECONCILE")
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);" in r     # d=3 kept
    for t in ("MART_QUERY_FAMILY_DAILY", "FACT_QUERY_DAILY", "MART_TAG_COVERAGE_DAILY",
              "MART_COST_ALLOCATION_DAILY", "FACT_COST_ALLOC_XDIM_DAILY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{t}\n     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());" in r, t
    # full-retention day marts + hour-grain marts + primary metering facts stay -3d
    for t in ("MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_TASK_GRAPH_DAILY",
              "FACT_WAREHOUSE_DAILY", "FACT_METERING_DAILY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{t}\n     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());" in r, t
    for t in ("FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{t}\n     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());" in r, t


def test_v056_ops_diag_bounds_are_hour_aligned():
    """#14: both the DELETE and the INSERTs bound on the SAME hour-aligned lower
    edge, so the boundary hour is fully deleted and fully re-inserted (no dup)."""
    ops = _proc(_V56, "SP_LOAD_OPS_DIAG")
    assert ops.count("DATE_TRUNC('hour', DATEADD('day', -:d, CURRENT_TIMESTAMP()))") == 3
    assert "-:d, CURRENT_TIMESTAMP())\n" not in ops.replace("DATE_TRUNC('hour', DATEADD('day', -:d, CURRENT_TIMESTAMP()))", "X")


def test_v056_alert_dedupe_and_classification():
    alert = _proc(_V56, "SP_ALERT_SCAN")
    # #4: PIPE_TASK_FAILURES dedupe key includes SCHEMA_NAME
    assert "COALESCE(tk.DATABASE_NAME, '') || '.' || COALESCE(tk.SCHEMA_NAME, '') || '.' || tk.TASK_NAME" in alert
    # #12: SEC_CRED_EXPIRY dedupe key discriminates EXPIRED vs EXPIRING
    assert "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')" in alert
    # #13: cloud-services ratio company via the UDF, not the name pattern
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME)" in alert
    assert "IFF(w.WAREHOUSE_NAME LIKE 'WH_TRXS%', 'Trexis', 'ALFA')" not in alert


def test_v056_no_new_teardown_needed():
    td = _read("snowflake/teardown.sql")
    # all five procs pre-exist; teardown already covers them
    for p in ("SP_LOAD_QH_EXTRACT", "SP_LOAD_MARTS_V27", "SP_NIGHTLY_RECONCILE",
              "SP_LOAD_OPS_DIAG", "SP_ALERT_SCAN"):
        assert p in td


def test_v056_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V56):
        sqlglot.parse(stmt, dialect="snowflake")
