"""Locks for V131 — atomic manual incident declare (Codex R34).

The manual declare ran two separate INSERTs (INCIDENTS, then INCIDENT_MEMBERS); a mid-failure
left a titled, member-less incident. SP_INCIDENT_DECLARE does both in one transaction, reproducing
the app's family-already-open guard + conditional entity filter server-side. The app now runs one
CALL instead of the two-statement loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V131__incident_declare_atomic.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v131_guarded_and_ordered():
    assert "EXCEPTION (-20131" in _MIG and "IF (v < 130) THEN" in _MIG
    assert "SELECT 131 AS VERSION" in _MIG and "WHERE VERSION = 131)" in _MIG


def test_v131_declare_is_one_transaction():
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE" in _MIG
    assert "BEGIN TRANSACTION;" in _MIG and "COMMIT;" in _MIG and "ROLLBACK;" in _MIG
    # exactly one incident insert + the member insert, both inside the txn
    assert _MIG.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS\n") == 1
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS" in _MIG


def test_v131_reproduces_the_app_guards_without_dynamic_sql():
    # the family-already-open guard (same predicate as the app pre-check)
    assert "i.STATUS IN ('OPEN', 'MITIGATED')" in _MIG
    # the conditional entity filter is STATIC (a bound boolean), not dynamic SQL
    assert "apply_entity" in _MIG and "NOT :apply_entity" in _MIG
    assert "EXECUTE IMMEDIATE" not in _MIG.split("$$", 2)[-1]  # no dynamic SQL in the proc body
    assert "\\" not in _MIG
    # the "only link members if the incident was created" guard survives
    assert "WHERE i2.INCIDENT_ID = :inc_id" in _MIG


def test_v131_app_runs_one_atomic_call_not_two_inserts():
    cr = _read("app/ui/pages/control_room.py")
    assert "_incident_declare_call_sql" in cr and "SP_INCIDENT_DECLARE" in cr
    assert "for _stmt in _dec:" not in cr            # the two-statement loop is gone
    # the family-open pre-check stays (execute_statement can't see the proc's NOOP return)
    assert "_incident_family_open_check_sql" in cr


def test_validate_and_docs_track_v131():
    val = _read("snowflake/validate.sql")
    assert "V001..V132 applied" in val and "VERSION BETWEEN 1 AND 132) = 132" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V131__incident_declare_atomic.sql" in _read(rel)
    assert "SP_INCIDENT_DECLARE" in _read("snowflake/teardown.sql")


def test_v131_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 131 in _EXPECTED_MIGRATIONS


def test_v131_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
