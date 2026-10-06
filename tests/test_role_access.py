"""v4.610.0 role-based access (owner decision 2026-10-05).

The owner asked (2026-10-05) for SNOW_PRI_GFR_PRD_ALFA_DSA to be OVERWATCH ADMIN with full parity, and for
SNOW_PRI_GFR_PRD_ALFA_DTI to be VIEW-ONLY on exactly two pages (Cost Intelligence and Operations). This
supersedes the 2026-07-13 "Access = SNOW_ACCOUNTADMINS + SNOW_SYSADMINS, period" decision.

Resolution, first match wins, for an identified Streamlit-in-Snowflake viewer:
  1. OPERATOR_USERS (the named admins): DBA + operator, source 'allowlist'. No SQL runs.
  2. a DIRECT USER grantee of SNOW_PRI_GFR_PRD_ALFA_DSA (SHOW GRANTS OF ROLE, run as the owner): DBA +
     operator, source 'role'. A ROLE grantee is not expanded.
  3. otherwise MONITOR (read-only, two pages): source 'default' when the lookup answered, 'lookup_failed'
     when it raised, 'unverified' when it returned no USER grantee (a privilege gap reads as empty).
DTI is never looked up: only a role holding USAGE opens the app (the decision names four; roles.sql grants the two
SNOW_* roles until the owner's pending change lands), so every identified viewer who is not
an admin is a DTI member or a SNOW_* holder, and gets MONITOR. An UNIDENTIFIED SiS viewer gets MONITOR and
is never an operator.

Every check here fails CLOSED: a lookup failure, an empty roster, a revoke or an error never grants admin.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st

import app.config as cfg
import app.core.errors as errors_mod
import app.core.identity as ident
import app.core.query as q
import app.core.session as sess
from app.data import access_sql

_ADMIN = cfg.OPERATOR_USERS[0]
_DSA_USER = "DSA_PERSON1"
_DTI_USER = "DTI_PERSON1"


def _rows(*pairs: tuple[str, str]) -> list[dict]:
    """SHOW GRANTS OF ROLE rows: (granted_to, grantee_name)."""
    return [{"created_on": "2026-10-05", "role": cfg.ADMIN_ACCESS_ROLE, "granted_to": kind,
             "grantee_name": name, "granted_by": "SNOW_ACCOUNTADMINS"} for kind, name in pairs]


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(monkeypatch):
    """An identified SiS viewer with a scripted DSA roster. ``env.viewer`` / ``env.rows`` / ``env.raise_``
    drive the lookup; ``env.shows`` counts the SHOW statements; ``env.errors`` collects APP_ERROR_LOG rows."""
    st.session_state.clear()

    class Env:
        def __init__(self) -> None:
            self.viewer = _DSA_USER
            self.rows: list = _rows(("USER", _DSA_USER), ("USER", "OTHER_DSA"), ("ROLE", "SOME_NESTED_ROLE"))
            self.raise_: Exception | None = None
            self.shows = 0
            self.errors: list = []
            self.clock = _Clock()

    e = Env()

    def _lookup():
        e.shows += 1
        if e.raise_ is not None:
            raise e.raise_
        return list(e.rows)

    monkeypatch.setattr(ident, "viewer_name", lambda: e.viewer)
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    monkeypatch.setattr(sess, "_admin_role_rows", _lookup)
    monkeypatch.setattr(sess, "_clock", e.clock)
    monkeypatch.setattr(errors_mod, "record_error",
                        lambda page, exc, context="": e.errors.append((page, exc, context)) or "ref")
    yield e
    st.session_state.clear()


def _usage_rows() -> list[str]:
    buf = st.session_state.get("_ow_write_buffer") or {}
    return [row for group in buf.values() for row in group.get("rows", [])]


# ---------------------------------------------------------------------------
# Config: the two roles, the MONITOR surface, the parity flag, the timings
# ---------------------------------------------------------------------------
def test_access_roles_are_pinned():
    assert cfg.ADMIN_ACCESS_ROLE == "SNOW_PRI_GFR_PRD_ALFA_DSA"
    assert cfg.VIEW_ACCESS_ROLE == "SNOW_PRI_GFR_PRD_ALFA_DTI"
    assert cfg.APP_ACCESS_ROLES == ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS",
                                    "SNOW_PRI_GFR_PRD_ALFA_DSA", "SNOW_PRI_GFR_PRD_ALFA_DTI")
    # owner 2026-10-05: DSA gets FULL parity, account-level levers (ALTER USER / ALTER ACCOUNT SET) included
    assert cfg.ROLE_ADMIN_ACCOUNT_LEVERS is True


def test_monitor_is_exactly_cost_intelligence_and_operations():
    assert cfg.PAGES_BY_PROFILE["MONITOR"] == ("Cost Intelligence", "Operations")
    assert cfg.VIEWER_UNKNOWN_PROFILE == "MONITOR"
    assert cfg.NO_IDENTITY_PROFILE == "MONITOR"
    assert "MONITOR" not in cfg.OPERATOR_PROFILES
    for page in cfg.PAGES_BY_PROFILE["MONITOR"]:
        assert page in cfg.PAGES_BY_PROFILE["DBA"]


def test_access_timings():
    assert cfg.ACCESS_TTL_S == 300
    assert cfg.ACCESS_RETRY_S == 60
    assert 0 < cfg.WRITE_RECHECK_S <= 15


def test_failed_lookup_retry_backs_off_to_the_ttl():
    # holistic 4.610 #2/#13: every open session runs the lookup, so a long outage must not cost one SHOW per
    # session per minute: 60 s after the first failure, doubling, never longer than the healthy re-check
    assert [cfg.access_retry_s(n) for n in (1, 2, 3, 4, 5, 50)] == [60, 120, 240, 300, 300, 300]
    for odd in (0, -3, None, "x"):
        assert cfg.access_retry_s(odd) == cfg.ACCESS_RETRY_S


def test_viewer_profiles_hold_no_admin_pin_and_no_etl_pins():
    # OPERATOR_USERS alone means admin; a DBA pin is forbidden (it would be a second, drifting admin list)
    assert not {v for v in cfg.VIEWER_PROFILES.values() if v in cfg.OPERATOR_PROFILES}
    for etl in ("GRTHOMP1", "SUDEVAX", "TV5073", "VS4229"):
        assert etl.upper() not in {k.upper() for k in cfg.VIEWER_PROFILES}
    for profile in cfg.VIEWER_PROFILES.values():
        assert profile in cfg.PAGES_BY_PROFILE


def test_a_dba_pin_is_ignored_structurally(monkeypatch):
    monkeypatch.setattr(cfg, "VIEWER_PROFILES", {"SNEAKY": "DBA", "PINNED": "READER"})
    assert cfg.resolve_viewer_profile("sneaky") is None      # never DBA through a pin
    assert cfg.resolve_viewer_profile("pinned") == "READER"


def test_admin_access_hint_names_both_routes():
    assert "OPERATOR_USERS" in cfg.ADMIN_ACCESS_HINT
    assert cfg.ADMIN_ACCESS_ROLE in cfg.ADMIN_ACCESS_HINT


# ---------------------------------------------------------------------------
# The SHOW builder: a quoted identifier, never an injection point
# ---------------------------------------------------------------------------
def test_show_grants_of_role_builder():
    assert access_sql.show_grants_of_role_sql() == 'SHOW GRANTS OF ROLE "SNOW_PRI_GFR_PRD_ALFA_DSA"'
    assert access_sql.show_grants_of_role_sql(cfg.VIEW_ACCESS_ROLE) == \
        'SHOW GRANTS OF ROLE "SNOW_PRI_GFR_PRD_ALFA_DTI"'
    # an unquoted Snowflake identifier is stored upper-case; quoting a lower-case one would miss the role
    assert access_sql.show_grants_of_role_sql("snow_x") == 'SHOW GRANTS OF ROLE "SNOW_X"'


@pytest.mark.parametrize("bad", ['X"; DROP ROLE Y; --', "A B", "", "   ", "A;B", '"A"', "A\nB", "1ROLE",
                                 "A.B", "A'B", "X" * 300, None])
def test_show_grants_of_role_rejects_anything_but_a_plain_identifier(bad):
    with pytest.raises(ValueError):
        access_sql.show_grants_of_role_sql(bad)


# ---------------------------------------------------------------------------
# Parsing: direct USER grantees only; ROLE grantees reported, not expanded
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, d: dict) -> None:
        self._d = d

    def asDict(self) -> dict:  # Snowpark's Row API
        return dict(self._d)


def test_role_grant_members_parses_users_and_reports_nested_roles():
    # names exactly as SHOW stores them: stripped, never case-folded (holistic 4.610 #1)
    rows = _rows(("USER", "alice"), ("ROLE", "nested_r"), ("USER", " BOB "), ("APPLICATION", "app1"))
    users, roles = sess.role_grant_members(rows)
    assert users == frozenset({"alice", "BOB"})
    assert roles == ("nested_r",)


def test_role_grant_members_accepts_snowpark_rows_and_quoted_keys_and_frames():
    rows = [_Row({'"granted_to"': "user", '"grantee_name"': "carol"}),
            _Row({"GRANTED_TO": "ROLE", "GRANTEE_NAME": "R1"})]
    assert sess.role_grant_members(rows) == (frozenset({"carol"}), ("R1",))
    df = pd.DataFrame(_rows(("USER", "dave"), ("ROLE", "R2")))
    assert sess.role_grant_members(df) == (frozenset({"dave"}), ("R2",))
    assert sess.role_grant_members([]) == (frozenset(), ())


# ---------------------------------------------------------------------------
# Holistic 4.610 #1: membership is an EXACT name match. Two Snowflake users whose names differ only by case
# are different users; neither rides the other's DSA grant (the allowlist keeps its intentional folding).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("member, viewer", [("jdoe", "JDOE"), ("JDOE", "jdoe"), ("JDoe", "jdoe")])
def test_a_case_colliding_user_is_not_a_role_admin(env, member, viewer):
    env.rows = _rows(("USER", member))
    env.viewer = viewer
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "default")
    assert sess.is_operator() is False
    assert sess.reverify_role_admin() is False


@pytest.mark.parametrize("name", ["jdoe", "JDOE", "JDoe"])
def test_the_exact_name_is_a_role_admin(env, name):
    env.rows = _rows(("USER", name))
    env.viewer = name
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("DBA", True, "role")
    assert sess.reverify_role_admin() is True


def test_the_allowlist_keeps_its_case_folding(env):
    # OPERATOR_USERS is hand-typed: folding is intentional there, and it never consults the roster
    env.rows = _rows(("USER", "someone_else"))
    env.viewer = _ADMIN.lower()
    assert (sess.viewer_access()["source"], sess.is_operator()) == ("allowlist", True)
    assert env.shows == 0


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_allowlisted_admin_is_operator_and_issues_no_show(env):
    env.viewer = _ADMIN.lower()
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("DBA", True, "allowlist")
    assert sess.is_operator() is True and sess.active_profile("") == "DBA"
    assert env.shows == 0


def test_direct_dsa_member_is_admin(env):
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("DBA", True, "role")
    assert sess.is_operator() is True
    assert sess.active_profile("") == "DBA"
    assert sess.access_source() == "role"
    assert env.shows == 1                                   # one SHOW serves the whole resolution


def test_identified_non_member_gets_monitor_read_only(env):
    env.viewer = _DTI_USER
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "default")
    assert sess.is_operator() is False
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "MONITOR"   # never the owner's role surface


def test_nested_role_grantee_is_not_expanded(env):
    env.viewer = "SOME_NESTED_ROLE"                         # the name of a ROLE grantee, not a user
    assert sess.viewer_access()["operator"] is False


def test_a_non_dba_pin_applies_to_a_non_member_but_dsa_wins(env, monkeypatch):
    monkeypatch.setattr(cfg, "VIEWER_PROFILES", {_DTI_USER: "READER", _DSA_USER: "READER"})
    env.viewer = _DTI_USER
    assert sess.viewer_access()["profile"] == "READER"
    st.session_state.clear()
    env.viewer = _DSA_USER
    assert (sess.viewer_access()["profile"], sess.is_operator()) == ("DBA", True)


def test_lookup_failure_fails_closed_and_logs_once(env):
    env.raise_ = RuntimeError("Insufficient privileges to operate on role")
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "lookup_failed")
    assert "Insufficient privileges" in a["error"]
    assert sess.is_operator() is False
    # retried after ACCESS_RETRY_S, not before; one APP_ERROR_LOG row per session
    env.clock.t += cfg.ACCESS_RETRY_S - 1
    sess.viewer_access()
    assert env.shows == 1
    env.clock.t += 2
    sess.viewer_access()
    assert env.shows == 2
    assert len(env.errors) == 1 and env.errors[0][0] == "Access"
    assert cfg.ADMIN_ACCESS_ROLE in env.errors[0][2]
    # recovery: the next retry that answers grants the role again (the second failure backed off to 2x)
    env.raise_ = None
    env.clock.t += cfg.access_retry_s(2) + 1
    assert sess.viewer_access()["source"] == "role"


@pytest.mark.parametrize("fail", ["raise", "empty"])
def test_a_persistent_failure_backs_off_then_a_success_resets_it(env, fail):
    """holistic 4.610 #2/#13: each consecutive failed (or empty) lookup doubles the wait before the next SHOW,
    up to ACCESS_TTL_S; a good answer resets the streak, so the next outage starts at ACCESS_RETRY_S again."""
    env.viewer = _DTI_USER                      # a view-only session pays for the lookup too
    if fail == "raise":
        env.raise_ = RuntimeError("Insufficient privileges")
    else:
        env.rows = _rows(("ROLE", "ONLY_A_ROLE"))
    sess.viewer_access()
    assert env.shows == 1
    for n in (1, 2, 3, 4, 5):
        wait = cfg.access_retry_s(n)
        env.clock.t += wait - 1
        sess.viewer_access()
        sess.is_operator()
        assert env.shows == n, (n, env.shows)    # not before the backed-off wait
        env.clock.t += 2
        assert sess.viewer_access()["source"] in sess.ACCESS_UNAVAILABLE_SOURCES
        assert env.shows == n + 1, (n, env.shows)
    assert st.session_state["_ow_access_roster"]["fails"] == 6
    assert len(env.errors) == 1                  # still one APP_ERROR_LOG row per session
    # recovery resets the streak
    env.raise_, env.rows = None, _rows(("USER", _DSA_USER))
    env.clock.t += cfg.ACCESS_TTL_S + 1
    assert sess.viewer_access()["source"] == "default"
    assert st.session_state["_ow_access_roster"]["fails"] == 0
    env.raise_ = RuntimeError("down again")
    env.clock.t += cfg.ACCESS_TTL_S + 1
    sess.viewer_access()
    shows = env.shows
    env.clock.t += cfg.ACCESS_RETRY_S + 1
    sess.viewer_access()
    assert env.shows == shows + 1               # the new outage starts at ACCESS_RETRY_S again


def test_an_empty_user_set_is_unverified_not_member(env):
    env.rows = _rows(("ROLE", "ONLY_A_ROLE"))
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "unverified")
    assert len(env.errors) == 1
    env.clock.t += cfg.ACCESS_RETRY_S + 1
    sess.viewer_access()
    assert env.shows == 2


def test_success_is_memoized_for_the_ttl_then_re_resolved(env):
    sess.viewer_access()
    for _ in range(5):
        sess.is_operator()
        sess.active_profile("")
    env.clock.t += cfg.ACCESS_TTL_S - 1
    sess.viewer_access()
    assert env.shows == 1
    env.rows = _rows(("USER", "SOMEONE_ELSE"))             # revoked meanwhile
    env.clock.t += 2
    assert (sess.viewer_access()["source"], sess.is_operator()) == ("default", False)
    assert env.shows == 2


def test_memo_is_keyed_by_viewer(env):
    assert sess.is_operator() is True
    env.viewer = _DTI_USER
    assert sess.is_operator() is False


def test_unidentified_sis_viewer_gets_monitor_and_never_operates(env):
    env.viewer = ""
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "MONITOR"
    assert sess.is_operator() is False
    assert sess.access_source() == "no_identity"
    assert env.shows == 0


def test_off_sis_identified_viewer_never_runs_the_show(env, monkeypatch):
    monkeypatch.setattr(sess, "is_sis", lambda: False)
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "default")
    assert env.shows == 0


def test_an_off_sis_default_is_never_memoized(env, monkeypatch):
    # Review d6c62b87 #2: on SiS a failed connection makes is_sis() False (get_cached_session() is None),
    # so the off-SiS 'default' answer is reached by a DISCONNECTED SiS run. Kept for ACCESS_TTL_S, it held
    # a direct DSA member read-only for 5 minutes after the connection came back.
    sis = {"on": False}
    monkeypatch.setattr(sess, "is_sis", lambda: sis["on"])
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "default")
    assert "_ow_access" not in st.session_state
    sis["on"] = True                                     # the connection recovered, same clock
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("DBA", True, "role")
    assert env.shows == 1


def test_off_sis_without_identity_keeps_the_role_path(env, monkeypatch):
    env.viewer = ""
    monkeypatch.setattr(sess, "is_sis", lambda: False)
    monkeypatch.setattr(sess, "current_role", lambda: "SNOW_SYSADMINS")
    assert sess.active_profile("SNOW_SYSADMINS") == "DBA"
    assert sess.is_operator() is True
    assert sess.access_source() == "off_sis"


def test_one_usage_event_per_source_names_it(env):
    sess.viewer_access()
    sess.is_operator()
    rows = _usage_rows()
    assert len(rows) == 1 and "'access_resolved'" in rows[0] and "'role'" in rows[0]
    env.rows = _rows(("USER", "SOMEONE_ELSE"))
    env.clock.t += cfg.ACCESS_TTL_S + 1
    sess.viewer_access()
    rows = _usage_rows()
    assert len(rows) == 2 and "'default'" in rows[1]       # the revoke transition is on the record


def test_usage_event_respects_the_usage_off_switch(env):
    st.session_state["_ow_usage_off"] = True
    sess.viewer_access()
    assert _usage_rows() == []


# ---------------------------------------------------------------------------
# Write-time re-verification (role-sourced admins only)
# ---------------------------------------------------------------------------
def test_reverify_runs_a_fresh_show_and_memoizes_briefly(env):
    sess.viewer_access()
    assert env.shows == 1
    assert sess.reverify_role_admin() is True
    assert env.shows == 2                                   # fresh, not the 300 s page memo
    env.clock.t += cfg.WRITE_RECHECK_S - 1
    assert sess.reverify_role_admin() is True
    assert env.shows == 2
    env.clock.t += 2
    assert sess.reverify_role_admin() is True
    assert env.shows == 3


def test_reverify_refuses_after_a_revoke_and_drops_the_page_memo(env):
    assert sess.is_operator() is True
    env.rows = _rows(("USER", "SOMEONE_ELSE"))
    assert sess.reverify_role_admin() is False
    # the UI catches up on the next call instead of waiting out the 300 s memo
    assert sess.is_operator() is False
    assert sess.access_source() == "default"


@pytest.mark.parametrize("rows, exc", [([], None), (None, RuntimeError("SHOW failed"))])
def test_reverify_refuses_on_an_empty_roster_or_an_error(env, rows, exc):
    assert sess.is_operator() is True
    if rows is not None:
        env.rows = rows
    env.raise_ = exc
    assert sess.reverify_role_admin() is False


def test_reverify_is_false_without_an_identity(env):
    env.viewer = ""
    assert sess.reverify_role_admin() is False
    assert env.shows == 0


# ---------------------------------------------------------------------------
# Admin ▸ App access API
# ---------------------------------------------------------------------------
def test_access_info_snapshot(env):
    info = sess.access_info()
    assert info["viewer"] == _DSA_USER and info["source"] == "role" and info["operator"] is True
    assert info["admin_role"] == cfg.ADMIN_ACCESS_ROLE and info["view_role"] == cfg.VIEW_ACCESS_ROLE
    assert info["account_levers"] is True
    assert info["roster_status"] == "ok"
    assert info["admin_users"] == (_DSA_USER, "OTHER_DSA")             # sorted
    assert info["nested_roles"] == ("SOME_NESTED_ROLE",)
    assert tuple(info["allowlist"]) == tuple(cfg.OPERATOR_USERS)
    assert info["age_s"] == 0
    assert info["ttl_s"] == cfg.ACCESS_TTL_S and info["retry_s"] is None      # a good roster: no retry pending


@pytest.mark.parametrize("fail", ["raise", "empty"])
def test_access_info_retry_is_the_backoff_in_force(env, fail):
    """retry_s is the wait before the next lookup after the CURRENT failure streak (config.access_retry_s),
    never the fixed first-retry constant; a recovered roster clears it."""
    if fail == "raise":
        env.raise_ = RuntimeError("Insufficient privileges")
    else:
        env.rows = _rows(("ROLE", "ONLY_A_ROLE"))
    assert sess.access_info()["retry_s"] == cfg.access_retry_s(1) == cfg.ACCESS_RETRY_S
    for n in (2, 3, 4, 5):
        env.clock.t += cfg.access_retry_s(n - 1) + 1
        info = sess.access_info()
        assert env.shows == n
        assert info["retry_s"] == cfg.access_retry_s(n), (n, info["retry_s"])
    assert info["retry_s"] == cfg.ACCESS_TTL_S != cfg.ACCESS_RETRY_S             # backed off to the TTL
    env.raise_, env.rows = None, _rows(("USER", _DSA_USER))
    env.clock.t += cfg.ACCESS_TTL_S + 1
    info = sess.access_info()
    assert info["roster_status"] == "ok" and info["retry_s"] is None


def test_access_info_roster_for_an_allowlisted_admin_is_lazy(env):
    env.viewer = _ADMIN
    assert sess.access_info()["roster_status"] == "not_checked"
    assert env.shows == 0
    info = sess.access_info(roster=True)
    assert info["roster_status"] == "ok" and _DSA_USER in info["admin_users"]
    assert env.shows == 1
    assert info["source"] == "allowlist"                    # the roster never changes an admin's source


def test_recheck_access_clears_only_this_sessions_memo(env):
    sess.viewer_access()
    env.rows = _rows(("USER", "SOMEONE_ELSE"))
    info = sess.recheck_access()
    assert env.shows == 2
    assert info["source"] == "default" and info["operator"] is False


# ---------------------------------------------------------------------------
# Executor: the widened privileged set and the write-time re-check
# ---------------------------------------------------------------------------
class _Stmt:
    def __init__(self, log: list, sql: str) -> None:
        self.log, self.sql = log, sql

    def collect(self, **_k):
        self.log.append(self.sql)
        return [("OK: done",)]

    def collect_nowait(self, **_k):
        self.log.append(self.sql)


class _Session:
    def __init__(self) -> None:
        self.log: list[str] = []

    def sql(self, sql: str) -> _Stmt:
        return _Stmt(self.log, sql)


@pytest.fixture
def wired(monkeypatch):
    st.session_state.clear()
    s, errs = _Session(), []
    monkeypatch.setattr(q, "get_session", lambda: s)
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q.st, "spinner", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(q, "record_error", lambda page, exc, context="": errs.append((page, exc, context)) or "r")
    yield s, errs
    st.session_state.clear()


@pytest.mark.parametrize("stmt", [
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS (USER_NAME) SELECT 'U'",
    "MERGE INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS t USING (SELECT 1) s ON 1=1 WHEN MATCHED THEN DELETE",
    "merge into dba_maint_db.overwatch.user_watchlist t using (select 1) s on 1=1 when matched then delete",
    "DELETE FROM DBA_MAINT_DB.OVERWATCH.USER_WATCHLIST WHERE 1=0",
    "\nMERGE INTO DBA_MAINT_DB.OVERWATCH.USER_WATCHLIST t\nUSING (SELECT 1) s ON 1=1 WHEN MATCHED THEN DELETE",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE) SELECT 'x'",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY(PAGE) SELECT 'x'",
])
def test_self_service_targets_are_not_privileged(stmt):
    assert q._is_privileged(stmt) is False


@pytest.mark.parametrize("stmt", [
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE (TITLE) SELECT 'x'",
    "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET ENABLED = FALSE WHERE RULE_ID = 'R'",
    "DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE 1=0",
    "MERGE INTO DBA_MAINT_DB.OVERWATCH.ENTITY_OWNERSHIP t USING (SELECT 1) s ON 1=1 WHEN MATCHED THEN DELETE",
    "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1','ACK','','','U','k1')",
    # substring bypasses: the exact token after the prefix decides, never a mention elsewhere
    "UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE SET NOTE = 'USER_PREFS'",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS_X (A) SELECT 1",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGEX (A) SELECT 1",
    'INSERT INTO DBA_MAINT_DB.OVERWATCH."USER_PREFS" (A) SELECT 1',
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS.X (A) SELECT 1",
    "ALTER WAREHOUSE WH_X SUSPEND",
    "ALTER USER U SET DISABLED = TRUE",
])
def test_everything_else_on_overwatch_is_privileged(stmt):
    assert q._is_privileged(stmt) is True


def test_target_object_reads_the_exact_token():
    assert q._target_object("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (A) SELECT 1") == "APP_USAGE"
    assert q._target_object("CALL DBA_MAINT_DB.OVERWATCH.SP_X('a')") == "SP_X"
    assert q._target_object("UPDATE DBA_MAINT_DB.OVERWATCH.T\nSET A = 1") == "T"
    assert q._target_object('INSERT INTO DBA_MAINT_DB.OVERWATCH."Q" (A) SELECT 1') == ""
    assert q._target_object("ALTER WAREHOUSE W SUSPEND") == ""
    assert set(q._SELF_SERVICE_OBJECTS) == {"USER_PREFS", "USER_WATCHLIST", "APP_USAGE", "APP_QUERY_TELEMETRY"}


def test_non_operator_overwatch_write_is_refused(wired, monkeypatch):
    s, errs = wired
    monkeypatch.setattr(sess, "is_operator", lambda: False)
    ok, msg = q.execute_statement("UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE SET NOTE = 'USER_PREFS'", page="x")
    assert ok is False and "operator entitlement required" in msg and s.log == []
    assert len(errs) == 1 and isinstance(errs[0][1], PermissionError)
    ok, msg = q.execute_action("CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1','ACK','','','U','k1')",
                               [], page="Alerts")
    assert ok is False and "operator entitlement required" in msg and s.log == []


def test_role_admin_write_is_re_verified(wired, monkeypatch):
    calls = {"n": 0}

    def _reverify() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "role")
    monkeypatch.setattr(sess, "reverify_role_admin", _reverify)
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is True
    assert q.execute_action("CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1','ACK','','','U','k1')",
                            [], page="Alerts")[0] is True
    assert q.execute_cancel_query("01b2c3d4-0000-1111-2222-333344445555", page="Operations")[0] is False
    assert calls["n"] == 3                                  # every privileged write re-verifies
    # telemetry and self-service writes never consult it
    assert q.execute_statement_async("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (X) SELECT 1", page="x")
    assert calls["n"] == 3


@pytest.mark.parametrize("outcome", [False, RuntimeError("SHOW failed")])
def test_role_admin_write_refused_when_re_verification_fails(wired, monkeypatch, outcome):
    s, errs = wired

    def _reverify() -> bool:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "role")
    monkeypatch.setattr(sess, "reverify_role_admin", _reverify)
    ok, msg = q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")
    assert ok is False and "operator entitlement required" in msg and cfg.ADMIN_ACCESS_ROLE in msg
    assert s.log == [] and len(errs) == 1


def test_allowlisted_admin_write_never_re_verifies(wired, monkeypatch):
    def _boom() -> bool:
        raise AssertionError("an allowlisted admin must not pay a SHOW per write")

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "allowlist")
    monkeypatch.setattr(sess, "reverify_role_admin", _boom)
    assert q.execute_statement("ALTER USER U SET DISABLED = TRUE", page="Operations")[0] is True


def test_off_sis_operator_writes_without_a_lookup(wired, monkeypatch):
    def _boom() -> bool:
        raise AssertionError("off-SiS (local dev) has no owner's-rights role to re-verify")

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "off_sis")
    monkeypatch.setattr(sess, "reverify_role_admin", _boom)
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is True


@pytest.mark.parametrize("source", ["default", "lookup_failed", "unverified", "no_identity", "", "not_a_source"])
def test_operator_with_a_non_admin_source_is_refused(wired, monkeypatch, source):
    """is_operator() and access_source() are two reads of the access memo, and the memo can expire between
    them, so the second read can come back as a non-admin source while the first said 'operator'. The
    executor allows only 'allowlist' and 'off_sis' outright, re-verifies 'role', and refuses every other
    source. It never reasons 'not role, so allowlist'."""
    s, errs = wired

    def _boom() -> bool:
        raise AssertionError("only a role-sourced admin re-verifies")

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: source)
    monkeypatch.setattr(sess, "reverify_role_admin", _boom)
    for stmt in ("ALTER WAREHOUSE WH_X SUSPEND", "ALTER USER U SET DISABLED = TRUE",
                 "UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE SET NOTE = 'x'"):
        ok, msg = q.execute_statement(stmt, page="Operations")
        assert ok is False and "operator entitlement required" in msg, (source, stmt)
    assert s.log == [] and len(errs) == 3


def test_operator_whose_source_read_raises_is_refused(wired, monkeypatch):
    s, _ = wired

    def _raise() -> str:
        raise RuntimeError("memo unreadable")

    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", _raise)
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is False
    assert s.log == []


@pytest.mark.parametrize("change", ["revoke", "lookup_error", "empty"])
def test_end_to_end_memo_expiring_between_the_two_reads_never_fails_open(env, wired, monkeypatch, change):
    """The real session + executor code with only the clock seam driven: the page memo is 299.5 s old when
    the write starts, so is_operator() still reads it as 'role' and the next read (a tick later) finds it
    expired and re-resolves. Whatever that re-resolution answers (a revoke, a failed SHOW, an empty roster),
    the write must not run."""
    s, _ = wired
    assert sess.is_operator() is True                      # page memo at t0, source 'role'
    env.clock.t += cfg.ACCESS_TTL_S - 0.5
    if change == "revoke":
        env.rows = _rows(("USER", "SOMEONE_ELSE"))
    elif change == "lookup_error":
        env.raise_ = RuntimeError("SHOW failed")
    else:
        env.rows = []

    def _ticking() -> float:
        env.clock.t += 0.3                                  # every clock read moves time on
        return env.clock.t

    monkeypatch.setattr(sess, "_clock", _ticking)
    ok, msg = q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")
    assert ok is False and "operator entitlement required" in msg
    assert s.log == []


@pytest.mark.parametrize("stmt, allowed", [
    ("ALTER USER U SET DISABLED = TRUE", False),
    ("ALTER ACCOUNT SET STATEMENT_TIMEOUT_IN_SECONDS = 7200", False),
    ("ALTER WAREHOUSE WH_X SUSPEND", True),
])
def test_account_levers_follow_the_parity_flag(wired, monkeypatch, stmt, allowed):
    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "role")
    monkeypatch.setattr(sess, "reverify_role_admin", lambda: True)
    assert q.execute_statement(stmt, page="Operations")[0] is True          # parity (the shipped value)
    monkeypatch.setattr(cfg, "ROLE_ADMIN_ACCOUNT_LEVERS", False)
    ok, msg = q.execute_statement(stmt, page="Operations")
    assert ok is allowed
    if not allowed:
        assert "OPERATOR_USERS" in msg


def test_end_to_end_revoked_dsa_member_cannot_write(env, wired, monkeypatch):
    s, _ = wired
    assert sess.is_operator() is True
    env.rows = _rows(("USER", "SOMEONE_ELSE"))             # revoked after the page memo was taken
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is False
    assert s.log == []
    assert sess.is_operator() is False                      # the refusal also downgraded the session


def test_end_to_end_dsa_member_writes(env, wired):
    s, _ = wired
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is True
    assert s.log == ["ALTER WAREHOUSE WH_X SUSPEND"]


def test_end_to_end_monitor_viewer_cannot_write_but_keeps_self_service(env, wired):
    s, _ = wired
    env.viewer = _DTI_USER
    assert q.execute_statement("ALTER WAREHOUSE WH_X SUSPEND", page="Operations")[0] is False
    ok, _ = q.execute_statement("DELETE FROM DBA_MAINT_DB.OVERWATCH.USER_WATCHLIST WHERE 1=0", page="x")
    assert ok is True and s.log == ["DELETE FROM DBA_MAINT_DB.OVERWATCH.USER_WATCHLIST WHERE 1=0"]


# ---------------------------------------------------------------------------
# Navigation clamp: a profile without Overview lands on its own first page
# ---------------------------------------------------------------------------
def test_clamp_target_prefers_overview_then_the_profiles_landing():
    from app.core.state import clamp_page
    dba, monitor = cfg.PAGES_BY_PROFILE["DBA"], cfg.PAGES_BY_PROFILE["MONITOR"]
    assert clamp_page("Alerts", dba) == "Alerts"
    assert clamp_page("Alerts", monitor) == "Cost Intelligence"
    assert clamp_page("Operations", monitor) == "Operations"
    assert clamp_page("Admin", cfg.PAGES_BY_PROFILE["EXECUTIVE"]) == "Overview"
    assert clamp_page("Admin", ()) == "Admin"                # an unreadable profile: the main.py deny holds
    assert clamp_page("", monitor) == ""


def test_main_falls_back_to_the_least_privileged_surface():
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    body = main.split("def main()", 1)[1]
    assert "PAGES_BY_PROFILE.get(profile, PAGES_BY_PROFILE[VIEWER_UNKNOWN_PROFILE])" in body
    assert 'PAGES_BY_PROFILE["ANALYST"]' not in body


def test_a_clamped_pending_jump_drops_the_foreign_section_and_context(monkeypatch):
    from app.core import state
    st.session_state.clear()
    monkeypatch.setattr(sess, "active_profile", lambda role="": "MONITOR")
    monkeypatch.setattr(sess, "current_role", lambda: "")
    st.session_state["_ow_nav_pending"] = {"page": "Alerts", "section": "Open events",
                                           "filters": {}, "context": {"event_id": "E1"}}
    state.consume_pending_navigation()
    assert st.session_state["_ow_page"] == "Cost Intelligence"
    assert "cost_section" not in st.session_state
    assert st.session_state["_ow_nav_context"] == {}
    # an in-profile jump keeps its section and context
    st.session_state["_ow_nav_pending"] = {"page": "Operations", "section": "Queries",
                                           "filters": {}, "context": {"query_id": "Q1"}}
    state.consume_pending_navigation()
    assert st.session_state["_ow_page"] == "Operations"
    assert st.session_state["_ow_nav_context"] == {"query_id": "Q1"}
    st.session_state.clear()


# ---------------------------------------------------------------------------
# End-to-end (AppTest): the shell on SiS for a role admin, a failed lookup and a view-only viewer
# ---------------------------------------------------------------------------
def _fake_run(*_args, **kwargs):
    from app.core.result import QueryResult
    return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))


@pytest.fixture
def sis_app(monkeypatch):
    """The real app shell as an identified SiS viewer whose admin-role lookup is scripted."""
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security
    from app.ui.pages.cost_parts import ai_chargeback, contract, optimize, spend

    state = {"viewer": _DSA_USER, "rows": _rows(("USER", _DSA_USER)), "raise": None}

    def _lookup():
        if state["raise"] is not None:
            raise state["raise"]
        return list(state["rows"])

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_ACCOUNTADMINS")
    monkeypatch.setattr(ident, "viewer_name", lambda: state["viewer"])
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    monkeypatch.setattr(sess, "_admin_role_rows", _lookup)
    monkeypatch.setattr(errors_mod, "record_error", lambda *a, **k: "ref")
    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    for module in (overview, control_room, cost, operations, alerts, security, admin,
                   spend, contract, ai_chargeback, optimize):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _fake_run)
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))
    return state


def _entry():
    import app.main

    app.main.main()


def _run_app():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    options = [o for r in at.radio if str(getattr(r, "key", "") or "").startswith("_ow_nav_")
               for o in r.options]
    captions = [str(c.value) for c in at.sidebar.caption]
    return at, options, captions


def _unavailable_caption() -> str:
    from app.main import ACCESS_CHECK_UNAVAILABLE  # lazy, like every app.main import in the suite
    return ACCESS_CHECK_UNAVAILABLE


def test_app_role_admin_gets_the_dba_surface(sis_app):
    _, options, captions = _run_app()
    assert "Admin" in options and "Alerts" in options and "Ask" in options
    assert _unavailable_caption() not in captions


def test_app_retry_connection_forgets_the_access_memo(sis_app, monkeypatch):
    # Review d6c62b87 #2: 'Retry connection' cleared the cached session and role but not the access memo, so
    # a lookup that failed during the outage kept a direct DSA member read-only until ACCESS_RETRY_S ran out.
    from streamlit.testing.v1 import AppTest

    import app.main as main_mod

    link = {"up": False}
    monkeypatch.setattr(main_mod, "connection_available", lambda: link["up"])
    monkeypatch.setattr(sess, "connection_error", lambda: "")
    real_clear = st.cache_resource.clear

    def _reconnect() -> None:                # the handler's cache clear is what brings the session back
        real_clear()
        link["up"], sis_app["raise"] = True, None

    monkeypatch.setattr(st.cache_resource, "clear", _reconnect)
    sis_app["raise"] = RuntimeError("connection lost")
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["_ow_access"]["source"] == "lookup_failed"
    retry = [b for b in at.button if str(b.label) == "Retry connection"]
    assert retry, [b.label for b in at.button]
    retry[0].click()
    at.run()
    assert not at.exception, at.exception
    options = [o for r in at.radio if str(getattr(r, "key", "") or "").startswith("_ow_nav_")
               for o in r.options]
    assert "Admin" in options, options


def test_app_failed_lookup_is_read_only_and_says_so(sis_app):
    sis_app["raise"] = RuntimeError("Insufficient privileges")
    at, options, captions = _run_app()
    assert sorted(options) == ["Cost Intelligence", "Operations"]
    assert _unavailable_caption() in captions
    assert at.session_state["_ow_page"] == "Cost Intelligence"


def test_app_view_only_viewer_sees_two_pages_without_the_outage_caption(sis_app):
    sis_app["viewer"] = _DTI_USER
    _, options, captions = _run_app()
    assert sorted(options) == ["Cost Intelligence", "Operations"]
    assert _unavailable_caption() not in captions


@pytest.mark.parametrize("outage", ["failed", "unverified"])
def test_app_view_only_viewer_during_an_outage_is_told_only_what_is_true(sis_app, outage):
    """holistic 4.610 #2/#13: during an outage the app cannot tell a DTI member from a DSA member, so a view-only
    viewer sees the caption too. It is deliberate, and its wording must hold for them: it says who is held
    read-only (admins) and never promises this viewer changes once the lookup recovers."""
    sis_app["viewer"] = _DTI_USER
    if outage == "failed":
        sis_app["raise"] = RuntimeError("Insufficient privileges")
    else:
        sis_app["rows"] = _rows(("ROLE", "ONLY_A_ROLE"))
    _, options, captions = _run_app()
    assert sorted(options) == ["Cost Intelligence", "Operations"]
    caption = _unavailable_caption()
    assert caption in captions
    assert caption.startswith("Admin access check unavailable")
    # only the admins BY ROLE are held read-only; the named admins never wait on the lookup (final review)
    assert f"admins by role ({cfg.ADMIN_ACCESS_ROLE}) are read-only until it recovers" in caption
    assert caption.endswith("named admins are unaffected.")
    assert "OVERWATCH admins are read-only" not in caption
    assert caption != "Access check unavailable — read-only until it recovers."


def _jump_app_run(monkeypatch):
    """The shell with a seeded Case File and the live jump targets loaded; records the run() keys."""
    from streamlit.testing.v1 import AppTest

    import app.main as main_mod
    from app.core.result import QueryResult
    from app.logic.case_file import CASE_STATE_KEY

    keys: list[str] = []

    def _run(*_a, **kwargs):
        keys.append(str(kwargs.get("key", "")))
        df = pd.DataFrame({"RULE_ID": ["RULE_X"]}) if kwargs.get("key") == "jump_rules" else pd.DataFrame()
        return QueryResult(df=df, ok=True, source="stub")

    monkeypatch.setattr(main_mod, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.session_state[CASE_STATE_KEY] = [{"title": "Evidence"}]
    at.session_state["_ow_jump_loaded"] = True
    at.run()
    assert not at.exception, at.exception
    buttons = [str(b.key or "") for b in at.sidebar.button]
    jump = [o for sb in at.sidebar.selectbox if str(sb.key or "").startswith("_ow_jump_") for o in sb.options]
    return buttons, jump, keys


def test_app_monitor_shell_offers_no_case_file_or_rule_jumps(sis_app, monkeypatch):
    """MONITOR has neither Brief (the Case File's home) nor Alerts (where a rule opens): the shell's Case File
    button and the jump box's 'Rule ·' options would clamp it onto Cost Intelligence, so neither renders and
    the ALERT_CONFIG read behind the rule options is skipped. A role admin keeps both."""
    sis_app["viewer"] = _DTI_USER
    buttons, jump, keys = _jump_app_run(monkeypatch)
    assert "_ow_case_shell" not in buttons
    assert not [o for o in jump if o.startswith("Rule · ")] and "jump_rules" not in keys
    assert [o for o in jump if o.startswith("Page · ")] == ["Page · Cost Intelligence", "Page · Operations"]


def test_app_role_admin_shell_keeps_the_case_file_and_rule_jumps(sis_app, monkeypatch):
    buttons, jump, keys = _jump_app_run(monkeypatch)
    assert "_ow_case_shell" in buttons
    assert "Rule · RULE_X" in jump and "jump_rules" in keys


def test_rule_jump_dispatch_is_gated_on_alerts(monkeypatch):
    import app.main as m
    from app.core import state

    navs: list[tuple] = []
    monkeypatch.setattr(m, "request_navigation", lambda *a, **k: navs.append(a))
    monkeypatch.setattr(sess, "current_role", lambda: "")
    monkeypatch.setattr(sess, "active_profile", lambda role="": "MONITOR")
    assert not state.can_open("Alerts")
    m._dispatch_jump("Rule · RULE_X")          # a stale recent can never carry MONITOR off its pages
    assert navs == []
    monkeypatch.setattr(sess, "active_profile", lambda role="": "DBA")
    m._dispatch_jump("Rule · RULE_X")
    assert navs == [("Alerts", "Rules")]


class _CaseSt:
    """``st`` stand-in for add_to_case_button: records whether the button rendered; it is clicked."""

    def __init__(self) -> None:
        self.session_state: dict = {}
        self.buttons = 0

    def button(self, *_a, **_k):
        self.buttons += 1
        return True

    def toast(self, *_a, **_k):
        return None


def test_add_to_case_renders_only_where_the_case_file_opens(monkeypatch):
    """The Case File is reviewed and exported on Brief. MONITOR has no Brief, so an 'Add to Case' button on
    Operations would fill a file the viewer can never open: it does not render there."""
    import app.core.state as state
    from app.core.result import QueryResult
    from app.logic.case_file import CASE_STATE_KEY
    from app.ui import components

    fake = _CaseSt()
    monkeypatch.setattr(components, "st", fake)
    monkeypatch.setattr(state, "filters", lambda: {"company": "ALFA", "window_label": "30d", "days": 30})
    monkeypatch.setattr(sess, "current_role", lambda: "")
    monkeypatch.setattr(sess, "active_profile", lambda role="": "MONITOR")
    res = QueryResult(df=pd.DataFrame({"A": [1]}), ok=True, source="t")
    assert components.add_to_case_button("Operations · Queries", res, summary="s", key="k1") is False
    assert fake.buttons == 0 and CASE_STATE_KEY not in fake.session_state
    monkeypatch.setattr(sess, "active_profile", lambda role="": "READER")
    assert components.add_to_case_button("Operations · Queries", res, summary="s", key="k2") is True
    assert fake.buttons == 1 and len(fake.session_state[CASE_STATE_KEY]) == 1


def test_entity_nav_table_is_a_plain_table_without_control_room(monkeypatch):
    """entity_nav_table's row click opens Control Room ▸ Entity 360. For MONITOR the clamp sends that click to
    Cost Intelligence (its landing page), so on Operations it would eject the viewer: the table renders plain,
    with no row-select hint and no selection, wherever Control Room does not open."""
    from app.core import state
    from app.ui import components

    calls = {"styled": 0, "nav_table": 0, "hint": 0}
    navs: list[tuple] = []

    def _bump(name):
        return lambda *a, **k: calls.__setitem__(name, calls[name] + 1)

    def _nav_table(frame, key, on_select, **k):
        calls["nav_table"] += 1
        on_select(0)

    monkeypatch.setattr(components, "styled_table", _bump("styled"))
    monkeypatch.setattr(components, "row_select_hint", _bump("hint"))
    monkeypatch.setattr(components, "selectable_nav_table", _nav_table)
    monkeypatch.setattr(state, "request_navigation", lambda *a, **k: navs.append(a))
    monkeypatch.setattr(sess, "current_role", lambda: "")
    monkeypatch.setattr(sess, "active_profile", lambda role="": "MONITOR")
    df = pd.DataFrame({"TASK_FQN": ["DB.S.T"]})
    components.entity_nav_table(df, key="t", key_col="TASK_FQN", entity_type="TASK")
    assert calls == {"styled": 1, "nav_table": 0, "hint": 0} and navs == []
    monkeypatch.setattr(sess, "active_profile", lambda role="": "READER")
    components.entity_nav_table(df, key="t", key_col="TASK_FQN", entity_type="TASK")
    assert calls == {"styled": 1, "nav_table": 1, "hint": 1} and navs == [("Control Room", "Entity 360")]


# ---------------------------------------------------------------------------
# Structural lock: no doorway off MONITOR's two pages (AGENTS.md "gate the affordance to profiles that have
# the target page"). MONITOR has no Overview, so the clamp sends an off-profile jump to Cost Intelligence:
# an ungated drill on Operations is not a no-op, it ejects the viewer.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
#: every module that renders on a MONITOR page: the shell, the shared UI the two pages import, the two pages
_MONITOR_SURFACE: tuple[str, ...] = (
    "app/main.py", "app/ui/components.py", "app/ui/charts.py", "app/ui/attention.py", "app/ui/ai_panel.py",
    "app/ui/schema_gate.py", "app/ui/pages/cost.py", "app/ui/pages/operations.py",
    *sorted(p.relative_to(_ROOT).as_posix() for p in (_ROOT / "app" / "ui" / "pages" / "cost_parts").glob("*.py")),
    *sorted(p.relative_to(_ROOT).as_posix() for p in (_ROOT / "app" / "ui" / "pages" / "ops_parts").glob("*.py")),
)


def _literal_nav_calls(tree: ast.AST):
    """(call, target page) for every request_navigation call (any import alias, or state.request_navigation)
    whose page is a string literal. A computed page (the jump box's own options, a C9 return) is the
    caller's job to derive from the viewer's pages."""
    aliases = {"request_navigation"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.state":
            aliases.update(a.asname or a.name for a in node.names if a.name == "request_navigation")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        named = (isinstance(f, ast.Name) and f.id in aliases) or (
            isinstance(f, ast.Attribute) and f.attr == "request_navigation")
        first = node.args[0]
        if named and isinstance(first, ast.Constant) and isinstance(first.value, str):
            yield node, first.value


def _is_gate(expr: ast.AST, target: str, names: set[str]) -> bool:
    """can_open("<target>"), "<target>" in <pages>, or a name bound to one of those."""
    if isinstance(expr, ast.Call) and expr.args and isinstance(expr.args[0], ast.Constant):
        f = expr.func
        fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        return fname == "can_open" and expr.args[0].value == target
    if isinstance(expr, ast.Compare) and len(expr.ops) == 1 and isinstance(expr.ops[0], ast.In):
        return isinstance(expr.left, ast.Constant) and expr.left.value == target
    return isinstance(expr, ast.Name) and expr.id in names


def _positive(test: ast.AST, target: str, names: set[str]) -> bool:
    """``test`` true implies the gate is true."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_positive(v, target, names) for v in test.values)
    return _is_gate(test, target, names)


def _negative(test: ast.AST, target: str, names: set[str]) -> bool:
    """``test`` false implies the gate is true (``not gate``, or ``... or not gate``)."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _positive(test.operand, target, names)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_negative(v, target, names) for v in test.values)
    return False


def _gate_names(tree: ast.AST, target: str) -> set[str]:
    return {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign) and _is_gate(node.value, target, set())
            for t in node.targets if isinstance(t, ast.Name)}


def _covered(node: ast.AST, target: str, parents: dict, names: set[str], seen: frozenset = frozenset()) -> bool:
    """Is ``node`` reachable only when the gate for ``target`` holds? An enclosing if/else on the gate, an
    earlier ``if not gate: ... return`` in an enclosing block, or (for a call inside a function) the gate
    covering every use of that function in its scope."""
    child, cur = node, parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.If):
            if child in cur.body and _positive(cur.test, target, names):
                return True
            if child in cur.orelse and _negative(cur.test, target, names):
                return True
        if isinstance(cur, ast.IfExp):
            if child is cur.body and _positive(cur.test, target, names):
                return True
            if child is cur.orelse and _negative(cur.test, target, names):
                return True
        if (isinstance(cur, ast.BoolOp) and isinstance(cur.op, ast.And) and child in cur.values
                and any(_positive(v, target, names) for v in cur.values[:cur.values.index(child)])):
            return True
        for field in ("body", "orelse", "finalbody"):
            block = getattr(cur, field, None)
            if isinstance(block, list) and child in block:
                for prev in block[:block.index(child)]:
                    if (isinstance(prev, ast.If) and _negative(prev.test, target, names) and prev.body
                            and isinstance(prev.body[-1], (ast.Return, ast.Raise))):
                        return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if cur in seen:
                return False
            scope = parents.get(cur)
            while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                scope = parents.get(scope)
            inside = {id(n) for n in ast.walk(cur)}
            refs = [n for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == cur.name
                    and isinstance(n.ctx, ast.Load) and id(n) not in inside] if scope is not None else []
            return bool(refs) and all(_covered(r, target, parents, names, seen | {cur}) for r in refs)
        child, cur = cur, parents.get(cur)
    return False


def _ungated_doorways(source: str, label: str, monitor: set[str]) -> tuple[list[str], int]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bad, checked = [], 0
    for call, target in _literal_nav_calls(tree):
        if target in monitor:
            continue
        checked += 1
        if not _covered(call, target, parents, _gate_names(tree, target)):
            bad.append(f"{label}:{call.lineno} -> {target}")
    return bad, checked


def test_the_doorway_lock_is_not_vacuous():
    monitor = set(cfg.PAGES_BY_PROFILE["MONITOR"])
    ungated = (
        "def a(df):\n"
        "    def _open(i):\n"
        "        request_navigation('Control Room', 'Entity 360')\n"
        "    selectable_nav_table(df, key='k', on_select=_open)\n"
        "def b():\n"
        "    from app.core.state import request_navigation as _go\n"
        "    if st.button('x'):\n"
        "        _go('Brief')\n"
        "def c():\n"
        "    if not can_open('Alerts'):\n"
        "        request_navigation('Alerts', 'Rules')\n"        # the gate's polarity is read, not just its name
        "def d():\n"
        "    request_navigation('Operations', 'Queries')\n")      # MONITOR has Operations: never flagged
    bad, checked = _ungated_doorways(ungated, "x", monitor)
    assert checked == 3 and len(bad) == 3, bad
    gated = (
        "def a(df):\n"
        "    if df.empty or not can_open('Control Room'):\n"
        "        styled_table(df)\n"
        "        return\n"
        "    def _open(i):\n"
        "        request_navigation('Control Room', 'Entity 360')\n"
        "    selectable_nav_table(df, key='k', on_select=_open)\n"
        "def b(pages):\n"
        "    if 'Brief' in pages and st.button('x'):\n"
        "        _state.request_navigation('Brief')\n"
        "def c(fp):\n"
        "    ok = can_open('Alerts')\n"
        "    if fp and ok and st.button('y'):\n"
        "        request_navigation('Alerts', 'Rules')\n"
        "def e(df):\n"
        "    def _open(i):\n"
        "        request_navigation('Control Room', 'Entity 360')\n"
        "    if can_open('Control Room'):\n"
        "        selectable_nav_table(df, key='k', on_select=_open)\n"
        "    else:\n"
        "        styled_table(df)\n")
    bad, checked = _ungated_doorways(gated, "x", monitor)
    assert checked == 4 and bad == [], bad


def test_monitor_pages_offer_no_doorway_to_a_page_monitor_lacks():
    """Every literal request_navigation toward a page MONITOR lacks, anywhere on MONITOR's surface (the shell,
    the shared UI and the two pages), sits behind a gate on that page (can_open, or a membership test on the
    viewer's pages). This covers row drills as well as buttons: entity_nav_table and the task-graph tables
    once sent a MONITOR viewer on Operations to Cost Intelligence."""
    monitor = set(cfg.PAGES_BY_PROFILE["MONITOR"])
    bad: list[str] = []
    checked = 0
    for rel in _MONITOR_SURFACE:
        b, c = _ungated_doorways((_ROOT / rel).read_text(encoding="utf-8"), rel, monitor)
        bad += b
        checked += c
    assert bad == [], bad
    # the Control Room drills (components, operations x2, optimize, optimize_queue x2), Proof, Brief, Alerts
    assert checked >= 9, checked


# ---------------------------------------------------------------------------
# Holistic 4.610 #14: no PROSE on MONITOR's pages points at a page MONITOR cannot open, unless it sits behind the
# same gate as a doorway (can_open / a membership test). What it covers: a string literal or f-string part (not a
# comment or docstring) in a _MONITOR_SURFACE file that names Control Room, Proof, Brief, Alerts, Security or
# Overview in one of the _PAGE_POINTER_FORMS ('X ▸', 'X >', 'X →', 'X ->', 'on X', 'in X', 'on the X page',
# and '**Proof**'); a page named any other way is not seen. Admin is not in the table on purpose: prose naming
# where an admin sets something ("an admin can check Admin → Migrations & freshness") is allowed, since it says
# who acts and sends no viewer anywhere.
# ---------------------------------------------------------------------------
_PAGE_POINTER_FORMS: tuple[str, ...] = ("{p} ▸", "{p} >", "{p} →", "{p} ->", "on {p}", "in {p}",
                                        "on the {p} page")
_PAGE_POINTERS: dict[str, tuple[str, ...]] = {
    page: tuple(form.format(p=page) for form in _PAGE_POINTER_FORMS) + extra
    for page, extra in (("Control Room", ()), ("Proof", ("**Proof**",)), ("Brief", ()), ("Alerts", ()),
                        ("Security", ()), ("Overview", ()))
}
#: Pointers already reachable only where their page opens, by a gate the static walk cannot see (a default
#: argument used only by a gated caller, or a whole function rendered only behind can_open).
_PROSE_WAIVED: dict[str, str] = {
    "Snapshot this evidence into the session Case File (see Brief ▸ Operator Case File).":
        "add_to_case_button returns before rendering unless can_open('Brief')",
    "Select a row to open it in Control Room ▸ Entity 360.":
        "row_select_hint's default; entity_nav_table shows it only behind can_open('Control Room')",
}


def _string_parts(tree: ast.AST):
    """(node, text) for every string literal and f-string literal part (docstrings excluded)."""
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node, node.value


def _ungated_prose(source: str, label: str, monitor: set[str]) -> tuple[list[str], int]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bad, checked = [], 0
    for node, text in _string_parts(tree):
        for page, marks in _PAGE_POINTERS.items():
            if page in monitor or not any(m in text for m in marks):
                continue
            checked += 1
            if text in _PROSE_WAIVED:
                continue
            # an f-string part is covered through its JoinedStr parent
            anchor = parents.get(node) if isinstance(parents.get(node), ast.JoinedStr) else node
            if not _covered(anchor, page, parents, _gate_names(tree, page)):
                bad.append(f"{label}:{node.lineno} -> {page}: {text[:60]!r}")
    return bad, checked


def test_the_prose_lock_is_not_vacuous():
    monitor = set(cfg.PAGES_BY_PROFILE["MONITOR"])
    src = (
        "def a():\n"
        "    st.caption('Totals live on **Proof**.')\n"
        "def b():\n"
        "    st.caption('register owners on Control Room ▸ Entity 360' if can_open('Control Room') else 'x')\n"
        "def c():\n"
        "    if can_open('Proof'):\n"
        "        st.caption('Totals live on **Proof**.')\n"
        "def d():\n"
        "    st.caption('see Operations ▸ Queries')\n"
        "def e():\n"
        "    st.caption('the live rule may be tuned in Alerts > Rules')\n"
        "def f():\n"
        "    st.caption('(threshold on the Alerts page)' if can_open('Alerts') else 'an admin sets it')\n")
    bad, checked = _ungated_prose(src, "x", monitor)
    assert checked == 5 and len(bad) == 2, bad
    assert "x:2 -> Proof" in bad[0] and "x:11 -> Alerts" in bad[1], bad


def test_monitor_pages_name_no_page_monitor_cannot_open():
    monitor = set(cfg.PAGES_BY_PROFILE["MONITOR"])
    bad: list[str] = []
    checked = 0
    for rel in _MONITOR_SURFACE:
        b, c = _ungated_prose((_ROOT / rel).read_text(encoding="utf-8"), rel, monitor)
        bad += b
        checked += c
    assert bad == [], bad
    # 11 today: the Savings Proof caption, the three Entity 360 pointers on Operations, the two Alerts pointers on
    # AI chargeback, the Overview pointer on Contract, the shell's Case File pointers and the two waived components
    assert checked >= 10, checked
    for text in _PROSE_WAIVED:                       # a waiver for a string that is gone must be dropped
        assert any(text in (_ROOT / rel).read_text(encoding="utf-8") for rel in _MONITOR_SURFACE), text
