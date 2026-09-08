"""Locks for V133 — DQ_SCHEMA_DRIFT schema-drift monitor (Codex R23, schema-drift half).

SP_SCAN_SCHEMA_DRIFT snapshots each catalog-registered OBJECT table's columns from
ACCOUNT_USAGE.COLUMNS and diffs today vs the latest prior snapshot, booking one DQ_SCHEMA_DRIFT
alert per table with added/removed/retyped columns. Metadata only (no table-data scan, no grants);
rides the daily TASK_ANOMALY_SWEEP cadence via a CALL arm. The null-rate half stays deferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V133__dq_schema_drift.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v133_guarded_and_ordered():
    assert "EXCEPTION (-20133" in _MIG and "IF (v < 132) THEN" in _MIG
    assert "SELECT 133 AS VERSION" in _MIG and "WHERE VERSION = 133)" in _MIG


def test_v133_creates_snapshot_table_and_scan_proc():
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT" in _MIG
    # metadata source only — the snapshot reads column metadata, never scans table DATA
    assert "SNOWFLAKE.ACCOUNT_USAGE.COLUMNS" in _MIG
    assert "e.ENTITY_TYPE = 'OBJECT'" in _MIG   # scoped to catalog-registered OBJECT tables


def test_v133_seeds_dq_schema_drift_rule():
    assert "('DQ_SCHEMA_DRIFT', 'PIPELINE'" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in _MIG
    assert "WHEN MATCHED THEN UPDATE" not in _MIG   # never clobber an operator's edited rule


def test_v133_diffs_the_three_change_kinds_and_baselines_first_scan():
    assert "'added '" in _MIG and "'removed '" in _MIG and "'retyped '" in _MIG
    # a table with no prior snapshot (first scan) must NOT alert — the added branch requires a baseline
    assert "JOIN prior_day pd ON pd.FQN = c.FQN" in _MIG
    # the removed branch is symmetrically guarded to tables STILL present today, so a dropped/
    # unregistered table doesn't spuriously re-alert "removed all columns" daily (verify MEDIUM finding)
    assert "JOIN (SELECT DISTINCT FQN FROM cur) cf ON cf.FQN = p.FQN" in _MIG
    # the OBJECT-only scope (vs the volume/DQ_BREACH DATABASE-expansion) is documented, not silent
    assert "SCOPE (deliberate)" in _MIG
    # today's snapshot is refreshed idempotently and old snapshots are retained ~90 days
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE()" in _MIG
    assert "SNAPSHOT_DAY < DATEADD('day', -90, CURRENT_DATE())" in _MIG
    # dedup: one alert per (table, day)
    assert "cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())" in _MIG


def test_v133_rides_the_sweep_with_no_new_task():
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()" in _MIG
    assert "CREATE TASK" not in _MIG and "ALTER TASK" not in _MIG   # reuses TASK_ANOMALY_SWEEP
    # re-derived sweep: the new scan proc + the sweep = 2 procs; nothing dropped from the sweep
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "RETURN 'anomaly sweep v3 complete'" in _MIG
    assert "-- DQ_BREACH (R24)" in _MIG and "PIPE_VOLUME_DROP" in _MIG
    assert "'DQ_SCHEMA_DRIFT check skipped'" in _MIG   # arm is exception-guarded


def test_validate_and_docs_track_v133():
    val = _read("snowflake/validate.sql")
    assert "V001..V133 applied" in val and "VERSION BETWEEN 1 AND 133) = 133" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V133__dq_schema_drift.sql" in _read(rel)
    assert "SP_SCAN_SCHEMA_DRIFT" in _read("snowflake/teardown.sql")


def test_v133_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 133 in _EXPECTED_MIGRATIONS


def test_v133_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
