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
DTI is never looked up: only the four granted roles can open the app, so every identified viewer who is not
an admin is a DTI member or a SNOW_* holder, and gets MONITOR. An UNIDENTIFIED SiS viewer gets MONITOR and
is never an operator.

Every check here fails CLOSED: a lookup failure, an empty roster, a revoke or an error never grants admin.
"""

from __future__ import annotations

import contextlib

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
    rows = _rows(("USER", "alice"), ("ROLE", "nested_r"), ("USER", " BOB "), ("APPLICATION", "app1"))
    users, roles = sess.role_grant_members(rows)
    assert users == frozenset({"ALICE", "BOB"})
    assert roles == ("NESTED_R",)


def test_role_grant_members_accepts_snowpark_rows_and_quoted_keys_and_frames():
    rows = [_Row({'"granted_to"': "USER", '"grantee_name"': "carol"}),
            _Row({"GRANTED_TO": "ROLE", "GRANTEE_NAME": "R1"})]
    assert sess.role_grant_members(rows) == (frozenset({"CAROL"}), ("R1",))
    df = pd.DataFrame(_rows(("USER", "dave"), ("ROLE", "R2")))
    assert sess.role_grant_members(df) == (frozenset({"DAVE"}), ("R2",))
    assert sess.role_grant_members([]) == (frozenset(), ())


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
    # recovery: the next retry that answers grants the role again
    env.raise_ = None
    env.clock.t += cfg.ACCESS_RETRY_S + 1
    assert sess.viewer_access()["source"] == "role"


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
# Admin ▸ Access API
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
    from pathlib import Path
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


def test_monitor_pages_offer_no_doorway_to_a_page_monitor_lacks():
    """The Cost Intelligence -> Proof doorway was the one ungated cross-page BUTTON on MONITOR's two pages;
    it now renders only where Proof opens. (Row drills toward Control Room are gated or, for MONITOR, inert:
    a clamped jump onto the current page is a no-op.)"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(
        encoding="utf-8")
    assert 'if can_open("Proof") and st.button("Open the proof → Proof", key="savings_roi_link"):' in src
