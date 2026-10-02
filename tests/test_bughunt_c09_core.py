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


def _prefs_run(monkeypatch, outcomes: list[bool], frame: pd.DataFrame = _PREFS) -> list[int]:
    """Patch app.main.run: each USER_PREFS read pops the next outcome (True = the prefs frame, False = a
    transient failure). Returns the read log."""
    import app.main as m

    reads: list[int] = []

    def _run(*_a, **_k):
        reads.append(1)
        ok = outcomes.pop(0) if outcomes else True
        return (QueryResult(df=frame.copy(), ok=True, source="USER_PREFS") if ok
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
    must still retry (prefs AND the saved landing) instead of reading its own write as a deep link. The hook
    alone here: no page is noted as rendered, so the landing may still apply (in main()'s order it does not --
    see the R1-002 follow-up locks below)."""
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


# R1-002 follow-up: now that the retry runs, a LATE DEFAULT_VIEW must never navigate once a page has rendered.

_PREFS_VIEW = pd.DataFrame({
    "PREF_KEY": ["PRESENT_MODE", "DEFAULT_VIEW"],
    "PREF_VALUE": ["audit", '{"page": "Overview", "filters": {"company": "Trexis"}}'],
})


def _sidebar_landing_script():
    """main()'s order around the landing hook: init_filters, _apply_default_landing, then _sidebar resolves the
    page into _ow_page, notes what this run rendered, and writes ?page= (remember_page)."""
    import streamlit as st

    import app.main as m
    from app.core.state import init_filters, remember_page

    init_filters()
    m._apply_default_landing()
    page = st.session_state.get("_ow_page") or "Brief"
    st.session_state["_ow_page"] = page
    getattr(m, "_note_landing_rendered", lambda: None)()   # pre-fix: absent, so the behaviour asserts fail
    remember_page(page)


def _first_run_fails(monkeypatch, outcomes: list[bool]):
    reads = _prefs_run(monkeypatch, outcomes, frame=_PREFS_VIEW)
    at = AppTest.from_function(_sidebar_landing_script, default_timeout=30)
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception
    assert reads == [1] and at.session_state["_ow_page"] == "Brief"
    assert at.session_state["flt_company"] == "ALFA"
    return at, reads


def test_late_prefs_retry_never_overrides_a_nav_click(monkeypatch):
    """The reviewer's probe: run 1's USER_PREFS read fails, the viewer clicks to Alerts (_nav_pick) before the
    retry, and run 2's read succeeds -- the presentation mode hydrates, the page and the scope stay theirs."""
    at, reads = _first_run_fails(monkeypatch, [False, True])
    at.session_state["_ow_page"] = "Alerts"
    at.run()
    assert not at.exception
    assert reads == [1, 1] and at.session_state["_ow_default_applied"] is True
    assert at.session_state["_ow_present_mode"] == "audit"            # display prefs still hydrate late
    assert at.session_state["_ow_page"] == "Alerts"                   # never pulled back to Overview
    assert at.session_state["flt_company"] == "ALFA"                  # nor re-scoped to the view's Trexis


def test_late_prefs_retry_never_overrides_a_scope_change(monkeypatch):
    at, reads = _first_run_fails(monkeypatch, [False, True])
    at.session_state["flt_company"] = "ALL"                           # a scope pick, same page
    at.run()
    assert not at.exception
    assert reads == [1, 1] and at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_page"] == "Brief" and at.session_state["flt_company"] == "ALL"


def test_a_move_is_sticky_across_retries(monkeypatch):
    """Moving away on one retry and back before the next still counts: the late view is not applied."""
    at, reads = _first_run_fails(monkeypatch, [False, False, True])
    at.session_state["_ow_page"] = "Alerts"
    at.run()
    assert not at.exception and reads == [1, 1]
    at.session_state["_ow_page"] = "Brief"
    at.run()
    assert not at.exception and reads == [1, 1, 1]
    assert at.session_state["_ow_page"] == "Brief" and at.session_state["flt_company"] == "ALFA"


def test_first_run_success_still_lands_the_saved_view(monkeypatch):
    """The saved DEFAULT_VIEW (page AND filters) still lands when the session's first USER_PREFS read succeeds."""
    reads = _prefs_run(monkeypatch, [True], frame=_PREFS_VIEW)
    at = AppTest.from_function(_sidebar_landing_script, default_timeout=30)
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception and reads == [1]
    assert at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_page"] == "Overview" and at.session_state["flt_company"] == "Trexis"


def test_late_prefs_retry_after_a_rendered_run_hydrates_display_prefs_only(monkeypatch):
    """c09 R1-002 recheck: once a run has rendered a page, a late successful retry hydrates PRESENT_MODE but
    never applies the DEFAULT_VIEW navigation or filters -- even with no move before the retry run, because
    the rerun carrying the retry IS the viewer's interaction with the page on screen."""
    at, reads = _first_run_fails(monkeypatch, [False, True])
    at.run()
    assert not at.exception and reads == [1, 1]
    assert at.session_state["_ow_default_applied"] is True
    assert at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_present_mode_toggle"] is True
    assert at.session_state["_ow_page"] == "Brief"                    # not the saved view's Overview
    assert at.session_state["flt_company"] == "ALFA"                  # nor its Trexis scope
    assert "_ow_nav_pending" not in at.session_state


def _sidebar_landing_click_script():
    """_sidebar_landing_script plus a Brief-body button whose handler runs AFTER the landing hook, the way a
    page-body action does in main()."""
    import streamlit as st

    import app.main as m
    from app.core.state import init_filters, remember_page

    init_filters()
    m._apply_default_landing()
    page = st.session_state.get("_ow_page") or "Brief"
    st.session_state["_ow_page"] = page
    m._note_landing_rendered()
    remember_page(page)
    if page == "Brief" and st.button("Open alerts", key="brief_open_alerts"):
        st.session_state["_ow_brief_clicked"] = True


def test_late_prefs_retry_never_preempts_an_action_handled_in_the_retry_run(monkeypatch):
    """The recheck's probe: run 1's USER_PREFS read fails; the viewer clicks a Brief button; that rerun's retry
    succeeds BEFORE the Brief body runs. A late DEFAULT_VIEW used to swap the page to Overview, so the click's
    handler never ran. Now the page stays and the click lands."""
    reads = _prefs_run(monkeypatch, [False, True], frame=_PREFS_VIEW)
    at = AppTest.from_function(_sidebar_landing_click_script, default_timeout=30)
    at.session_state["_ow_current_user"] = "JFREEZE"
    at.run()
    assert not at.exception and reads == [1]
    at.button(key="brief_open_alerts").click().run()
    assert not at.exception and reads == [1, 1]
    assert at.session_state["_ow_present_mode"] == "audit"
    assert at.session_state["_ow_page"] == "Brief"
    assert at.session_state["_ow_brief_clicked"] is True


def test_sidebar_notes_the_rendered_landing_before_writing_the_page_param():
    from tests._source import read

    side = read("app/main.py").split("def _sidebar(", 1)[1].split("\ndef ", 1)[0]
    i_page = side.index('st.session_state["_ow_page"] = page')
    assert i_page < side.index("_note_landing_rendered()") < side.index("remember_page(page)")


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


# ------------------------------------------------------------------ R1-005: batch cache hits say so (R12) ----

def _quiet_batch_layer(monkeypatch):
    import streamlit as st

    import app.core.query as q

    st.session_state.clear()
    monkeypatch.setattr(q, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(q, "record_error", lambda *a, **k: "ref")
    return q


def test_run_batch_mixed_member_cache_hit_is_captioned_as_cached(monkeypatch):
    q = _quiet_batch_layer(monkeypatch)
    calls: list[tuple] = []

    def _exec(sqls, *_a, **_k):
        calls.append(sqls)
        return tuple(pd.DataFrame({"X": [1]}) for _ in sqls)

    monkeypatch.setattr(q, "_execute_batch", _exec)
    spec = [{"key": "k", "sql": "SELECT 1 AS X /* c09 R1-005 mixed */", "tier": "hourly", "source": "s"}]
    first = q.run_batch_mixed(spec, page="t")["k"]
    second = q.run_batch_mixed(spec, page="t")["k"]
    assert len(calls) == 1                              # the second answer never left the member cache
    assert first.ok and first.cache_hit is False
    assert second.ok and second.cache_hit is True       # 'served ... cached', never 'fetched <now>'


def test_run_batch_tuple_cache_replay_is_captioned_as_cached(monkeypatch):
    q = _quiet_batch_layer(monkeypatch)
    # A tuple-level st.cache_data hit replays frames WITHOUT running _execute_batch, so _BATCH_MEMBER_MS
    # stays None (the #29 sentinel) -- emulate exactly that.
    monkeypatch.setitem(q._BATCH_FETCHERS, "recent",
                        lambda sqls, *_a, **_k: tuple(pd.DataFrame({"X": [1]}) for _ in sqls))
    out = q.run_batch([{"key": "k", "sql": "SELECT 1 AS X /* c09 R1-005 tuple */", "source": "s"}],
                      page="t", tier="recent")
    assert out["k"].ok and out["k"].cache_hit is True


# ------------------------------------------------------------------ R1-006: the session-expired remedy works ----

@pytest.mark.parametrize("raw", ["390111: Session no longer exists: 12345",
                                 "Authentication token has expired. The user must authenticate again."])
def test_session_expired_hint_names_a_remedy_that_reconnects(raw):
    from app.core.errors import format_snowflake_error

    msg = format_snowflake_error(raw)
    assert "Reload the app in your browser to reconnect" in msg        # SiS: a reload is a fresh instance
    assert "local dev: press 'Refresh data'" in msg                    # off SiS: Refresh now reconnects


_EXPIRED = "390111 (08001): Session no longer exists. New login required to access the service."


@pytest.mark.parametrize(("sis", "raw", "cleared"), [
    (False, _EXPIRED, 1),
    (False, "Authentication token has expired.  The user must authenticate again.", 1),
    (False, "000630 (57014): Statement reached its statement or warehouse timeout of 60 second(s)", 0),
    (True, _EXPIRED, 0),                  # SiS: the platform's session; the message says reload instead
])
def test_refresh_reconnects_off_sis_only_after_a_session_expired_read(monkeypatch, sis, raw, cleared):
    """Off SiS the cached session is process-wide (every local tab's one connection) and an externalbrowser
    reconnect can open an SSO prompt, so 'Refresh data' drops it ONLY after a read failed session-expired --
    the classification format_snowflake_error uses -- and once per such failure; any other Refresh stays a
    salt bump."""
    import streamlit as st

    import app.core.query as q
    import app.core.session as session_mod
    import app.main as m

    st.session_state.clear()
    calls: list[int] = []
    monkeypatch.setattr(session_mod, "is_sis", lambda: sis)
    monkeypatch.setattr(st.cache_resource, "clear", lambda: calls.append(1))
    monkeypatch.setattr(q, "_telemetry", lambda *_a, **_k: None)

    def _boom(_sql, _scope, _page=""):
        raise RuntimeError(raw)

    monkeypatch.setitem(q._FETCHERS, "live", _boom)
    m._reconnect_off_sis()                                              # a routine Refresh: never a reconnect
    assert calls == []
    res = q.run("SELECT 1 AS X /* c09 R1-006 follow-up */", page="t", key="k", tier="live")
    assert res.ok is False
    expired = "expired" in raw.lower() or "no longer exists" in raw.lower()
    assert ("Reload the app in your browser to reconnect" in res.error) is expired   # message and flag agree
    m._reconnect_off_sis()                                              # the Refresh after that failure
    assert len(calls) == cleared
    m._reconnect_off_sis()                                              # spent: the next one is a salt bump
    assert len(calls) == cleared
    from tests._source import read

    src = read("app/main.py")
    block = src.split('if st.button("Refresh data"', 1)[1].split("st.rerun()", 1)[0]
    assert "_reconnect_off_sis()" in block                              # the sidebar Refresh calls it
    st.session_state.clear()


# ------------------------------------------------------------------ R1-173 / R1-335: DEPLOY_ACTORS is a live setting ----

def _settings_tab_outcome(monkeypatch, keys: list[str]) -> tuple[list[str], list]:
    from types import SimpleNamespace

    from app.ui.pages import admin

    warnings: list[str] = []
    options: list = []

    def _selectbox(_label, opts, **_k):
        options.extend(opts)
        return opts[0]

    frame = pd.DataFrame({"KEY": keys, "VALUE": [""] * len(keys),
                          "UPDATED_AT": [None] * len(keys), "UPDATED_BY": [""] * len(keys)})
    monkeypatch.setattr(admin, "run", lambda *_a, **_k: QueryResult(df=frame.copy(), ok=True, source="SETTINGS"))
    monkeypatch.setattr(admin, "load_settings", lambda _p: {"_source": "stub"})
    monkeypatch.setattr(admin, "guard", lambda res, *_a, **_k: res.usable())
    for name in ("panel_help", "styled_table", "result_caption", "section_header"):
        monkeypatch.setattr(admin, name, lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "with_user_names", lambda df, *_a, **_k: df)
    monkeypatch.setattr(admin, "_setting_value_input", lambda *_a, **_k: "")
    monkeypatch.setattr(admin, "st", SimpleNamespace(caption=lambda *_a, **_k: None, warning=warnings.append,
                                                     selectbox=_selectbox, code=lambda *_a, **_k: None))
    admin._settings_tab(is_operator=False)
    return warnings, options


def test_deploy_actors_is_editable_and_never_called_safe_to_delete(monkeypatch):
    from app.config import DEFAULT_SETTINGS

    # every migration-seeded key that is still live, plus the V033 / V032 rows the account carries
    keys = [k for k in DEFAULT_SETTINGS if k != "DEPLOY_ACTORS"] + ["DEPLOY_ACTORS", "INCIDENT_REOPEN_DAYS"]
    warnings, options = _settings_tab_outcome(monkeypatch, keys)
    assert warnings == ["Settings rows the app no longer reads (safe to delete): INCIDENT_REOPEN_DAYS"]
    assert "DEPLOY_ACTORS" in options                    # docs/FLYWAY_ADOPTION.md: set it on Admin > Settings


def test_every_settings_key_the_app_reads_in_sql_is_a_known_setting():
    """Recurrence lock: a SETTINGS key an app SQL builder reads (`KEY = 'X'` / `KEY IN ('X', ...)`) must be in
    DEFAULT_SETTINGS, or Admin flags the live row 'safe to delete' and offers no editor for it."""
    import re

    from app.config import DEFAULT_SETTINGS
    from tests._source import ROOT

    read: set[str] = set()
    for path in sorted((ROOT / "app" / "data").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "SETTINGS" not in text:
            continue
        for m in re.finditer(r"\bKEY\s*(?:=\s*'([A-Z0-9_]+)'|IN\s*\(([^)]*)\))", text):
            if m.group(1):
                read.add(m.group(1))
            else:
                read |= set(re.findall(r"'([A-Z0-9_]+)'", m.group(2)))
    assert "DEPLOY_ACTORS" in read                       # the scan sees change_impact_sql's read
    assert read - set(DEFAULT_SETTINGS) == set()


# ------------------------------------------------------------------ R1-174: the access self-check probes fresh ----

def test_access_self_check_never_serves_a_stale_ok(monkeypatch):
    """The probes ran on the 4h 'metadata' tier; successes are cached and failures are not, so a grant revoked
    after a passing click still read 'All sources reachable' for up to 4h. Every probe now rides a tier whose
    cache lives no longer than one statement timeout (30s)."""
    from types import SimpleNamespace

    from app.core.query import CACHE_TTLS
    from app.ui.pages import admin

    tiers: list[str] = []

    def _batch(specs, *, page, tier):
        tiers.append(tier)
        return {s["key"]: QueryResult(ok=True, df=pd.DataFrame({"X": [1]})) for s in specs}

    def _run(*_a, tier, **_k):
        tiers.append(tier)
        return QueryResult(ok=True, df=pd.DataFrame({"name": ["WH"]}))

    shown: list = []
    monkeypatch.setattr(admin, "run_batch", _batch)
    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "styled_table", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", lambda kind, msg, **_k: shown.append((kind, msg)))
    monkeypatch.setattr(admin, "st", SimpleNamespace(caption=lambda *_a, **_k: None, button=lambda *_a, **_k: True,
                                                     error=lambda *_a, **_k: None, divider=lambda: None))
    admin._access_self_check()
    assert shown == [("clean", "All 7 sources reachable.")]
    assert tiers and all(CACHE_TTLS[t] <= 30 for t in tiers), tiers


# ------------------------------------------------------------------ R1-176: fleet stats count only slow/failed ----

@pytest.mark.parametrize("page", ["", "Watch"])
def test_fleet_query_stats_excludes_the_healthy_sample(page):
    """APP_QUERY_TELEMETRY also persists a ~2% healthy sample (SAMPLE_PROB 0.02); without a predicate those rows
    were counted as SLOW_OR_FAILED and pulled P50/P95, so the clean state was unreachable. The WHERE must keep
    only >=2s-or-failed rows (the SLOW_2S threshold telemetry_by_page uses), with or without a page filter."""
    import sqlglot

    from app.data import mart_sql

    sql = mart_sql.fleet_query_stats(7, page=page)
    where = sqlglot.parse_one(sql, read="snowflake").args["where"].sql(dialect="snowflake")
    assert "(NOT OK OR ELAPSED_MS >= 2000)" in where, where
    assert ("PAGE = 'Watch'" in where) == bool(page)
