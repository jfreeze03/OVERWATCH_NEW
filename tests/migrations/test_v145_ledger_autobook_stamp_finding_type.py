"""Locks for V145 — SP_LEDGER_AUTOBOOK stamps the savings lever (FINDING_TYPE).

The dominant autobook path left FINDING_TYPE NULL, so the Decision Studio ROI by-lever chart
pooled every autobooked saving into 'unclassified'. V145 re-derives the proc (from V118) to
stamp FINDING_TYPE from the source registry SETTING (SIZE -> RESIZE) and backfills existing rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V145 = (_MIG / "V145__ledger_autobook_stamp_finding_type.sql").read_text(encoding="utf-8")
_V118 = (_MIG / "V118__ledger_autobook_dedup.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def test_v145_guarded_and_ordered():
    assert "EXCEPTION (-20145" in _V145 and "IF (v < 144) THEN" in _V145
    assert "SELECT 145 AS VERSION" in _V145 and "WHERE VERSION = 145)" in _V145


def test_v145_stamps_finding_type_in_the_insert():
    # FINDING_TYPE is now in the autobook INSERT column list + valued from SETTING (SIZE->RESIZE)
    assert ("(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, SOURCE_CHANGE_ID, FINDING_TYPE)"
            in _V145)
    assert "CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END" in _V145
    # V118 did NOT stamp it (guards against a stale baseline)
    assert "SOURCE_CHANGE_ID, FINDING_TYPE)" not in _V118


def test_v145_backfills_existing_rows_idempotently():
    # one-time backfill of existing autobook rows, guarded so a re-run is a no-op
    assert "SET FINDING_TYPE = CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END" in _V145
    assert "l.SOURCE_CHANGE_ID = r.CHANGE_ID" in _V145
    assert "COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''), '') = ''" in _V145   # the idempotency guard


def test_v145_proc_matches_v118_except_the_finding_type_stamp():
    # proc-only re-derive: the settle/dedup logic must be byte-identical to V118 — removing the
    # two FINDING_TYPE additions from the V145 proc reproduces the V118 proc exactly.
    p118, p145 = _proc(_V118), _proc(_V145)
    assert p118 != p145
    norm = (p145
            .replace(", SOURCE_CHANGE_ID, FINDING_TYPE)", ", SOURCE_CHANGE_ID)")
            .replace("           r.CHANGE_ID,\n"
                     "           CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END\n",
                     "           r.CHANGE_ID\n")
            # the V145-only INSERT comment line
            .replace("    -- Book detected cost-lever changes as ESTIMATED $0. V145: also stamp "
                     "FINDING_TYPE from the\n    -- source SETTING (SIZE -> RESIZE) so the lever is "
                     "on the row, not just in the free-text NOTE.\n",
                     "    -- Book detected cost-lever changes as ESTIMATED $0 (unchanged from V038).\n"))
    assert norm == p118, "V145 changed the autobook proc beyond the FINDING_TYPE stamp"


def test_validate_and_docs_track_v145():
    val = _read("snowflake/validate.sql")
    assert "V001..V149 applied" in val and "VERSION BETWEEN 1 AND 149) = 149" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V145__ledger_autobook_stamp_finding_type.sql" in _read(rel)


def test_v145_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 145 in _EXPECTED_MIGRATIONS


def test_v145_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V145):
        sqlglot.parse(statement, dialect="snowflake")
