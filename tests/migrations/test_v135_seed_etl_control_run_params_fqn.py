"""Locks for V135 — seed the ETL run-inventory config (CONTROL_RUN_ID / CONTROL_PARAMS).

The Informatica cycle registers each run in CONTROL_RUN_ID and records the parameters it
ran with in CONTROL_PARAMS. V135 seeds the two FQN config keys to the PRD tables so
Operations ▸ Pipeline ▸ "Run inventory & parameters" is live on apply. Data-seed only,
WHEN NOT MATCHED (never overwrites an operator edit).
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V135__seed_etl_control_run_params_fqn.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v135_guard_and_data_seed_shape():
    assert "EXCEPTION (-20135" in _MIG and "IF (v < 134) THEN" in _MIG
    assert "SELECT 135 AS VERSION" in _MIG and "WHERE VERSION = 135)" in _MIG
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG


def test_v135_seeds_both_keys_to_prd():
    assert "ETL_CONTROL_RUN_ID_FQN" in DEFAULT_SETTINGS and "ETL_CONTROL_PARAMS_FQN" in DEFAULT_SETTINGS
    assert "('ETL_CONTROL_RUN_ID_FQN', 'ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID')" in _MIG
    assert "('ETL_CONTROL_PARAMS_FQN', 'ALFA_EDW_PRD.PUBLIC.CONTROL_PARAMS')" in _MIG
    assert "ALFA_EDW_MGM" not in _MIG


def test_inventory_wired_across_config_sites():
    cfg = _read("app/config.py")
    assert '"ETL_CONTROL_RUN_ID_FQN": ""' in cfg and '"ETL_CONTROL_PARAMS_FQN": ""' in cfg
    from app.data import etl_control_sql
    assert hasattr(etl_control_sql, "run_inventory_scan")
    assert hasattr(etl_control_sql, "run_params_scan")
    ops = _read("app/ui/pages/operations.py")
    assert "_run_inventory_panel" in ops


def test_validate_and_docs_track_v135():
    val = _read("snowflake/validate.sql")
    assert "V001..V135 applied" in val and "VERSION BETWEEN 1 AND 135) = 135" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V135__seed_etl_control_run_params_fqn.sql" in _read(rel)


def test_v135_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 135 in _EXPECTED_MIGRATIONS


def test_v135_plain_sql_parses():
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
