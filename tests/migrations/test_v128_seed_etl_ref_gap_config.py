"""Locks for V128 — seed the ETL reference-data gap monitor config.

The nightly load hard-fails when a source system emits a code with no XLAT translation
row; the operator ran a manual MINUS every morning to catch it. V128 seeds the two
config keys (ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS) with the pinned pc_uwissuetype.code
check, so Operations ▸ Pipeline ▸ "Reference-data gaps" is live on apply. Data-seed only,
WHEN NOT MATCHED (never overwrites an operator edit)."""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V128__seed_etl_ref_gap_config.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v128_guard_and_data_seed_shape():
    assert "EXCEPTION (-20128" in _MIG and "IF (v < 127) THEN" in _MIG
    assert "SELECT 128 AS VERSION" in _MIG and "WHERE VERSION = 128)" in _MIG
    # data-seed ONLY — a SETTINGS MERGE, never a schema/proc/view/task change
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG


def test_v128_seeds_both_config_keys():
    # both keys are real editable settings now, and the seed carries the pinned morning check
    assert "ETL_REF_GAP_XLAT" in DEFAULT_SETTINGS and "ETL_REF_GAP_CHECKS" in DEFAULT_SETTINGS
    assert "('ETL_REF_GAP_XLAT'," in _MIG
    assert "TERADATA_ETL_REF_XLAT" in _MIG
    assert "('ETL_REF_GAP_CHECKS'," in _MIG
    # pinned (leading '*') so pc_uwissuetype always shows past the Database filter
    assert "'*pc_uwissuetype.code | ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE | CODE_STG'" in _MIG


def test_ref_gap_wired_across_config_sites():
    # DEFAULT_SETTINGS ('' code default), the builder module, and the live panel must all exist.
    cfg = _read("app/config.py")
    assert '"ETL_REF_GAP_XLAT": ""' in cfg and '"ETL_REF_GAP_CHECKS": ""' in cfg
    assert (_ROOT / "app" / "data" / "etl_control_sql.py").exists()
    ops = _read("app/ui/pages/operations.py")
    assert "_reference_gap_panel" in ops
    assert 'settings.get("ETL_REF_GAP_XLAT")' in ops and 'settings.get("ETL_REF_GAP_CHECKS")' in ops


def test_validate_and_docs_track_v128():
    val = _read("snowflake/validate.sql")
    assert "V001..V138 applied" in val and "VERSION BETWEEN 1 AND 138) = 138" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V128__seed_etl_ref_gap_config.sql" in _read(rel)


def test_v128_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 128 in _EXPECTED_MIGRATIONS


def test_v128_plain_sql_parses():
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
