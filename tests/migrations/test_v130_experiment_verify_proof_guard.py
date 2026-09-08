"""Locks for V130 — proc-level proof enforcement in SP_VERIFY_EXPERIMENT (Codex R32).

The Decision Studio UI gated a VERIFIED settlement on a result note, positive USD and a closed
observation window, but the settlement proc did not — a hand-called VERIFIED with an empty note
and $0 could book a no-evidence SAVINGS_LEDGER row. V130 re-derives the proc from V081 with the
proof guards, returning BLOCKED before the transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V130__experiment_verify_proof_guard.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v130_guarded_and_ordered():
    assert "EXCEPTION (-20130" in _MIG and "IF (v < 129) THEN" in _MIG
    assert "SELECT 130 AS VERSION" in _MIG and "WHERE VERSION = 130)" in _MIG


def test_v130_verified_settlement_requires_proof():
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_EXPERIMENT" in _MIG
    assert "BLOCKED: VERIFIED needs a result note and positive verified savings" in _MIG
    assert "BLOCKED: the observation window has not closed yet" in _MIG
    assert "MAX(TO_DATE(OBSERVATION_END))" in _MIG and "obs_end DATE;" in _MIG
    # the guards fire BEFORE the transaction opens (reject, never a half-applied settle)
    pre_txn = _MIG.split("BEGIN TRANSACTION")[0]
    assert "BLOCKED: VERIFIED needs a result note" in pre_txn
    assert "BLOCKED: the observation window has not closed yet" in pre_txn


def test_v130_is_proc_only_no_schema_change():
    assert "CREATE TABLE " not in _MIG and "ALTER TABLE " not in _MIG
    assert "CREATE OR REPLACE VIEW" not in _MIG and "CREATE TASK" not in _MIG
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    # nothing dropped from the re-derived proc
    assert "SAVINGS_LEDGER" in _MIG and "ACTION_QUEUE" in _MIG and "OPTIMIZATION_EXPERIMENTS" in _MIG


def test_validate_and_docs_track_v130():
    val = _read("snowflake/validate.sql")
    assert "V001..V132 applied" in val and "VERSION BETWEEN 1 AND 132) = 132" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V130__experiment_verify_proof_guard.sql" in _read(rel)


def test_v130_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 130 in _EXPECTED_MIGRATIONS


def test_v130_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
