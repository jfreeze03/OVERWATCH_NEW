"""Locks for V148 — restore the CoCo/CoWork AI-rate broadening on the exec board.

V079 broadened SP_REFRESH_EXEC_BOARD's sv_daily AI predicate (IS_AI + DRIVER_LABEL) to include
'%COCO%'/'%COWORK%' so SNOWFLAKE_COCO_SNOWSIGHT (Cortex Code / CoWork) prices at the AI rate and
labels 'AI/Cortex:'. V123 re-derived the proc from the pre-V079 V073 base for the account clock and
SILENTLY dropped that broadening, so the Overview cost-driver panel has since priced CoCo at the
compute rate and mislabeled it 'Serverless'. V148 re-derives from V123 (account clock kept) and
restores the broadening — byte-identical to V123 apart from the two predicates + a comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V148 = (_MIG / "V148__exec_board_ai_predicate_restore_coco.sql").read_text(encoding="utf-8")
_V123 = (_MIG / "V123__exec_board_account_clock.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def test_v148_guarded_and_ordered():
    assert "EXCEPTION (-20148" in _V148 and "IF (v < 147) THEN" in _V148
    assert "SELECT 148 AS VERSION" in _V148 and "WHERE VERSION = 148)" in _V148


def test_v148_restores_the_coco_cowork_broadening_in_both_predicates():
    p148 = _proc(_V148)
    # both the IS_AI flag and the DRIVER_LABEL prefix carry the broadening
    broad = ("SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE "
             "'%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%'")
    assert p148.count(broad) == 2, "both IS_AI and DRIVER_LABEL must carry the CoCo/CoWork broadening"
    # and V123 (the reverted base) did NOT — this is the regression being fixed
    assert "'%COCO%'" not in _proc(_V123)


def test_v148_keeps_the_account_clock_and_is_proc_only():
    # V123's whole point (account-clock windows) is preserved; no session/UTC CURRENT_DATE() creeps back
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE" in _V148
    assert "-w.WINDOW_DAYS, CURRENT_DATE()" not in _V148
    # proc-only re-derive: re-CREATE + re-CALL, no new table/task
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD" in _V148
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()" in _V148
    assert "CREATE TRANSIENT TABLE" not in _V148 and "CREATE TASK" not in _V148


def test_v148_proc_is_byte_identical_to_v123_apart_from_the_broadening():
    # normalize V148 back to V123: narrow the two predicates + drop the V148 comment; the two
    # proc bodies must then be identical (proves ONLY the AI predicate + a comment changed).
    p148 = _proc(_V148)
    narrow = ("SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR "
              "SERVICE_TYPE ILIKE '%INTELLIGENCE%'")
    broad = narrow + " OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%'"
    norm = p148.replace(broad, narrow)
    norm = norm.replace(
        "    -- V148: restore V079's CoCo/CoWork broadening (dropped by V123's V073 re-derivation) so\n"
        "    -- SNOWFLAKE_COCO_SNOWSIGHT (Cortex Code / CoWork) prices at the AI rate and labels 'AI/Cortex:',\n"
        "    -- matching app/data/common.ai_service_predicate() and every other AI-rate surface.\n"
        "    sv_daily AS (",
        "    sv_daily AS (")
    assert norm == _proc(_V123), "V148 changed the proc beyond the CoCo/CoWork broadening + its comment"


def test_validate_and_docs_track_v148():
    val = _read("snowflake/validate.sql")
    assert "V001..V159 applied" in val and "VERSION BETWEEN 1 AND 159) = 159" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V148__exec_board_ai_predicate_restore_coco.sql" in _read(rel)


def test_v148_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 148 in _EXPECTED_MIGRATIONS


def test_v148_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V148):
        sqlglot.parse(statement, dialect="snowflake")
