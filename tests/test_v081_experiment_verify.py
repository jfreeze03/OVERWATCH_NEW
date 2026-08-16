"""V081 locks: SP_VERIFY_EXPERIMENT unifies experiment verify + savings ledger + action close.

Decision Studio review, finding #5. One transactional procedure records an
experiment's verified verdict, upserts SAVINGS_LEDGER keyed on ACTION_ID
(FINDING_TYPE='EXPERIMENT'), and closes the source ACTION_QUEUE row, so the two
"verified savings" totals can no longer diverge. Procedure-only; the app rewire
(update_experiment_sql -> CALL) ships with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.logic.workbench import update_experiment_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V081 = (_MIG / "V081__unified_experiment_verify.sql").read_text(encoding="utf-8")


# --- the migration file ----------------------------------------------------

def test_v081_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v081.py")],
        env={**os.environ, "V081_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V081, (
        "V081 drifted from its forward-generation — edit outputs/gen_v081.py, "
        "not the .sql, then regenerate."
    )


def test_v081_is_one_guarded_procedure():
    assert "EXCEPTION (-20081" in _V081 and "IF (v < 80) THEN" in _V081
    assert "SELECT 81 AS VERSION" in _V081 and "WHERE VERSION = 81)" in _V081
    assert _V081.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE VIEW" not in _V081
    # no schema change / no reload — a procedure-only migration
    assert "CREATE TABLE" not in _V081 and "ALTER TABLE" not in _V081
    assert "CREATE TASK" not in _V081 and "RESOURCE MONITOR" not in _V081


def test_v081_settles_both_directions_in_one_transaction():
    # exactly one atomic transaction with rollback on any error
    assert _V081.count("BEGIN TRANSACTION;") == 1 and _V081.count("COMMIT;") == 1
    assert "EXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;" in _V081
    # only terminal outcomes settle
    assert "new_status NOT IN ('VERIFIED', 'REJECTED', 'ROLLED_BACK')" in _V081
    # always: the experiment verdict
    assert "UPDATE DBA_MAINT_DB.OVERWATCH.OPTIMIZATION_EXPERIMENTS" in _V081
    # VERIFIED leg: ledger upsert keyed on ACTION_ID (discriminated) + close the action
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER" in _V081
    assert "l.ACTION_ID = s.ACTION_ID AND l.FINDING_TYPE = 'EXPERIMENT'" in _V081
    assert "STATUS = 'DONE'" in _V081
    # REJECTED/ROLLED_BACK leg: reverse a PRIOR booking (gated) + reopen the action
    assert "IF (had_booking > 0) THEN" in _V081
    assert "STATE = 'REJECTED'" in _V081
    assert "STATUS = 'OPEN'" in _V081 and "COMPLETED_AT = NULL" in _V081
    # both legs audited; both action writes present (close + reopen)
    assert _V081.count("UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE") == 2
    assert _V081.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY") == 2


def test_v081_ledger_and_action_legs_gated_on_action_id():
    # a standalone experiment (no ACTION_ID) still records its verdict but has no
    # ledger row to key or action to close
    assert "IF (aid IS NOT NULL AND TRIM(aid) <> '') THEN" in _V081


def test_v081_is_idempotent_by_request_key():
    assert "DUPLICATE: request already applied" in _V081
    assert "WHERE REQUEST_KEY = :P_REQUEST_KEY" in _V081


def test_v081_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V081):
        sqlglot.parse(statement, dialect="snowflake")


# --- the app rewire --------------------------------------------------------

def test_verify_transition_calls_the_proc():
    sql = update_experiment_sql("exp-1", status="VERIFIED", result="held up over 14d",
                                verified_usd=1234.5, actor="jdees")
    assert sql.startswith("CALL")
    assert "SP_VERIFY_EXPERIMENT(" in sql
    assert "'exp-1', 'VERIFIED', 1234.5," in sql    # eid, status, usd in order
    assert "'held up over 14d'" in sql and "'jdees'" in sql
    assert "UPDATE" not in sql   # a settle never emits a bare UPDATE


def test_reject_and_rollback_also_call_the_proc_with_status():
    for state in ("REJECTED", "ROLLED_BACK"):
        sql = update_experiment_sql("exp-1", status=state, result="regressed",
                                    verified_usd=None, actor="jdees")
        assert sql.startswith("CALL") and "SP_VERIFY_EXPERIMENT(" in sql
        assert f"'exp-1', '{state}', NULL," in sql   # verdict cleared for non-verify
        assert "UPDATE" not in sql


def test_verify_with_no_measured_saving_passes_null():
    sql = update_experiment_sql("exp-1", status="VERIFIED", result="", verified_usd=None,
                                actor="jdees")
    assert sql.startswith("CALL") and "SP_VERIFY_EXPERIMENT(" in sql
    assert "'exp-1', 'VERIFIED', NULL," in sql       # verified_usd -> NULL, not 0


def test_in_flight_transitions_stay_a_plain_update():
    for state in ("PLANNED", "RUNNING", "OBSERVING"):
        sql = update_experiment_sql("exp-1", status=state, result="note",
                                    verified_usd=None, actor="jdees")
        assert sql.startswith("UPDATE DBA_MAINT_DB.OVERWATCH.OPTIMIZATION_EXPERIMENTS")
        assert "SP_VERIFY_EXPERIMENT" not in sql
        assert f"'{state}'" in sql


def test_update_experiment_requires_id_and_valid_status():
    with pytest.raises(ValueError):
        update_experiment_sql("", status="VERIFIED", result="", verified_usd=1.0, actor="x")
    with pytest.raises(ValueError):
        update_experiment_sql("exp-1", status="BOGUS", result="", verified_usd=1.0, actor="x")


def test_verify_usd_is_clamped_non_negative():
    sql = update_experiment_sql("exp-1", status="VERIFIED", result="", verified_usd=-5.0,
                                actor="x")
    assert "-5.0" not in sql                    # the negative value never reaches the proc
    assert "'exp-1', 'VERIFIED', 0.0," in sql   # clamped to 0.0 as the USD CALL arg
