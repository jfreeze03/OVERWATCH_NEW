"""Locks for V136 — seed the ETL Phase 3 reconciliation-DQ config (RECON_MTRC_ERROR).

The nightly recon logs a RECON_MTRC_ERROR row when a metric's SOURCE_LAYER and TARGET_LAYER
don't tie out. V136 seeds ETL_RECON_ERROR_FQN to the PRD recon table so Operations ▸ Pipeline
▸ "Reconciliation errors" is live on apply. Data-seed only, WHEN NOT MATCHED. NOTE the recon
table lives in DB_T_PROD_CORE, not the PUBLIC schema the CONTROL_* tables use.
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V136__seed_etl_recon_error_fqn.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v136_guard_and_data_seed_shape():
    assert "EXCEPTION (-20136" in _MIG and "IF (v < 135) THEN" in _MIG
    assert "SELECT 136 AS VERSION" in _MIG and "WHERE VERSION = 136)" in _MIG
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG


def test_v136_seeds_recon_key_to_prd_core():
    assert "ETL_RECON_ERROR_FQN" in DEFAULT_SETTINGS
    assert "('ETL_RECON_ERROR_FQN', 'ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR')" in _MIG
    assert "ALFA_EDW_MGM" not in _MIG


def test_recon_wired_across_config_sites():
    cfg = _read("app/config.py")
    assert '"ETL_RECON_ERROR_FQN": ""' in cfg
    from app.data import etl_control_sql
    assert hasattr(etl_control_sql, "recon_errors_scan")
    ops = _read("app/ui/pages/operations.py")
    assert "_recon_error_panel" in ops
    assert 'get("ETL_RECON_ERROR_FQN")' in ops


def test_validate_and_docs_track_v136():
    val = _read("snowflake/validate.sql")
    assert "V001..V138 applied" in val and "VERSION BETWEEN 1 AND 138) = 138" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V136__seed_etl_recon_error_fqn.sql" in _read(rel)


def test_v136_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 136 in _EXPECTED_MIGRATIONS


def test_v136_plain_sql_parses():
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
