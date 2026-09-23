"""Locks for V139 — object-cost loader search-opt column fix.

Snowflake renamed SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY.TABLE_NAME ->
BASE_TABLE_NAME (the view now carries INDEX_NAME/INDEX_ID/BASE_TABLE_ID/BASE_TABLE_NAME/
INDEX_TYPE). SP_LOAD_OBJECT_COST's search-opt arm referenced TABLE_NAME, so it threw
'invalid identifier TABLE_NAME' at run time every night, the atomic load rolled back, and
FACT_OBJECT_COST_DAILY froze at its 2026-09-09 fill. V139 re-derives the proc from V067,
changing ONLY that one arm's object-name column, and backfills 14d on apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "snowflake" / "migrations"
_V139 = (_MIG_DIR / "V139__object_cost_search_opt_column.sql").read_text(encoding="utf-8")
_V67 = (_MIG_DIR / "V067__alert_attribution_onset_supersede_objectcost.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    start = text.find(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert start > 0, name
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v139_guarded_and_ordered():
    assert "EXCEPTION (-20139" in _V139 and "IF (v < 138) THEN" in _V139
    assert "SELECT 139 AS VERSION" in _V139 and "WHERE VERSION = 139)" in _V139


def test_v139_proc_is_v067_with_only_the_search_opt_column_renamed():
    """The strongest guard: the proc is byte-identical to V067's except the ONE
    search-opt arm column. If the hand-copy drifted anywhere else, this fails."""
    v067_proc = _proc(_V67, "SP_LOAD_OBJECT_COST")
    v139_proc = _proc(_V139, "SP_LOAD_OBJECT_COST")
    expected = v067_proc.replace(
        "COALESCE(TABLE_NAME, 'UNKNOWN'),\n           'TABLE', 'SEARCH_OPT',",
        "COALESCE(BASE_TABLE_NAME, 'UNKNOWN'),\n           'TABLE', 'SEARCH_OPT',",
    )
    # the anchor must actually have matched (V067 really did use TABLE_NAME there)
    assert expected != v067_proc
    assert v139_proc == expected


def test_v139_only_the_search_opt_arm_changed_columns():
    # the fixed arm now reads the current Snowflake column
    assert "COALESCE(BASE_TABLE_NAME, 'UNKNOWN'),\n           'TABLE', 'SEARCH_OPT'," in _V139
    # the FROM SEARCH_OPTIMIZATION_HISTORY arm must no longer reference the old TABLE_NAME
    so_arm = _V139.split("'TABLE', 'SEARCH_OPT',", 1)[0].rsplit("INSERT INTO", 1)[1]
    assert "BASE_TABLE_NAME" in so_arm and "COALESCE(TABLE_NAME" not in so_arm
    # every other arm is unchanged: clustering + MV keep TABLE_NAME (2 refs), task/pipe keep theirs
    assert _V139.count("COALESCE(TABLE_NAME, 'UNKNOWN')") == 2      # clustering + MV only
    assert "'TABLE', 'CLUSTERING'," in _V139 and "'MATERIALIZED_VIEW', 'MV_REFRESH'," in _V139
    assert "COALESCE(TASK_NAME, 'UNKNOWN')" in _V139               # serverless-task arm
    assert "COALESCE(PIPE_NAME, 'UNKNOWN_PIPE')" in _V139          # snowpipe arm
    # atomic-load contract preserved (rollback keeps the previous fill)
    assert "BEGIN TRANSACTION;" in _V139 and "ROLLBACK;" in _V139
    assert "'object_cost_load_failed'" in _V139
    assert "\\" not in _V139   # no backslash escapes in the generated SQL


def test_v139_backfills_the_gap_on_apply():
    # heal 2026-09-10..present the moment it is applied (matches the V048 first-fill window)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);" in _V139


def test_validate_and_docs_track_v139():
    val = _read("snowflake/validate.sql")
    assert "V001..V150 applied" in val and "VERSION BETWEEN 1 AND 150) = 150" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V139__object_cost_search_opt_column.sql" in _read(rel)


def test_v139_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 139 in _EXPECTED_MIGRATIONS


def test_v139_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V139):
        sqlglot.parse(statement, dialect="snowflake")
