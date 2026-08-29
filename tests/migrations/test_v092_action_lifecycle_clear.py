"""V092 locks: SP_ACTION_LIFECYCLE gains explicit CLEAR signals.

V074's proc COALESCE-keeps OWNER and DEFER_UNTIL, so a blank owner / NULL defer is a
keep, never a clear — the Action Center could not un-assign an owner or un-defer an
item (v4.318 dropped those effects from the UI to stay honest). V092 re-derives the
proc from V074 byte-identically plus P_CLEAR_OWNER / P_CLEAR_DEFER, so both are real
savable effects again. Procedure-only; the app rewire ships with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.logic.workbench import action_transition_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V092 = (_MIG / "V092__action_lifecycle_clear_signals.sql").read_text(encoding="utf-8")
_V074 = (_MIG / "V074__operating_workbench_foundation.sql").read_text(encoding="utf-8")


# --- the migration file ----------------------------------------------------

def test_v092_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v092.py")],
        env={**os.environ, "V092_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V092, (
        "V092 drifted from its forward-generation — edit outputs/gen_v092.py, "
        "not the .sql, then regenerate."
    )


def test_v092_is_one_guarded_proc_redefinition():
    assert "EXCEPTION (-20092" in _V092 and "IF (v < 91) THEN" in _V092
    assert "SELECT 92 AS VERSION" in _V092 and "WHERE VERSION = 92)" in _V092
    assert _V092.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE VIEW" not in _V092
    # procedure-only — no schema change, no reload
    assert "CREATE TABLE" not in _V092 and "ALTER TABLE" not in _V092
    assert "CREATE TASK" not in _V092 and "RESOURCE MONITOR" not in _V092
    # the signature changed, so the old 8-arg overload is dropped first
    assert _V092.count("DROP PROCEDURE IF EXISTS") == 1
    assert ("SP_ACTION_LIFECYCLE(VARCHAR, VARCHAR, VARCHAR, DATE, DATE, "
            "VARCHAR, VARCHAR, VARCHAR)") in _V092


def test_v092_adds_only_the_two_clear_edits():
    # edit 1: two new BOOLEAN clear parameters, appended after P_REQUEST_KEY
    assert "P_REQUEST_KEY VARCHAR,\n    P_CLEAR_OWNER BOOLEAN,\n    P_CLEAR_DEFER BOOLEAN\n)" in _V092
    # edit 2: an explicit clear wins over the old COALESCE-keep, which survives verbatim
    #         inside the IFF false branch (nowhere else)
    assert ("OWNER = IFF(:P_CLEAR_OWNER, NULL, "
            "COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER))") in _V092
    assert ("DEFER_UNTIL = IFF(:P_CLEAR_DEFER, NULL, "
            "COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL))") in _V092
    assert _V092.count("COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER)") == 1
    assert _V092.count("COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL)") == 1
    # DUE_DATE keeps its plain COALESCE-keep (only OWNER + DEFER gained a clear)
    assert "DUE_DATE = COALESCE(:P_DUE_DATE, DUE_DATE)," in _V092


def test_v092_body_is_v074_byte_identical_apart_from_the_edits():
    # everything except the two edits is carried straight from V074
    for anchor in (
        "seen NUMBER DEFAULT 0;",
        "next_status := COALESCE(NULLIF(UPPER(TRIM(P_STATUS)), ''), old_status);",
        "BEGIN TRANSACTION;",
        "RESOLUTION_NOTE = IFF(:next_status IN ('DONE', 'DROPPED')",
        "COMPLETED_AT = IFF(:next_status IN ('DONE', 'DROPPED'),",
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY",
        "IFF(:next_status = :old_status, 'COMMENT', 'TRANSITION'),",
    ):
        assert anchor in _V074 and anchor in _V092, anchor


def test_v092_keeps_one_transaction_and_idempotency():
    assert _V092.count("BEGIN TRANSACTION;") == 1 and _V092.count("COMMIT;") == 1
    assert "EXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;" in _V092
    assert "DUPLICATE: request already applied" in _V092
    assert "WHERE REQUEST_KEY = :P_REQUEST_KEY" in _V092


def test_v092_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V092):
        sqlglot.parse(statement, dialect="snowflake")


# --- the app rewire --------------------------------------------------------

def test_transition_defaults_pass_both_clear_flags_false():
    sql = action_transition_sql("a1", status="IN_PROGRESS", owner="Joe",
                                request_key="rk1")
    assert "SP_ACTION_LIFECYCLE(" in sql
    assert sql.rstrip().endswith("FALSE, FALSE)")   # ordinary edit = COALESCE-keep


def test_transition_clear_owner_and_defer_pass_true():
    sql = action_transition_sql("a1", status="OPEN", owner="", defer_until=None,
                                request_key="rk2", clear_owner=True, clear_defer=True)
    assert sql.rstrip().endswith("TRUE, TRUE)")
    assert sql.count(",") == 9    # 10 positional args


def test_transition_clear_flags_are_independent():
    only_owner = action_transition_sql("a1", request_key="k", clear_owner=True)
    assert only_owner.rstrip().endswith("TRUE, FALSE)")
    only_defer = action_transition_sql("a1", request_key="k", clear_defer=True)
    assert only_defer.rstrip().endswith("FALSE, TRUE)")


def test_transition_still_validates_id_and_status():
    with pytest.raises(ValueError):
        action_transition_sql("", status="OPEN")
    with pytest.raises(ValueError):
        action_transition_sql("a1", status="SURPRISE")
