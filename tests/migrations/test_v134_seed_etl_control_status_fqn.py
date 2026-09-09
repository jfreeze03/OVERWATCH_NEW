"""Locks for V134 — seed the ETL process-control Phase 2 config.

Alfa's nightly cycle is Informatica-orchestrated stored-proc CALLs, invisible to
Snowflake's ACCOUNT_USAGE.TASK_HISTORY, so the CONTROL_STATUS table is the only record
of each task's runtime + status per run. V134 seeds ETL_CONTROL_STATUS_FQN to
ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS (owner focus: the PRD database; the source doc's
ALFA_EDW_MGM.PUBLIC mapped to _PRD), so Operations ▸ Pipeline ▸ "Workflow runtimes" is
live on apply. Data-seed only, WHEN NOT MATCHED (never overwrites an operator edit).
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V134__seed_etl_control_status_fqn.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v134_guard_and_data_seed_shape():
    assert "EXCEPTION (-20134" in _MIG and "IF (v < 133) THEN" in _MIG
    assert "SELECT 134 AS VERSION" in _MIG and "WHERE VERSION = 134)" in _MIG
    # data-seed ONLY — a SETTINGS MERGE, never a schema/proc/view/task change
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG


def test_v134_seeds_control_status_fqn_to_prd():
    assert "ETL_CONTROL_STATUS_FQN" in DEFAULT_SETTINGS
    assert "('ETL_CONTROL_STATUS_FQN'," in _MIG
    # owner focus: the PRD database (source doc's ALFA_EDW_MGM mapped to _PRD)
    assert "'ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS'" in _MIG
    assert "ALFA_EDW_MGM" not in _MIG   # must NOT ship the MGM location


def test_control_status_wired_across_config_sites():
    # DEFAULT_SETTINGS ('' code default), the builder, and the live panel must all exist.
    cfg = _read("app/config.py")
    assert '"ETL_CONTROL_STATUS_FQN": ""' in cfg
    from app.data import etl_control_sql
    assert hasattr(etl_control_sql, "workflow_runtimes_scan")
    ops = _read("app/ui/pages/operations.py")
    assert "_workflow_runtimes_panel" in ops
    assert 'get("ETL_CONTROL_STATUS_FQN")' in ops


def test_validate_and_docs_track_v134():
    val = _read("snowflake/validate.sql")
    assert "V001..V137 applied" in val and "VERSION BETWEEN 1 AND 137) = 137" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V134__seed_etl_control_status_fqn.sql" in _read(rel)


def test_v134_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 134 in _EXPECTED_MIGRATIONS


def test_v134_plain_sql_parses():
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
