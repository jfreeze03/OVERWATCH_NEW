"""Next-Fifty #23 (v4.588.0): every SiS viewer runs with the app OWNER's rights, so the in-app operator
allowlist is the authorization boundary. The query executors now re-check it themselves for owner-
privileged statements (the ALTER levers + query cancel) instead of trusting every call site to remember
is_operator().

v4.610.0 (owner decision 2026-10-05) widened the privileged set: every OVERWATCH-table DML / CALL needs the
entitlement too (action-proc CALLs included), EXCEPT the viewer's own self-service rows (USER_PREFS,
USER_WATCHLIST, APP_USAGE, APP_QUERY_TELEMETRY), which never consult it. tests/test_role_access.py covers the
exact-target matching and the role-sourced write-time re-check."""

from __future__ import annotations

import contextlib

import pytest
import streamlit as st

import app.core.query as q
import app.core.session as session_mod


class _Stmt:
    def __init__(self, log: list, sql: str) -> None:
        self.log, self.sql = log, sql

    def collect(self):
        self.log.append(self.sql)
        return [("OK: 1 event(s) ACK",)]

    def collect_nowait(self):
        self.log.append(self.sql)


class _Session:
    def __init__(self) -> None:
        self.log: list[str] = []

    def sql(self, sql: str) -> _Stmt:
        return _Stmt(self.log, sql)


@pytest.fixture
def wired(monkeypatch):
    st.session_state.clear()
    sess, errors = _Session(), []
    monkeypatch.setattr(q, "get_session", lambda: sess)
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q.st, "spinner", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(q, "record_error",
                        lambda page, exc, context="": errors.append((page, exc, context)) or "ref")
    yield sess, errors
    st.session_state.clear()


def test_privileged_prefixes_are_exactly_the_alter_levers():
    expected = {"ALTER WAREHOUSE ", "ALTER PIPE ", "ALTER TASK ", "ALTER USER ", "ALTER ACCOUNT SET "}
    assert set(q._PRIVILEGED_PREFIXES) == {p for p in q._WRITE_PREFIXES if p.startswith("ALTER ")} == expected
    assert not any("DBA_MAINT_DB.OVERWATCH." in p for p in q._PRIVILEGED_PREFIXES)


@pytest.mark.parametrize("stmt", [
    "ALTER WAREHOUSE WH_X SUSPEND;",
    "ALTER PIPE A.B.C SET PIPE_EXECUTION_PAUSED = TRUE;",
    "ALTER TASK A.B.C SUSPEND;",
    "ALTER USER U SET DISABLED = TRUE;",
    "ALTER ACCOUNT SET STATEMENT_TIMEOUT_IN_SECONDS = 7200;",
    "  alter warehouse wh_x resume if suspended",
])
def test_non_operator_privileged_statement_refused(wired, monkeypatch, stmt):
    sess, errors = wired
    monkeypatch.setattr(session_mod, "is_operator", lambda: False)
    ok, msg = q.execute_statement(stmt, page="Operations")
    assert ok is False and "operator entitlement required" in msg
    assert sess.log == []                                          # never reached Snowflake
    assert len(errors) == 1 and isinstance(errors[0][1], PermissionError)
    assert "execute_statement refused" in errors[0][2]
    assert "_ow_refresh_salt" not in st.session_state              # no cache bump on a refusal


def test_non_operator_async_privileged_refused(wired, monkeypatch):
    sess, _ = wired
    monkeypatch.setattr(session_mod, "is_operator", lambda: False)
    assert q.execute_statement_async("ALTER USER U SET DISABLED = TRUE;", page="x") is False
    assert sess.log == []


def test_non_operator_cancel_refused_after_the_id_check(wired, monkeypatch):
    sess, _ = wired
    monkeypatch.setattr(session_mod, "is_operator", lambda: False)
    ok, msg = q.execute_cancel_query("01b2c3d4-0000-1111-2222-333344445555", page="Operations")
    assert ok is False and "operator entitlement required" in msg and sess.log == []
    ok, msg = q.execute_cancel_query("bad id; drop", page="Operations")
    assert ok is False and msg.startswith("Invalid query id")       # the id check still runs first


def test_operator_privileged_statement_runs(wired, monkeypatch):
    sess, _ = wired
    monkeypatch.setattr(session_mod, "is_operator", lambda: True)
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND;", page="Operations") == (True, "Statement executed.")
    assert sess.log == ["ALTER WAREHOUSE WH_X SUSPEND;"]
    assert "_ow_refresh_salt" in st.session_state


def test_self_service_writes_never_consult_entitlement(wired, monkeypatch):
    calls = {"n": 0}

    def _counting() -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(session_mod, "is_operator", _counting)
    ok, _ = q.execute_statement("INSERT INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS (USER_NAME, PREF_KEY, PREF_VALUE) "
                                "SELECT 'U','K','V'", page="x")
    assert ok is True
    assert q.execute_statement_async("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (X) SELECT 1", page="x") is True
    assert q.execute_statement_async("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY (X) SELECT 1",
                                     page="Telemetry") is True
    ok, _ = q.execute_statement("DELETE FROM DBA_MAINT_DB.OVERWATCH.USER_WATCHLIST WHERE 1=0", page="x")
    assert ok is True
    assert calls["n"] == 0


def test_non_operator_action_proc_call_is_refused(wired, monkeypatch):
    # v4.610.0 flipped this: an action-proc CALL used to run for any viewer (UI gating only). It now needs the
    # entitlement like every other non-self-service OVERWATCH write, and the refusal never reaches Snowflake.
    sess, errors = wired
    calls = {"n": 0}

    def _counting() -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(session_mod, "is_operator", _counting)
    ok, msg = q.execute_action("CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1','ACK','','','U','k1')",
                               [], page="Alerts")
    assert ok is False and "operator entitlement required" in msg
    assert calls["n"] == 1 and sess.log == []
    assert len(errors) == 1 and "execute_action refused" in errors[0][2]


def test_operator_action_proc_call_runs(wired, monkeypatch):
    sess, _ = wired
    monkeypatch.setattr(session_mod, "is_operator", lambda: True)
    ok, _ = q.execute_action("CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1','ACK','','','U','k1')",
                             [], page="Alerts")
    assert ok is True and len(sess.log) == 1


def test_entitlement_fails_closed_when_the_check_raises(wired, monkeypatch):
    def _boom() -> bool:
        raise RuntimeError("identity probe failed")

    monkeypatch.setattr(session_mod, "is_operator", _boom)
    assert q.execute_statement("ALTER TASK A.B.C SUSPEND;", page="x")[0] is False


def test_allow_list_refusal_precedes_entitlement(wired, monkeypatch):
    monkeypatch.setattr(session_mod, "is_operator", lambda: False)
    ok, msg = q.execute_statement("DROP TABLE DBA_MAINT_DB.OVERWATCH.SETTINGS;", page="x")
    assert ok is False and "allow-list" in msg and "entitlement" not in msg
