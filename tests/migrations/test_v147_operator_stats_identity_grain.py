"""Locks for V147 — stamp USER_NAME/DATABASE_NAME/SCHEMA_NAME onto the operator-stats fact.

V143/V144 stamped FACT_QUERY_OPERATOR_STATS_DAILY with only COMPANY + WAREHOUSE_NAME, so the
QOIE Slice 2 Operator profile could honor company/warehouse/window but NOT the User/Database/
Schema scope filters. V147 ALTERs in the three identity columns and re-derives
SP_LOAD_QUERY_OPERATOR_STATS from V144 so the set-based enrich UPDATE (which already joins the
query's QUERY_HISTORY row) also fills them — byte-identical to V144 otherwise — plus a one-time
idempotent backfill of the already-collected rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V147 = (_MIG / "V147__operator_stats_identity_grain.sql").read_text(encoding="utf-8")
_V144 = (_MIG / "V144__operator_time_pct_scale.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def test_v147_guarded_and_ordered():
    assert "EXCEPTION (-20147" in _V147 and "IF (v < 146) THEN" in _V147
    assert "SELECT 147 AS VERSION" in _V147 and "WHERE VERSION = 147)" in _V147


def test_v147_adds_the_three_identity_columns():
    for col in ("USER_NAME", "DATABASE_NAME", "SCHEMA_NAME"):
        assert (f"ADD COLUMN IF NOT EXISTS {col} VARCHAR(256)" in _V147), col
    # named for the fact, not ACCOUNT_USAGE — they are added to the collector mart
    assert "ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY" in _V147


def test_v147_enrich_stamps_the_grain_and_is_proc_only_vs_v144():
    # re-derives ONLY the collector proc; no new table/task, no first-fill CALL
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS" in _V147
    assert "CREATE TRANSIENT TABLE" not in _V147
    assert "CREATE TASK" not in _V147
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS" not in _V147  # no first-fill

    p144, p147 = _proc(_V144), _proc(_V147)
    # the enrich UPDATE now stamps the three identity columns from the QUERY_HISTORY row
    for stamp in ("USER_NAME = qh.USER_NAME", "DATABASE_NAME = qh.DATABASE_NAME",
                  "SCHEMA_NAME = qh.SCHEMA_NAME"):
        assert stamp in p147 and stamp not in p144, stamp
    # the V144 scale fix is preserved (not reverted)
    assert "EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT * 100, 2)" in p147

    # normalize the ONLY intended change (the added SET lines + the V147 inline comment) back to
    # V144's form; the two proc bodies must then be byte-identical.
    norm = p147.replace(
        "        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3),\n"
        "        -- V147: identity grain so the Operator profile honors the User/Database/Schema scope\n"
        "        -- filters (same QUERY_HISTORY columns the query-level _query_scope filters on).\n"
        "        USER_NAME = qh.USER_NAME,\n"
        "        DATABASE_NAME = qh.DATABASE_NAME,\n"
        "        SCHEMA_NAME = qh.SCHEMA_NAME\n"
        "    FROM",
        "        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3)\n"
        "    FROM")
    assert norm == p144, "V147 changed the proc beyond the identity-grain enrich SETs"


def test_v147_backfills_existing_rows_idempotently():
    # a one-time backfill AFTER the proc (existing rows carry QUERY_DAY, so the proc enrich
    # skips them); fills only NULLs (idempotent) within the retention + collection-lag window.
    assert "SET USER_NAME = qh.USER_NAME" in _V147
    assert "f.USER_NAME IS NULL" in _V147
    assert "DATEADD('day', -35, CURRENT_TIMESTAMP())" in _V147


def test_validate_and_docs_track_v147():
    val = _read("snowflake/validate.sql")
    assert "V001..V147 applied" in val and "VERSION BETWEEN 1 AND 147) = 147" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V147__operator_stats_identity_grain.sql" in _read(rel)


def test_v147_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 147 in _EXPECTED_MIGRATIONS


def test_v147_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V147):
        sqlglot.parse(statement, dialect="snowflake")
