"""Locks for V051 action layer — scoped slice (Tranche C / r28, 2026-07-27).

Ships ONE wired, verified path: SP_ALERT_LIFECYCLE (atomic set-based alert
ack/resolve) + OW_ACTION_INTENTS idempotency. The remediation/verify/action
procs were deferred after adversarial review found an owner-privileged SQL
injection in the unwired draft — these locks pin that they did NOT ship, so a
future round adds them deliberately, hardened.
"""
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V51 = (_ROOT / "snowflake" / "migrations" / "V051__action_layer.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v051_guard_version_house_rules():
    assert "EXCEPTION (-20051" in _V51 and "RAISE not_ready;" in _V51
    assert "IF (v < 50) THEN" in _V51 and "SELECT 51 AS VERSION" in _V51


def test_v051_ships_only_the_wired_proc():
    # Exactly one proc + one table this round.
    assert _V51.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE" in _V51
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS" in _V51


def test_v051_deferred_procs_are_absent():
    # The unwired, injection-bearing draft procs must NOT ship. A future round
    # adds them wired + hardened; this lock keeps them out until then.
    for proc in ("SP_EXECUTE_REMEDIATION", "SP_VERIFY_SAVINGS", "SP_ACTION_UPDATE"):
        assert proc not in _V51, f"{proc} shipped — it was deferred after security review"
    # ...and the injection surface (concatenated PROOF_SQL / EXECUTE IMMEDIATE
    # of a stored proof) is nowhere in the shipped migration.
    assert "EXECUTE IMMEDIATE :v_proof" not in _V51
    assert "EXECUTE IMMEDIATE :P_STMT" not in _V51


def test_v051_proc_follows_house_shape_and_verdicts():
    assert "LANGUAGE SQL" in _V51 and "EXECUTE AS OWNER" in _V51 and "RETURNS VARCHAR" in _V51
    assert "LANGUAGE JAVASCRIPT" not in _V51 and "EXECUTE AS CALLER" not in _V51
    for token in ("'DUPLICATE: '", "'BLOCKED:", "'OK: '"):
        assert token in _V51, token


def test_v051_transactional_and_idempotent():
    assert _V51.count("BEGIN TRANSACTION;") == 1
    assert "ROLLBACK;\n        RAISE;" in _V51
    assert "RETURN 'DUPLICATE: ' || :P_IDEM_KEY;" in _V51
    # empty/NULL key is refused, not slipped past the EXISTS check
    assert "RETURN 'BLOCKED: missing idempotency key';" in _V51


def test_v051_audit_is_pre_state_filtered_not_post_update():
    # The fixed audit: INSERT ... SELECT filters the PRE-state (OPEN / OPEN,ACK)
    # BEFORE the UPDATE, so audit rows match exactly the transitioned events —
    # the draft's post-update STATUS='ACK'/'RESOLVED' re-audited stale rows.
    assert "SELECT EVENT_ID, 'ACK'" in _V51 and "AND STATUS = 'OPEN'" in _V51
    assert "SELECT EVENT_ID, 'RESOLVE'" in _V51 and "AND STATUS IN ('OPEN', 'ACK')" in _V51
    # the draft's buggy audit selection is gone
    assert "AND STATUS = 'ACK';" not in _V51
    assert "AND STATUS = 'RESOLVED';" not in _V51


def test_v051_hardening_details():
    assert _V51.count("TRIM(VALUE)::VARCHAR") == 4          # whitespace-tolerant split
    assert "CREATED_AT TIMESTAMP_NTZ" in _V51               # AT reserved keyword renamed
    assert "\n    AT " not in _V51


def test_v051_app_wiring_is_proc_first_with_fallback():
    q = _read("app/core/query.py")
    assert "def execute_action(call_sql: str, fallback: list[str]" in q
    assert "_legacy_action(fallback, page=page)" in q
    assert 'verdict.startswith("BLOCKED")' in q
    ident = _read("app/core/identity.py")
    assert "def idempotency_key(" in ident and "account_now()" in ident
    alerts = _read("app/ui/pages/alerts.py")
    assert "execute_action(call, legacy, page=_PAGE)" in alerts        # single
    assert "execute_action(call, [upd, aud], page=_PAGE)" in alerts    # bulk


def test_v051_teardown_covers_the_shipped_objects_only():
    td = _read("snowflake/teardown.sql").upper()
    assert "SP_ALERT_LIFECYCLE" in td and "OW_ACTION_INTENTS" in td
    # The remediation/verify/action-queue procs were all dropped after review
    # (see V053 / test_v053a); none should appear in teardown.
    for gone in ("SP_EXECUTE_REMEDIATION", "SP_VERIFY_SAVINGS", "SP_ACTION_UPDATE"):
        assert gone not in td, f"teardown drops a never-created proc: {gone}"


def test_v051_has_no_generator():
    # The slim slice re-derives nothing, so the derivation law does not apply
    # and there is no gen_v051.py to drift from.
    assert not (_ROOT / "outputs" / "gen_v051.py").exists()


def test_v051_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V51):
        sqlglot.parse(stmt, dialect="snowflake")
