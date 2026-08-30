"""V098 locks: SP_INCIDENT_AUTODECLARE must not re-link an already-membered alert.

The member INSERT re-scanned ALERT_EVENTS with no anti-membership guard, so an alert already
a member of one incident (e.g. a still-OPEN CRITICAL whose incident was resolved without
resolving the alert) could be re-attached to a second incident and double-counted. V098
re-derives the proc with the same NOT EXISTS INCIDENT_MEMBERS guard the crit CTE already uses
added to the member INSERT. Byte-locked to outputs/gen_v098.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V098 = (_MIG / "V098__incident_autodeclare_relink_guard.sql").read_text(encoding="utf-8")
_V032 = (_MIG / "V032__incident_object.sql").read_text(encoding="utf-8")


def test_v098_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v098.py")],
        env={**os.environ, "V098_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V098, (
        "V098 drifted from its forward-generation — edit outputs/gen_v098.py, "
        "not the .sql, then regenerate."
    )


def test_v098_is_one_guarded_proc_redefinition():
    assert "EXCEPTION (-20098" in _V098 and "IF (v < 97) THEN" in _V098
    assert "SELECT 98 AS VERSION" in _V098 and "WHERE VERSION = 98)" in _V098
    assert _V098.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE" in _V098
    assert "CREATE TABLE" not in _V098 and "ALTER TABLE" not in _V098
    assert "CREATE OR REPLACE VIEW" not in _V098 and "CREATE TASK" not in _V098


def test_v098_member_insert_guards_against_relink():
    # the member INSERT now carries the anti-membership guard (alias m2)
    assert "WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);" in _V098
    # both guards now present: the crit CTE's (m) and the member INSERT's (m2)
    assert _V098.count("m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID") == 1
    assert _V098.count("m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID") == 1
    # untouched anchors: crit CTE guard + family-already-open guard survive
    assert "AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY" in _V098
    assert "RETURN 'auto-declared ' || :made || ' incident(s)'" in _V098
    # V032 (base) has only the crit CTE guard, NOT the member-INSERT guard, confirming the bug
    assert "m.REF_ID = e.EVENT_ID" in _V032
    assert "m2.REF_ID = e.EVENT_ID" not in _V032


def test_v098_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V098):
        sqlglot.parse(statement, dialect="snowflake")
