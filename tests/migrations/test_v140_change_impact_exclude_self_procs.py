"""Locks for V140 — change-impact detector excludes OVERWATCH's own DBA_MAINT_DB objects.

SP_CHANGE_IMPACT_SCAN registered OVERWATCH's own maintenance procs/tasks into
OBJECT_CHANGE_REGISTRY. Because those are CREATE OR REPLACE'd on every migration and often run a
one-time apply-time backfill CALL, the heavy one-off call inflated the after-window p95/credits and
tripped a false "PROCEDURE ... regressed after <date>" CRITICAL (SP_LOAD_PATTERN_COST after V120;
SP_LOAD_OBJECT_COST after V139 would have followed). V140 re-derives the scan from V061 adding a
DBA_MAINT_DB exclusion to each registration arm, and one-time resolves the open self-object alerts
and drops their registry rows. OVERWATCH's own plumbing stays monitored via SOURCE_FRESHNESS_STATE
+ per-loader error logging.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V140 = (_MIG / "V140__change_impact_exclude_self_procs.sql").read_text(encoding="utf-8")
_V61 = (_MIG / "V061__ai_loader_alert_score_purge_fixes.sql").read_text(encoding="utf-8")

_EDIT1 = ("          AND PROCEDURE_CATALOG IS NOT NULL\n          AND LAST_ALTERED",
          "          AND PROCEDURE_CATALOG IS NOT NULL\n          AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'"
          "   -- V140: OVERWATCH's own procs are self-monitored (freshness + per-loader error log), not change-impact-tracked\n"
          "          AND LAST_ALTERED")
_EDIT2 = ("            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())\n              AND PREV_DEFINITION IS NOT NULL",
          "            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())\n              AND DATABASE_NAME <> 'DBA_MAINT_DB'"
          "   -- V140: OVERWATCH's own tasks are self-monitored, not change-impact-tracked\n"
          "              AND PREV_DEFINITION IS NOT NULL")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def test_v140_guarded_and_ordered():
    assert "EXCEPTION (-20140" in _V140 and "IF (v < 139) THEN" in _V140
    assert "SELECT 140 AS VERSION" in _V140 and "WHERE VERSION = 140)" in _V140


def test_v140_proc_is_v061_with_only_the_two_self_exclusions():
    """Strong guard: the scan proc is byte-identical to V061's except the two
    DBA_MAINT_DB registration filters. Any other drift fails here."""
    v061 = _proc(_V61, "SP_CHANGE_IMPACT_SCAN()")
    v140 = _proc(_V140, "SP_CHANGE_IMPACT_SCAN()")
    expected = v061.replace(*_EDIT1).replace(*_EDIT2)
    assert expected != v061          # both anchors actually matched V061
    assert v140 == expected


def test_v140_excludes_self_db_from_both_registration_arms():
    assert "PROCEDURE_CATALOG <> 'DBA_MAINT_DB'" in _V140   # procedures arm (1a)
    assert "DATABASE_NAME <> 'DBA_MAINT_DB'" in _V140       # tasks arm (1b)
    assert _V140.count("<> 'DBA_MAINT_DB'") == 2            # exactly the two registration filters
    assert "\\" not in _V140                                # no backslash escapes


def test_v140_clears_the_existing_false_self_alerts():
    # resolve the open self-object change-impact alerts, and drop their registry rows
    assert "DEDUPE_KEY LIKE 'PERF_CHANGE_REGRESSION|DBA_MAINT_DB.%'" in _V140
    assert "STATUS = 'RESOLVED'" in _V140 and "RESOLUTION_KIND = 'EXPECTED'" in _V140
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY" in _V140
    assert "WHERE DATABASE_NAME = 'DBA_MAINT_DB'" in _V140


def test_validate_and_docs_track_v140():
    val = _read("snowflake/validate.sql")
    assert "V001..V159 applied" in val and "VERSION BETWEEN 1 AND 159) = 159" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V140__change_impact_exclude_self_procs.sql" in _read(rel)


def test_v140_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 140 in _EXPECTED_MIGRATIONS


def test_v140_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V140):
        sqlglot.parse(statement, dialect="snowflake")
