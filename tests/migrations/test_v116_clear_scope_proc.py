"""V116: atomic, idempotent scope-based alert clear (SP_ALERT_CLEAR_SCOPE).

The clear-queue used to run its ALERT_AUDIT insert and ALERT_EVENTS status change as two
separate auto-committed statements; a mid-op failure + retry could duplicate audit rows.
V116 does both in one transaction over a STATUS + company scope, guarded by an
OW_ACTION_INTENTS idempotency key (mirrors SP_ALERT_LIFECYCLE / SP_ALERT_SNOOZE). The app
calls it via execute_action() with the two-statement builder as the pre-V116 legacy fallback.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v116_creates_atomic_idempotent_clear_proc() -> None:
    mig = _read("snowflake/migrations/V116__alert_clear_scope_proc.sql")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_CLEAR_SCOPE" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig   # new proc, no schema change
    # idempotency: DUPLICATE short-circuit + records the intent key on success
    assert "OW_ACTION_INTENTS WHERE IDEM_KEY = :P_IDEM_KEY" in mig
    assert "RETURN 'DUPLICATE: '" in mig
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS" in mig
    # atomic: audit + status change inside ONE transaction, rollback on failure
    assert "BEGIN TRANSACTION;" in mig and mig.count("COMMIT;") == 1
    assert "ROLLBACK;" in mig and "WHEN OTHER THEN" in mig
    # audit row carries the actor (never lets CURRENT_USER() default to the owner)
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)" in mig
    # ACK touches only OPEN; RESOLVE covers OPEN+ACK; untagged kind leaves RESOLUTION_KIND untouched
    assert "SET STATUS = 'ACK'" in mig and "SET STATUS = 'RESOLVED'" in mig
    assert "RESOLUTION_KIND = IFF(:v_kind <> '', :v_kind, RESOLUTION_KIND)" in mig
    # company scope: ALL clears everything, else that company + account-level
    assert "(:v_all OR COMPANY = :P_COMPANY OR UPPER(COMPANY) = 'ALL')" in mig
    # ordered guard + version stamp
    assert "EXCEPTION (-20116" in mig and "IF (v < 115) THEN" in mig
    assert "SELECT 116 AS VERSION" in mig and "WHERE VERSION = 116)" in mig


def test_v116_app_routes_clear_queue_through_the_atomic_proc() -> None:
    src = _read("app/ui/pages/alerts.py")
    # the clear-queue now calls the proc via execute_action (atomic, DUPLICATE-idempotent)
    assert "SP_ALERT_CLEAR_SCOPE" in src
    assert 'idempotency_key(f"ALERT_CLEAR_{_c_verb}"' in src
    assert "execute_action(" in src and "_c_call" in src
    # the old two-autocommit loop is gone...
    assert "for _c_stmt in _clear_open_queue_stmts" not in src
    # ...but the two-statement builder survives as the pre-V116 legacy fallback
    assert "_clear_open_queue_stmts(company, _c_verb, _c_note, _c_kind)" in src


def test_v116_registered() -> None:
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 116 in _EXPECTED_MIGRATIONS
    # the moving validate tip is asserted by the 7 designated tip tests (bumped every
    # migration), not here -- asserting it would break at V117. Teeth-floor is stable.
    assert "BETWEEN 1 AND 88" in _read("snowflake/validate.sql")
