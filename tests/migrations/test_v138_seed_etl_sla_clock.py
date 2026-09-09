"""Locks for V138 — seed the ETL SLA finish-forecast config.

The nightly cycle must finish before a clock deadline (07:00 target / 08:00 hard). V138 seeds
the four editable keys — the two anchor workflows (starter + terminal) and the two clock times —
to the ALFA PRD bookends so Operations ▸ Pipeline ▸ "SLA finish forecast" is live on apply. The
panel already forecasts from the DEFAULT_SETTINGS defaults before this is applied. Data-seed only,
WHEN NOT MATCHED. Reads only CONTROL_STATUS (already granted) — no new grant.
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V138__seed_etl_sla_clock.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v138_guarded_and_ordered():
    assert "EXCEPTION (-20138" in _MIG and "IF (v < 137) THEN" in _MIG
    assert "SELECT 138 AS VERSION" in _MIG and "WHERE VERSION = 138)" in _MIG


def test_v138_data_seed_shape():
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    # data-seed only: no schema/proc/task creation
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG
    assert "\\" not in _MIG   # no backslash escapes in the generated SQL


def test_v138_seeds_the_four_keys_to_prd_bookends():
    for key in ("ETL_CYCLE_START_WORKFLOW", "ETL_CYCLE_END_WORKFLOW",
                "ETL_SLA_TARGET_HHMM", "ETL_SLA_BREACH_HHMM"):
        assert key in DEFAULT_SETTINGS, f"{key} missing from DEFAULT_SETTINGS"
        assert f"'{key}'" in _MIG
    assert "('ETL_CYCLE_START_WORKFLOW', 'WF_BASE_GW_CLOSEOUT_CTL_DLY')" in _MIG
    assert "('ETL_CYCLE_END_WORKFLOW', 'WF_BASE_RECON_MTRC_CMPSIT_DAILY')" in _MIG
    assert "('ETL_SLA_TARGET_HHMM', '07:00')" in _MIG
    assert "('ETL_SLA_BREACH_HHMM', '08:00')" in _MIG
    assert "ALFA_EDW_MGM" not in _MIG


def test_sla_forecast_wired_across_config_sites():
    cfg = _read("app/config.py")
    assert '"ETL_SLA_TARGET_HHMM": "07:00"' in cfg
    assert '"ETL_CYCLE_START_WORKFLOW": "WF_BASE_GW_CLOSEOUT_CTL_DLY"' in cfg
    from app.data import etl_control_sql
    assert hasattr(etl_control_sql, "cycle_finish_history_scan")
    from app.logic.insights import etl_cycle_sla_forecast  # noqa: F401
    ops = _read("app/ui/pages/operations.py")
    assert "_sla_finish_forecast_panel" in ops
    assert 'get("ETL_CYCLE_START_WORKFLOW")' in ops


def test_validate_and_docs_track_v138():
    val = _read("snowflake/validate.sql")
    assert "V001..V138 applied" in val and "VERSION BETWEEN 1 AND 138) = 138" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V138__seed_etl_sla_clock.sql" in _read(rel)


def test_v138_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 138 in _EXPECTED_MIGRATIONS


def test_v138_plain_sql_parses():
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
