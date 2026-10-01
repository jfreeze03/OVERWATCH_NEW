"""Bug-hunt cluster c09 (overview / brief / admin / main / core): behavioural locks for the confirmed findings.

Each test drives the real code path with fakes (no Snowflake session; tests/conftest.py refuses one) and fails
on the pre-fix code.
"""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult

# ------------------------------------------------------------------ R1-002: deep link vs saved prefs ----

_PREFS = pd.DataFrame({
    "PREF_KEY": ["PRESENT_MODE", "DENSITY", "DISPLAY_TZ", "DEFAULT_VIEW"],
    "PREF_VALUE": ["audit", "compact", "America/New_York", '{"page": "Overview"}'],
})


def _landing_script():
    import streamlit as st

    import app.main as m
    from app.core.state import remember_page

    m._apply_default_landing()
    # _sidebar's remember_page runs on EVERY run, after the landing hook: the app writes its own ?page=.
    remember_page(st.session_state.get("_ow_page") or "Brief")


def _prefs_run(monkeypatch, outcomes: list[bool]) -> list[int]:
    """Patch app.main.run: each USER_PREFS read pops the next outcome (True = the prefs frame, False = a
    transient failure). Returns the read log."""
    import app.main as m

    reads: list[int] = []

    def _run(*_a, **_k):
        reads.append(1)
        ok = outcomes.pop(0) if outcomes else True
        return (QueryResult(df=_PREFS.copy(), ok=True, source="USER_PREFS") if ok
                else QueryResult(ok=False, error="transient", error_kind="timeout"))

    monkeypatch.setattr(m, "run", _run)
    return reads


def test_deep_link_arrival_still_hydrates_display_prefs(monkeypatch):
    """A session opened on ?page=alerts (a shared link or a reload) keeps the deep link but still hydrates the
    saved presentation mode / density / timezone -- it used to skip the USER_PREFS read entirely."""
    reads = _prefs_run(monkeypatch, [True])
    at = AppTest.from_function(_landing_script, default_timeout=30)
    at.query_params["page"] = "alerts"
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception
    assert reads == [1]
    assert at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_present_mode_toggle"] is True
    assert at.session_state["_ow_density"] == "compact"
    assert at.session_state["_ow_display_tz"] == "America/New_York"
    # the deep link wins over DEFAULT_VIEW: no saved-view navigation was applied
    assert "_ow_page" not in at.session_state


def test_transient_prefs_failure_retries_despite_the_apps_own_page_param(monkeypatch):
    """r10 #1's bounded retry: run 1's USER_PREFS read fails, the app writes ?page=brief itself, and run 2
    must still retry (prefs AND the saved landing) instead of reading its own write as a deep link."""
    reads = _prefs_run(monkeypatch, [False, True])
    at = AppTest.from_function(_landing_script, default_timeout=30)
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception
    assert reads == [1] and at.session_state["_ow_default_attempts"] == 1
    assert at.query_params.get("page") in ("brief", ["brief"])          # the app's own write
    at.run()
    assert not at.exception
    assert reads == [1, 1]
    assert at.session_state["_ow_default_applied"] is True
    assert at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_page"] == "Overview"                  # the saved landing applied late


def test_identity_late_run_still_reads_prefs(monkeypatch):
    """r11 #3: no identity on run 1 (no read, no attempt spent); identity on run 2 must read the prefs even
    though run 1 already wrote ?page=."""
    reads = _prefs_run(monkeypatch, [True])
    at = AppTest.from_function(_landing_script, default_timeout=30)
    at.run()
    assert not at.exception and reads == []
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception
    assert reads == [1] and at.session_state["_ow_present_mode"] == "audit"


# ------------------------------------------------------------------ R1-003: WH jump carries its company ----

@pytest.mark.parametrize(("pick", "company"), [
    ("WH · WH_TRXS_LOAD", "Trexis"),          # offered under the default ALFA scope before 'Load all'
    ("WH · WH_ALFA_ADMIN", "ALFA"),
    ("WH · SOME_OTHER_WH", "ALL"),            # name-unclassified: never a company it may not belong to
])
def test_warehouse_jump_carries_a_company_that_cannot_contradict_it(monkeypatch, pick, company):
    import app.main as m

    navs: list[tuple] = []
    monkeypatch.setattr(m, "request_navigation", lambda *a, **k: navs.append((a, k)))
    m._dispatch_jump(pick, ("Operations",))
    (args, _kw), = navs
    assert args[:2] == ("Operations", "Queries")
    assert args[2] == {"company": company, "warehouse_contains": pick.split(" · ", 1)[1]}


# ------------------------------------------------------------------ R1-004: cancel reports Snowflake's answer ----

_QID = "01b2c3d4-0000-1111-2222-333344445555"


class _CancelStmt:
    def __init__(self, log: list, sql: str, reply) -> None:
        self.log, self.sql, self.reply = log, sql, reply

    def collect(self, **_kw):
        self.log.append(self.sql)
        return self.reply


class _CancelSession:
    def __init__(self, reply) -> None:
        self.log: list[str] = []
        self.reply = reply

    def sql(self, sql: str) -> _CancelStmt:
        return _CancelStmt(self.log, sql, self.reply)


def _wire_cancel(monkeypatch, reply):
    import contextlib

    import streamlit as st

    import app.core.query as q
    import app.core.session as session_mod

    st.session_state.clear()
    sess, errors = _CancelSession(reply), []
    monkeypatch.setattr(q, "get_session", lambda: sess)
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q.st, "spinner", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(q, "record_error", lambda page, exc, context="": errors.append(exc) or "ref")
    monkeypatch.setattr(session_mod, "is_operator", lambda: True)
    return q, sess, errors


@pytest.mark.parametrize("reply", [
    [("Identified SQL statement is not currently executing.",)],     # finished after the cached list was read
    [],                                                                # no confirmation at all
    [(None,)],
])
def test_cancel_that_snowflake_did_not_make_is_not_a_success(monkeypatch, reply):
    import streamlit as st

    q, sess, errors = _wire_cancel(monkeypatch, reply)
    ok, msg = q.execute_cancel_query(_QID, page="Operations")
    assert ok is False                                 # the caller's REMEDIATION_LOG row reads FAILED
    assert sess.log == [f"SELECT SYSTEM$CANCEL_QUERY('{_QID}')"]
    if reply and reply[0][0]:
        assert msg == "Snowflake: Identified SQL statement is not currently executing."
    else:
        assert "no confirmation" in msg
    assert not errors and "_ow_refresh_salt" not in st.session_state
    st.session_state.clear()


def test_confirmed_cancel_is_a_success_and_refreshes_the_running_list(monkeypatch):
    import streamlit as st

    q, _sess, _errors = _wire_cancel(monkeypatch, [(f"query [{_QID}] terminated.",)])
    ok, msg = q.execute_cancel_query(_QID, page="Operations")
    assert ok is True and msg == f"Snowflake: query [{_QID}] terminated."
    assert "_ow_refresh_salt" in st.session_state      # the cached running-queries list re-reads
    st.session_state.clear()
