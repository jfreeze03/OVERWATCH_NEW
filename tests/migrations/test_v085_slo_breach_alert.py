"""V085 locks: SLO-breach proactive alert (CoCo Control Room #16, notify half).

A new raiser SP_SLO_BREACH_SCAN evaluates the configured SLO_OBJECTIVES against the
warehouse/task/family marts (the same logic the app's slo_cockpit renders) and raises
a HIGH alert per objective in BREACH, from a task serialized after the hourly mart load.
Hand-authored new objects (proc + task), so locked by substring + parse, not byte-compare.
"""

from __future__ import annotations

from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_V085 = (_ROOT / "snowflake" / "migrations" / "V085__slo_breach_alert.sql").read_text(
    encoding="utf-8"
)


def test_v085_guard_version_house_rules():
    assert "EXCEPTION (-20085" in _V085 and "RAISE not_ready;" in _V085
    assert "RAISE EXCEPTION (" not in _V085
    assert "IF (v < 84) THEN" in _V085
    assert "SELECT 85 AS VERSION" in _V085 and "WHERE VERSION = 85)" in _V085
    assert "CREATE WAREHOUSE" not in _V085 and "RESOURCE MONITOR" not in _V085


def test_v085_seeds_the_performance_rule():
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG" in _V085
    assert "WHEN NOT MATCHED THEN INSERT" in _V085          # idempotent; never clobbers edits
    assert "'PERF_SLO_BREACH' AS RULE_ID" in _V085
    assert "'PERFORMANCE' AS FAMILY" in _V085
    assert "TRUE AS ENABLED, 'HIGH' AS SEVERITY, 1 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS" in _V085


def test_v085_creates_the_raiser_proc():
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN()" in _V085
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" in _V085   # it is a raiser
    assert "RULE_ID = 'PERF_SLO_BREACH'" in _V085                       # scopes cfg to its rule
    # Evaluates the configured objectives against the same three marts as slo_cockpit.
    assert "DBA_MAINT_DB.OVERWATCH.SLO_OBJECTIVES" in _V085
    for mart in ("MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_TASK_NODE_DAILY",
                 "MART_QUERY_FAMILY_DAILY"):
        assert f"DBA_MAINT_DB.OVERWATCH.{mart}" in _V085, mart
    # Only genuine breaches page — STALE / NO_DATA are excluded (the staleness guard
    # is present and the emit filters to STATUS = 'BREACH').
    assert "THEN 'STALE'" in _V085 and "THEN 'NO_DATA'" in _V085
    assert "WHERE e.STATUS = 'BREACH'" in _V085
    # Persistent-condition dedup: per objective per day (not a discrete-event key).
    assert "c.RULE_ID || '|' || e.SLO_ID || '|' || TO_VARCHAR(CURRENT_DATE())" in _V085
    assert "WHERE e.DEDUPE_KEY = b.DEDUPE_KEY" in _V085
    # Hardening from adversarial review:
    # (a) a page needs a real sample — sub-5-observation breaches are gated to NO_DATA.
    assert "COALESCE(m.SAMPLE_N, 0) < 5 THEN 'NO_DATA'" in _V085
    assert "SUM(m.QUERIES) AS SAMPLE_N" in _V085 and "SUM(m.RUNS) AS SAMPLE_N" in _V085
    # (b) burn escalates severity + is carried into the payload for triage.
    assert "IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRITICAL', c.SEVERITY)" in _V085
    assert "AS BURN_MULTIPLE" in _V085
    # (c) TITLE/DETAIL are LEFT-bounded to their column widths (no overflow abort).
    assert "LEFT('SLO breach: ' || e.NAME, 300)" in _V085
    assert ", 2000)" in _V085
    # (d) the scan is exception-isolated: a failure logs to APP_ERROR_LOG, not silent.
    assert "EXCEPTION" in _V085 and "WHEN OTHER THEN" in _V085
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG" in _V085
    assert "'SloBreachScan', 'scan_failed'" in _V085


def test_v085_wires_a_task_after_the_mart_load_without_firing_at_apply():
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN" in _V085
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY" in _V085
    # V075 task-graph law: suspend the root, resume child + root, re-enable dependents.
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;" in _V085
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN RESUME;" in _V085
    assert "SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');" in _V085
    # The scanner is invoked ONLY from the task body, never standalone at apply time
    # (a standalone call would fire alerts on an owner's install).
    assert _V085.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN()") == 1


def test_v085_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V085):
        sqlglot.parse(statement, dialect="snowflake")


def test_v085_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 85 in _EXPECTED_MIGRATIONS


def test_v085_teardown_drops_the_new_objects():
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN SUSPEND;" in teardown
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN;" in teardown
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN();" in teardown
    # Child dropped before its parent mart task.
    assert teardown.index("DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN;") < \
        teardown.index("DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY;")


def test_v085_validate_floor_and_deploy_docs_track_the_migration():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V103 applied" in validate
    assert "BETWEEN 1 AND 103) = 103" in validate
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V085__slo_breach_alert.sql" in (_ROOT / rel).read_text(encoding="utf-8")
