"""Locks for V083: ACTION_QUEUE.PERIOD -- the time basis of ESTIMATED_USD (DS #7).

A pure additive column (no Snowflake proc writes ESTIMATED_USD), stamped app-side at
both writers and surfaced in every reader so estimates authored on different time
bases are no longer summed or compared as one number.
"""
from pathlib import Path

import pytest

from app.data import mart_sql, workbench_sql
from app.logic.workbench import ACTION_ESTIMATE_PERIODS, create_action_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_V83 = (_ROOT / "snowflake" / "migrations" / "V083__action_estimate_period.sql").read_text(
    encoding="utf-8"
)


# --- the migration file ----------------------------------------------------

def test_v083_guard_version_house_rules():
    assert "EXCEPTION (-20083" in _V83 and "RAISE not_ready;" in _V83
    assert "RAISE EXCEPTION (" not in _V83
    assert "IF (v < 82) THEN" in _V83 and "SELECT 83 AS VERSION" in _V83
    assert "WHERE VERSION = 83)" in _V83


def test_v083_is_a_pure_additive_column_with_no_new_objects():
    # ALTER-only: adds PERIOD to the pre-existing operator table and derives/creates
    # nothing (unlike V080-V082, which re-derive a proc -> a gen script + byte-compare).
    assert "ALTER TABLE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE" in _V83
    assert "ADD COLUMN IF NOT EXISTS PERIOD VARCHAR(20)" in _V83
    for forbidden in ("CREATE OR REPLACE PROCEDURE", "CREATE OR REPLACE VIEW",
                      "CREATE TABLE", "CREATE TRANSIENT TABLE", "CREATE TASK"):
        assert forbidden not in _V83, forbidden


def test_v083_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V83):
        sqlglot.parse(stmt, dialect="snowflake")


# --- the app rewire: writers stamp PERIOD ----------------------------------

def test_create_action_sql_stamps_and_validates_period():
    labeled = create_action_sql(
        title="Tune idle warehouse", detail="", company="ALFA", severity="HIGH",
        owner="DBA", due_date=None, source="test", entity_type="WAREHOUSE",
        entity_key="WH_X", estimated_usd=1200, period="monthly",
    )
    assert "ESTIMATED_USD, PERIOD)" in labeled and "'MONTHLY'" in labeled
    sqlglot.parse(labeled, dialect="snowflake")

    # Default is unspecified -> NULL, and the PERIOD column is always in the insert
    # (so the shape never depends on whether a basis was passed).
    unlabeled = create_action_sql(
        title="x", detail="", company="ALFA", severity="LOW", owner="DBA",
        due_date=None, source="test", entity_type="WAREHOUSE", entity_key="WH_X",
        estimated_usd=10,
    )
    assert "ESTIMATED_USD, PERIOD)" in unlabeled
    assert unlabeled.rstrip().endswith("NULL")   # period_sql = NULL trails the SELECT
    sqlglot.parse(unlabeled, dialect="snowflake")

    with pytest.raises(ValueError):
        create_action_sql(
            title="x", detail="", company="ALFA", severity="LOW", owner="DBA",
            due_date=None, source="test", entity_type="WAREHOUSE", entity_key="WH_X",
            estimated_usd=10, period="WEEKLY",
        )
    assert "" in ACTION_ESTIMATE_PERIODS and "MONTHLY" in ACTION_ESTIMATE_PERIODS


def test_ai_chargeback_queue_insert_stamps_monthly():
    # The AI-chargeback queue insert stores a 30-day projection -> stamp MONTHLY.
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(
        encoding="utf-8"
    )
    assert "OWNER, SOURCE, ESTIMATED_USD, PERIOD)" in src
    assert "'MONTHLY'" in src


# --- the app rewire: readers surface PERIOD --------------------------------

def test_action_queue_readers_select_period():
    for sql in (workbench_sql.action_center("ALFA", True), mart_sql.action_queue(200)):
        assert "PERIOD" in sql
        sqlglot.parse(sql, dialect="snowflake")


# --- lockstep --------------------------------------------------------------

def test_v083_validate_floor_and_deploy_docs_track_the_migration():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V115 applied" in validate
    assert "BETWEEN 1 AND 115) = 115" in validate
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V083__action_estimate_period.sql" in (_ROOT / rel).read_text(encoding="utf-8")
