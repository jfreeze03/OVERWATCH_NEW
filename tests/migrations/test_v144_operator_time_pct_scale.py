"""Locks for V144 — store OP_TIME_PCT on a 0-100 scale.

The owner's STEP-2 probe confirmed EXECUTION_TIME_BREAKDOWN:overall_percentage is a 0-1
fraction, so V144 re-derives SP_LOAD_QUERY_OPERATOR_STATS to multiply it by 100 (matching
SCAN_PCT / the _PCT convention). Proc-only, byte-identical to V143 except that one extraction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V144 = (_MIG / "V144__operator_time_pct_scale.sql").read_text(encoding="utf-8")
_V143 = (_MIG / "V143__query_operator_stats_collector.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v144_guarded_and_ordered():
    assert "EXCEPTION (-20144" in _V144 and "IF (v < 143) THEN" in _V144
    assert "SELECT 144 AS VERSION" in _V144 and "WHERE VERSION = 144)" in _V144


def test_v144_multiplies_overall_percentage_by_100():
    # the fix: the raw 0-1 fraction is scaled to 0-100
    assert "EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT * 100, 2)" in _V144
    # V143 stored it as-is (guards against a stale baseline)
    assert "EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT, 2)" in _V143
    assert "EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT, 2)" not in _V144


def test_v144_is_proc_only_and_matches_v143_except_the_scale():
    # re-derives ONLY the collector proc; no new table/task/DDL, no first-fill CALL.
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS" in _V144
    assert "CREATE TRANSIENT TABLE" not in _V144
    assert "CREATE TASK" not in _V144
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS" not in _V144  # no first-fill
    # the proc body is byte-identical to V143's except the single overall_percentage extraction:
    # replacing the V144 change back to the V143 form makes the two proc bodies equal.

    def _proc(text: str) -> str:
        s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS")
        o = text.index("$$", s)
        return text[s:text.index("$$;", o + 2) + 3]

    p143, p144 = _proc(_V143), _proc(_V144)
    assert p143 != p144
    # normalize the one intended change + the V144-only inline comment, then they match exactly
    norm144 = (p144.replace("overall_percentage::FLOAT * 100, 2)", "overall_percentage::FLOAT, 2)")
                   .replace("                -- V144: overall_percentage is a 0-1 fraction "
                            "(owner probe) -> * 100 for a 0-100 _PCT.\n", ""))
    assert norm144 == p143, "V144 changed the proc beyond the overall_percentage scale"


def test_validate_and_docs_track_v144():
    val = _read("snowflake/validate.sql")
    assert "V001..V146 applied" in val and "VERSION BETWEEN 1 AND 146) = 146" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V144__operator_time_pct_scale.sql" in _read(rel)


def test_v144_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 144 in _EXPECTED_MIGRATIONS


def test_v144_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V144):
        sqlglot.parse(statement, dialect="snowflake")
