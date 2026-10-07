"""Snowflake session management.

SiS-first: get_active_session() (the app owner's rights for every viewer) with a
st.connection("snowflake") fallback for local dev. Query tag and statement
timeout are tracked as attributes ON the session object — a recycled
connection can never inherit stale session_state flags (old-app finding M4).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping

import streamlit as st

from app.config import APP_QUERY_TAG_PREFIX

_TAG_ATTR = "_ow_query_tag"
_TIMEOUT_ATTR = "_ow_stmt_timeout"
_ALTER_SUPPORT_ATTR = "_ow_alter_session_supported"  # None unknown / True / False
_SIS_ATTR = "_ow_is_sis"
_TAG_MAX = 200
_PARAMS_ATTR = "_ow_stmt_params_ok"  # False once this Snowpark rejects statement_params
# Next-Fifty #7 Slice B: tiers whose per-tier STATEMENT_TIMEOUT_IN_SECONDS also rides statement_params
# on SiS. The owner probe (2026-09-24) proved an owner's-rights proc honors it, but the READ tiers'
# 30/120/180s ceilings have never been enforced in production (the real wall is WH_ALFA_ADMIN's
# STATEMENT_TIMEOUT_IN_SECONDS, read live on Admin > Performance: V002 set 300 s at install, but this
# account's cancels there fire at 1800 s; Snowflake's own default is 172800 s),
# so they stay off until per-tier durations are measured (APP_QUERY_TELEMETRY records the tier
# and QUERY_ID of every slow read). Cortex's 90s ceiling is the documented intent for an explicit,
# spinner-backed button (core.ai). Whether SiS honors it is unverified: SiS overrides the QUERY_TAG.
STATEMENT_PARAMS_TIMEOUT_TIERS: frozenset[str] = frozenset({"cortex"})


def _sanitize_tag_part(value: object, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9 _&:/.-]+", "", str(value or "")).strip()
    return re.sub(r"\s+", "_", text)[:max_len] or "unknown"


def build_query_tag(page: str = "", tier: str = "") -> str:
    parts = [APP_QUERY_TAG_PREFIX]
    if page:
        parts.append(f"page={_sanitize_tag_part(page)}")
    if tier:
        parts.append(f"tier={_sanitize_tag_part(tier, 20)}")
    return "|".join(parts)[:_TAG_MAX]


def statement_params(session, *, page: str, tier: str, timeout_s: int | None = None) -> dict[str, str] | None:
    """Per-statement QUERY_TAG for owner's-rights SiS, where ALTER SESSION is rejected (Next-Fifty #7
    Slice B). Rides the statement's own request - no extra statement, unlike the per-query ALTER SESSION
    that was declined. The owner probe (2026-09-24) proved an owner's-rights proc records it, BUT the
    post-deploy diagnostic showed Streamlit-in-Snowflake overrides it with its own app tag on every
    statement - so self-traffic keys on SiS's tag (common.app_self_sql), and this tag only lands off-SiS
    or wherever Snowflake stops overriding it. Kept: harmless, and it carries Cortex's timeout.
    Off-SiS returns None: the ALTER SESSION path already tags there, and test fakes stay untouched.
    STATEMENT_TIMEOUT_IN_SECONDS is added only for STATEMENT_PARAMS_TIMEOUT_TIERS."""
    if session is None or not getattr(session, _SIS_ATTR, False) or getattr(session, _PARAMS_ATTR, None) is False:
        return None
    params = {"QUERY_TAG": build_query_tag(page=page, tier=tier)}
    if timeout_s and tier in STATEMENT_PARAMS_TIMEOUT_TIERS:
        params["STATEMENT_TIMEOUT_IN_SECONDS"] = str(max(10, min(int(timeout_s), 900)))
    return params


def _params_rejected(session, exc: Exception) -> bool:
    """A Snowpark too old for statement_params raises TypeError naming it at SUBMIT (nothing ran yet):
    remember that on the session (one rejection, not one per query) so the caller resubmits untagged.
    Any other TypeError is a real failure and propagates."""
    if isinstance(exc, TypeError) and "statement_params" in str(exc):
        setattr(session, _PARAMS_ATTR, False)
        return True
    return False


def submit_pandas(session, statement, params: dict[str, str] | None, **kwargs):
    """statement.to_pandas(**kwargs), carrying statement_params when there are any."""
    if params:
        try:
            return statement.to_pandas(statement_params=params, **kwargs)
        except TypeError as exc:
            if not _params_rejected(session, exc):
                raise
    return statement.to_pandas(**kwargs)


def submit_collect(session, statement, params: dict[str, str] | None, *, nowait: bool = False):
    """statement.collect() / collect_nowait(), carrying statement_params when there are any. A missing
    collect_nowait (older Snowpark) still raises AttributeError for the caller's blocking fallback."""
    submit = statement.collect_nowait if nowait else statement.collect
    if params:
        try:
            return submit(statement_params=params)
        except TypeError as exc:
            if not _params_rejected(session, exc):
                raise
    return submit()


@st.cache_resource(show_spinner=False)
def _connect():
    """One Snowpark session per server process/user context."""
    try:
        from snowflake.snowpark.context import get_active_session

        session = get_active_session()  # Streamlit-in-Snowflake
        # SiS executes inside an owner's-rights procedure where ALTER SESSION
        # raises "Unsupported statement type 'ALTER_SESSION'". Mark it up
        # front so we never spray failed statements into QUERY_HISTORY.
        # Consequence (#31): the per-tier STATEMENT_TIMEOUT the app tries to set
        # via apply_statement_timeout() cannot take effect here — the warehouse/
        # account STATEMENT_TIMEOUT_IN_SECONDS (the warehouse value, read live on Admin >
        # Performance; V002 set 300 s at install; Snowflake's own default is 172800 s) is the
        # ceiling in production. Next-Fifty #7 Slice B: the QUERY_TAG (and, for the tiers in
        # STATEMENT_PARAMS_TIMEOUT_TIERS, the timeout) now ride each statement via
        # statement_params() instead.
        setattr(session, _SIS_ATTR, True)
        setattr(session, _ALTER_SUPPORT_ATTR, False)
        return session
    except Exception:
        pass
    conn = st.connection("snowflake")  # local dev secrets; raises if absent
    return conn.session()


def get_session():
    """Return the session, creating it if needed. Raises when unreachable."""
    session = _connect()
    _apply_base_parameters(session)
    return session


def get_cached_session():
    """Session if one already exists and is healthy enough for best-effort
    writes (error sink); returns None instead of raising."""
    try:
        return _connect()
    except Exception:
        return None


def connection_error() -> str:
    """Why the connection attempt failed ('' when connected).

    Surfaced on the not-connected screen so local-dev setup problems are
    diagnosable without digging into logs (wrong account, bad key, no
    secrets.toml section, network) — the message is shown in an expander.
    """
    try:
        get_session()
        return ""
    except Exception as exc:
        return str(exc)[:500]


def connection_available() -> bool:
    try:
        get_session()
        return True
    except Exception:
        return False


def alter_session_supported(session) -> bool:
    """Whether this runtime accepts ALTER SESSION (SiS does not)."""
    return getattr(session, _ALTER_SUPPORT_ATTR, None) is not False


def _try_alter_session(session, statement: str) -> bool:
    """Run one ALTER SESSION, learning the runtime's capability exactly once.

    On the first failure the session object is marked unsupported and no
    ALTER SESSION is ever attempted again — one failed probe maximum, not a
    failed statement per query (the SiS screenshots that motivated this fix).
    """
    if not alter_session_supported(session):
        return False
    try:
        session.sql(statement).collect()
        setattr(session, _ALTER_SUPPORT_ATTR, True)
        return True
    except Exception:
        setattr(session, _ALTER_SUPPORT_ATTR, False)
        return False


def _apply_base_parameters(session) -> None:
    if getattr(session, _TAG_ATTR, None) is None:
        # correctness #22: set the session TIMEZONE to America/Chicago so a
        # non-SiS run (local dev, tests, off-SiS deploys) agrees with
        # formulas.account_today()'s Chicago basis instead of drifting on a UTC
        # clock. Harmless in SiS — ALTER SESSION is a no-op there (owner's-rights
        # rejects it), so this changes nothing for the live app. NOTE for the
        # owner: for the SiS path, set the ACCOUNT default TIMEZONE to
        # America/Chicago (ALTER ACCOUNT SET TIMEZONE='America/Chicago') — that is
        # the only lever that moves the SiS session clock.
        applied = _try_alter_session(
            session,
            f"ALTER SESSION SET QUERY_TAG = '{APP_QUERY_TAG_PREFIX}', TIMEZONE = 'America/Chicago'",
        )
        setattr(session, _TAG_ATTR, APP_QUERY_TAG_PREFIX if applied else "")


def apply_query_tag(session, tag: str) -> None:
    """Set QUERY_TAG only when it changes; no-op where ALTER SESSION is unsupported."""
    if not alter_session_supported(session):
        return
    tag = (tag or APP_QUERY_TAG_PREFIX)[:_TAG_MAX]
    if getattr(session, _TAG_ATTR, None) == tag:
        return
    safe = tag.replace("'", "''")
    if _try_alter_session(session, f"ALTER SESSION SET QUERY_TAG = '{safe}'"):
        setattr(session, _TAG_ATTR, tag)


def apply_statement_timeout(session, seconds: int) -> None:
    """Per-tier session statement timeout — INEFFECTIVE on the owner's-rights SiS path.

    #31: ALTER SESSION is rejected under owner's-rights SiS, so the app's per-tier
    timeouts (30/120/180s) never actually apply there. Every app READ is instead
    governed by the warehouse/account STATEMENT_TIMEOUT_IN_SECONDS — the warehouse value
    (read live on Admin > Performance; V002 set 300 s at install, a DBA may have changed it
    since; Snowflake's own default is 172800 s) — which is the REAL contract in production,
    not the values passed here.
    To enforce a tighter ceiling the OWNER must SET STATEMENT_TIMEOUT_IN_SECONDS on
    the app warehouse (or account). Follow-up: a QUERY_HISTORY monitor on long
    app-tagged queries (the APP_QUERY_TAG_PREFIX QUERY_TAG) to catch anything
    approaching that warehouse ceiling. This call still does real work OFF-SiS (local dev,
    tests) where ALTER SESSION is accepted. Next-Fifty #7 Slice B: on SiS the timeout can
    ride the statement itself (statement_params), per tier in STATEMENT_PARAMS_TIMEOUT_TIERS:
    Cortex is on now (its documented 90s intent); a read tier joins once its tagged
    durations are measured (the post-deploy check).
    """
    if not alter_session_supported(session):
        return
    seconds = max(10, min(int(seconds), 900))
    if getattr(session, _TIMEOUT_ATTR, None) == seconds:
        return
    if _try_alter_session(session, f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}"):
        setattr(session, _TIMEOUT_ATTR, seconds)


def current_role() -> str:
    """CURRENT_ROLE for navigation profiles; cached per Streamlit session.

    The same probe captures CURRENT_USER for the query-cache scope: caching
    by role alone let two users who share a role serve each other's
    user-scoped frames (USER_PREFS saved views) for a TTL window.
    """
    cached = st.session_state.get("_ow_current_role")
    if cached is not None:
        return str(cached)
    try:
        _s = get_session()
        rows = submit_collect(_s, _s.sql("SELECT CURRENT_ROLE() AS R, CURRENT_USER() AS U"),
                              statement_params(_s, page="session", tier="metadata"))
        role = str(rows[0]["R"] or "").upper() if rows else ""
        user = str(rows[0]["U"] or "").upper() if rows else ""
    except Exception:
        # Transient failure: return unknown WITHOUT pinning it. Caching ""
        # here locked the whole session into the ANALYST fallback profile
        # (and dropped role from the cache scope) after one bad probe.
        return ""
    st.session_state["_ow_current_role"] = role
    st.session_state["_ow_current_user"] = user
    return role


def is_operator() -> bool:
    """In-app operator entitlement, resolved from the VIEWER identity (correctness #3).

    Under owner's-rights Streamlit-in-Snowflake, SQL CURRENT_ROLE() is the app
    OWNER's role for EVERY viewer, so gating operator UI/actions on
    ``resolve_role_profile(current_role()) in OPERATOR_PROFILES`` never
    differentiates people — one accidental app grant would expose DBA actions to
    any viewer. Entitle from st.user (the actual viewer) instead, through
    viewer_access(): the config.OPERATOR_USERS allowlist, or (v4.610.0, owner
    decision 2026-10-05) a DIRECT user grant of config.ADMIN_ACCESS_ROLE looked up
    live as the owner.

    Snowflake RBAC is NOT a backstop here: because every viewer executes with the
    app OWNER's privileges, an operator write does NOT fail server-side for an
    under-privileged viewer — it runs as the owner. This resolution is therefore
    the application authorization boundary, not merely "what the app offers". The
    query executors re-check it for every privileged statement
    (query._entitlement_refusal), and a role-sourced admin's write re-verifies the
    membership live (reverify_role_admin), so a revoke stops writes within
    config.WRITE_RECHECK_S even while the session's page memo still lists DBA pages.

    Off-SiS (local dev, tests, older runtimes) st.user is absent so
    viewer_name() == "": fall back to the role->profile check there, so local
    development and AppTest keep working exactly as before.
    """
    from app.config import OPERATOR_PROFILES, resolve_role_profile

    if _viewer():
        return bool(viewer_access()["operator"])
    # On SiS with NO resolvable viewer identity, FAIL CLOSED (mirrors active_profile()):
    # every viewer executes with the owner's role, so the role check below would treat an
    # unidentified SiS viewer as the owner-operator — a write escalation. Off-SiS (local
    # dev, tests, older runtimes) there is no owner's-rights ambiguity, so the role check
    # is safe and preserves existing behavior.
    if is_sis():
        return False
    return resolve_role_profile(current_role()) in OPERATOR_PROFILES


def is_sis() -> bool:
    """True when running inside owner's-rights Streamlit-in-Snowflake.

    Reads the marker _connect() stamps on the SiS session. Built on the
    None-safe cached-session accessor so it never raises and never forces a NEW
    connection — off-SiS (local dev, tests, older runtimes) it returns False.
    """
    session = get_cached_session()
    return bool(getattr(session, _SIS_ATTR, False)) if session is not None else False


def active_profile(role: str = "") -> str:
    """Navigation profile (page visibility) for the current VIEWER — the read
    analog of is_operator().

    Under owner's-rights SiS, SQL CURRENT_ROLE() is the app OWNER's role for
    EVERY viewer, so page visibility must key on the viewer identity (st.user),
    not the role. An identified viewer resolves through viewer_access(): an admin
    (OPERATOR_USERS or a direct ADMIN_ACCESS_ROLE grant) gets DBA; anyone else an
    explicit non-admin VIEWER_PROFILES pin or the view-only
    VIEWER_UNKNOWN_PROFILE (MONITOR), NEVER the owner's DBA. When no viewer
    identity is available, distinguish SiS (fail CLOSED to NO_IDENTITY_PROFILE —
    an unresolved SiS viewer must never inherit the owner's DBA surface) from
    off-SiS (local dev/tests: fall back to the role->profile map, preserving
    today's behavior exactly).
    """
    from app.config import NO_IDENTITY_PROFILE, resolve_role_profile

    if _viewer():
        return str(viewer_access()["profile"])
    if is_sis():
        return NO_IDENTITY_PROFILE
    return resolve_role_profile(role or current_role())


# ---------------------------------------------------------------------------
# Viewer access resolution (v4.610.0, owner decision 2026-10-05).
#
# For an identified viewer, first match wins:
#   1. config.OPERATOR_USERS            -> DBA + operator, source 'allowlist' (no SQL at all)
#   2. a DIRECT user grantee of config.ADMIN_ACCESS_ROLE (SHOW GRANTS OF ROLE, run as the owner; once V175
#      is applied, CALL SP_ADMIN_ROLE_MEMBERS(), the same SHOW run as the procedure's owner)
#                                        -> DBA + operator, source 'role'
#   3. otherwise                         -> a non-admin VIEWER_PROFILES pin or VIEWER_UNKNOWN_PROFILE
#      (MONITOR), read-only; source 'default' when the lookup answered, 'lookup_failed' when it raised,
#      'unverified' when it listed no USER grantee (SHOW shows only what the owner can see, so an empty
#      answer is a privilege gap, never proof of "no members").
# FAIL CLOSED: no error, empty answer or revoke ever grants admin. VIEW_ACCESS_ROLE is never looked up:
# only a role holding USAGE on the app can open it (the four config.APP_ACCESS_ROLES, which
# snowflake/roles.sql grants and proves since 4.610.1), so every identified non-admin is a DTI member or
# a SNOW_* holder and gets the view-only default.
#
# MEMBERSHIP IS EXACT: the viewer's st.user name is compared with SHOW's grantee_name as stored, with no
# case folding (holistic 4.610 #1: a user named "jdoe" must never ride a DSA grant to user JDOE). The
# upper-cased _viewer() form keys the memos and the hand-typed OPERATOR_USERS / VIEWER_PROFILES lookups.
#
# One st.session_state memo per viewer session (the warehouse runtime gives each viewer a personal app
# instance, so nothing is shared between viewers): a resolved answer is re-resolved after
# ACCESS_TTL_S; a failed / empty lookup is retried after config.access_retry_s(n), ACCESS_RETRY_S after
# the first consecutive failure and doubling up to ACCESS_TTL_S (every open session runs the lookup, so a
# long outage must not cost one SHOW per session per minute). A failure writes ONE APP_ERROR_LOG row per
# session, and every distinct (viewer, source) writes one APP_USAGE 'access_resolved' event naming the
# source (the audit of who held admin through the role).
# ---------------------------------------------------------------------------
_ACCESS_KEY = "_ow_access"             # {viewer, profile, operator, source, at, error}
_ROSTER_KEY = "_ow_access_roster"      # {status, users, roles, at, error} — the last admin-role lookup
_RECHECK_KEY = "_ow_access_recheck"    # {viewer, at} — the last POSITIVE write-time re-verification
_ACCESS_ERR_LOGGED_KEY = "_ow_access_err_logged"
_ACCESS_EVENTS_KEY = "_ow_access_events"
_LOOKUP_SQL_KEY = "_ow_access_lookup_sql"   # the statement the last admin-role lookup ran (SHOW, or V175's CALL)
#: The sources an identified viewer's resolution carries. An unidentified viewer reads 'no_identity'
#: on SiS and 'off_sis' elsewhere (no memo, no lookup).
ACCESS_SOURCES: tuple[str, ...] = ("allowlist", "role", "default", "lookup_failed", "unverified")
#: Sources that mean the admin-role lookup could not answer: read-only until it recovers (sidebar caption).
ACCESS_UNAVAILABLE_SOURCES: tuple[str, ...] = ("lookup_failed", "unverified")
_USAGE_PREFIX = ("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE "
                 "(PAGE, SECTION, RENDER_MS, EVENT_KIND, IS_RERUN, USER_NAME) ")


def _clock() -> float:
    """Wall-clock seconds for the access memos (a seam: tests drive the TTLs)."""
    return time.time()


def _viewer() -> str:
    """The viewer's username, upper-cased ('' when unidentified): the memo key and the form the hand-typed
    OPERATOR_USERS / VIEWER_PROFILES lookups fold to. Module-attribute lookup on identity is the seam the
    tests monkeypatch."""
    return _viewer_exact().upper()


def _viewer_exact() -> str:
    """The viewer's username exactly as st.user gives it (stripped, never case-folded): the only form the
    admin-role membership check compares with SHOW GRANTS OF ROLE's grantee_name."""
    from app.core import identity as _identity

    return str(_identity.viewer_name() or "").strip()


def _fresh(memo: object, ttl: float) -> bool:
    if not isinstance(memo, dict):
        return False
    try:
        age = _clock() - float(memo.get("at") or 0.0)
    except (TypeError, ValueError):
        return False
    return 0 <= age < ttl          # a clock that stepped backwards re-resolves


def _row_mapping(row: object) -> dict:
    if isinstance(row, Mapping):
        raw = dict(row)
    elif hasattr(row, "asDict"):
        raw = row.asDict()
    else:
        raw = dict(row)  # type: ignore[call-overload]
    return {str(k).strip().strip('"').lower(): v for k, v in raw.items()}


def role_grant_members(rows: object) -> tuple[frozenset[str], tuple[str, ...]]:
    """(direct USER grantees, ROLE grantees) of a SHOW GRANTS OF ROLE answer, names exactly as stored.

    Accepts Snowpark Rows, mappings, or a DataFrame (a run(..., max_rows=0) read on Admin ▸ App access).
    Only granted_to = 'USER' rows are members; a ROLE grantee is reported (nested, NOT expanded) and any
    other grantee kind is ignored. Names are stripped, never case-folded: membership is an exact match with
    the viewer's st.user name (two users whose names differ only by case are different users). Pure."""
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    users: set[str] = set()
    roles: set[str] = set()
    for row in rows or ():  # type: ignore[attr-defined]
        m = _row_mapping(row)
        name = str(m.get("grantee_name") or "").strip()
        kind = str(m.get("granted_to") or "").strip().upper()
        if not name:
            continue
        if kind == "USER":
            users.add(name)
        elif kind == "ROLE":
            roles.add(name)
    return frozenset(users), tuple(sorted(roles))


def _admin_lookup_sql() -> str:
    """The admin-access lookup statement. Once V175 is applied: CALL SP_ADMIN_ROLE_MEMBERS(), which runs
    SHOW GRANTS OF ROLE <ADMIN_ACCESS_ROLE> as the PROCEDURE's owner, so it answers the same whatever role owns
    the app (owner decision 2026-10-06: SNOW_SYSADMINS will). Before it: that SHOW, as the app owner.

    The gate answers from the startup schema read (no statement of its own) and is False when SCHEMA_VERSION
    is unreadable: SHOW then, the pre-V175 behaviour. Either statement fails closed the same way."""
    from app.config import ADMIN_ACCESS_ROLE
    from app.data import access_sql

    try:
        from app.ui import schema_gate  # lazy: schema_gate imports app.core.query, which imports this module

        via_proc = schema_gate.has_migration(access_sql.ADMIN_MEMBERS_MIGRATION, "session")
    except Exception:
        via_proc = False
    if via_proc:
        return access_sql.call_admin_role_members_sql()
    return access_sql.show_grants_of_role_sql(ADMIN_ACCESS_ROLE)


def _lookup_label() -> str:
    """The statement the last admin-role lookup ran, for its error log and Admin ▸ App access."""
    from app.config import ADMIN_ACCESS_ROLE
    from app.data.access_sql import show_grants_of_role_sql

    return str(st.session_state.get(_LOOKUP_SQL_KEY) or show_grants_of_role_sql(ADMIN_ACCESS_ROLE))


def _admin_role_rows() -> list:
    """The live admin-role answer (_admin_lookup_sql: V175's CALL, else SHOW GRANTS OF ROLE as the app owner).
    Raises on any failure (the callers fail closed). Not through run(): its tier cache would outlive the
    access TTLs and the write-time re-check must be fresh. Not through the write executor either: the CALL
    is a read, and it is what decides admin entitlement."""
    sql = _admin_lookup_sql()
    st.session_state[_LOOKUP_SQL_KEY] = sql
    s = get_session()
    return list(submit_collect(s, s.sql(sql), statement_params(s, page="session", tier="metadata")) or [])


def _log_access_failure(exc: BaseException) -> None:
    """ONE APP_ERROR_LOG row per session for a failed or empty admin-role lookup (the sidebar and
    Admin ▸ App access say so on every run; the log needs it once). Never raises."""
    if st.session_state.get(_ACCESS_ERR_LOGGED_KEY):
        return
    st.session_state[_ACCESS_ERR_LOGGED_KEY] = True
    try:
        from app.core import errors as _errors

        _errors.record_error("Access", exc, context=(
            f"admin-access lookup ({_lookup_label()}) failed or listed no user: "
            "viewers not on OPERATOR_USERS resolve read-only until it recovers"))
    except Exception:
        pass


def _admin_roster(*, fresh: bool = False) -> dict:
    """The memoized admin-role lookup: {status: ok | unverified | lookup_failed, users, roles, at, error,
    fails}. ``fails`` counts consecutive failed / empty lookups (0 after a good one) and sets the retry
    backoff (config.access_retry_s). ``fresh`` bypasses the memo (the write-time re-check). Never raises."""
    from app.config import ACCESS_TTL_S, ADMIN_ACCESS_ROLE, access_retry_s

    memo = st.session_state.get(_ROSTER_KEY)
    if not fresh and isinstance(memo, dict) and _fresh(
            memo, ACCESS_TTL_S if memo.get("status") == "ok" else access_retry_s(memo.get("fails"))):
        return memo
    prior = memo if isinstance(memo, dict) else {}
    try:
        streak = int(prior.get("fails") or 0) if prior.get("status") in ("lookup_failed", "unverified") else 0
    except (TypeError, ValueError):
        streak = 0
    now = _clock()
    try:
        users, roles = role_grant_members(_admin_role_rows())
    except Exception as exc:   # fail CLOSED: a failed lookup never makes an admin
        try:
            detail = str(exc)[:300]
        except Exception:
            detail = type(exc).__name__
        memo = {"status": "lookup_failed", "users": (), "roles": (), "at": now, "error": detail,
                "fails": streak + 1}
        _log_access_failure(exc)
    else:
        if users:
            memo = {"status": "ok", "users": tuple(sorted(users)), "roles": roles, "at": now, "error": "",
                    "fails": 0}
        else:
            msg = (f"{_lookup_label()} listed no USER grantee of {ADMIN_ACCESS_ROLE}: a privilege gap or an "
                   "empty role, treated as unverified (read-only), never as 'no members'")
            memo = {"status": "unverified", "users": (), "roles": roles, "at": now, "error": msg,
                    "fails": streak + 1}
            _log_access_failure(RuntimeError(msg))
    st.session_state[_ROSTER_KEY] = memo
    return memo


def _log_access_event(access: dict) -> None:
    """One APP_USAGE 'access_resolved' row per (viewer, source) per session, SECTION = the source. The
    prefix is byte-identical to components.log_ui_event's, so it rides the same buffered INSERT; it
    honours the usage off / old-shape switches. Never raises."""
    try:
        seen = st.session_state.setdefault(_ACCESS_EVENTS_KEY, [])
        key = f"{access.get('viewer')}|{access.get('source')}"
        if key in seen:
            return
        seen.append(key)
        if st.session_state.get("_ow_usage_off") or st.session_state.get("_ow_usage_oldshape"):
            return
        from app.core.identity import identity_sql
        from app.core.query import _buffer_write
        from app.core.sqlsafe import sql_literal

        _buffer_write(
            _USAGE_PREFIX,
            f"SELECT {sql_literal('Access')}, {sql_literal(str(access.get('source') or '')[:80])}, NULL, "
            f"{sql_literal('access_resolved')}, FALSE, {identity_sql()}",
            off_flag="_ow_usage_off", downgrade_flag="_ow_usage_oldshape")
    except Exception:
        pass


def _resolve_identified(name: str, exact: str = "") -> tuple[dict, bool]:
    """(access, memoize). ``name`` is the upper-cased viewer (allowlist / pin lookups, memo key); ``exact``
    is the st.user name as given, the only form compared with the admin-role roster (default: ``name``).
    The off-SiS answer is never memoized: is_sis() is also False on a DISCONNECTED SiS run
    (get_cached_session() is None), and that pure 'default' must not outlive the outage."""
    from app.config import (
        OPERATOR_PROFILES,
        VIEWER_UNKNOWN_PROFILE,
        access_retry_s,
        is_operator_user,
        resolve_viewer_profile,
    )

    admin = OPERATOR_PROFILES[0]
    base = {"viewer": name, "at": _clock(), "error": ""}
    if is_operator_user(name):           # break-glass: the named admins never wait on a lookup
        return {**base, "profile": admin, "operator": True, "source": "allowlist"}, True
    view_profile = resolve_viewer_profile(name) or VIEWER_UNKNOWN_PROFILE
    if not is_sis():
        # Off-SiS (local dev, tests) there is no owner's-rights session to look the role up as.
        return {**base, "profile": view_profile, "operator": False, "source": "default"}, False
    roster = _admin_roster()
    # the answer is as old as the lookup behind it, so the memo expires with the data
    base["at"] = roster["at"]
    if roster["status"] == "ok":
        if (exact or name) in roster["users"]:     # exact, case-sensitive (holistic 4.610 #1)
            return {**base, "profile": admin, "operator": True, "source": "role"}, True
        return {**base, "profile": view_profile, "operator": False, "source": "default"}, True
    # the page memo waits out the same backoff as the roster memo behind it
    return {**base, "profile": view_profile, "operator": False, "source": roster["status"],
            "error": roster["error"], "retry_s": access_retry_s(roster.get("fails"))}, True


def viewer_access() -> dict:
    """This viewer's resolved access: {viewer, profile, operator, source, at, error} (a copy).

    Identified viewers are memoized per session (see the block comment above), except the off-SiS answer
    (no lookup ran; see _resolve_identified). An unidentified viewer is never memoized: SiS ->
    NO_IDENTITY_PROFILE, not an operator, source 'no_identity'; off-SiS -> the role->profile map, source
    'off_sis'."""
    from app.config import (
        ACCESS_TTL_S,
        NO_IDENTITY_PROFILE,
        OPERATOR_PROFILES,
        access_retry_s,
        resolve_role_profile,
    )

    name = _viewer()
    if not name:
        if is_sis():
            return {"viewer": "", "profile": NO_IDENTITY_PROFILE, "operator": False,
                    "source": "no_identity", "at": _clock(), "error": ""}
        profile = resolve_role_profile(current_role())
        return {"viewer": "", "profile": profile, "operator": profile in OPERATOR_PROFILES,
                "source": "off_sis", "at": _clock(), "error": ""}
    memo = st.session_state.get(_ACCESS_KEY)
    if isinstance(memo, dict) and memo.get("viewer") == name and _fresh(
            memo, (memo.get("retry_s") or access_retry_s(1))
            if memo.get("source") in ACCESS_UNAVAILABLE_SOURCES else ACCESS_TTL_S):
        return dict(memo)
    memo, keep = _resolve_identified(name, _viewer_exact())
    if keep:
        st.session_state[_ACCESS_KEY] = memo
    else:
        st.session_state.pop(_ACCESS_KEY, None)
    _log_access_event(memo)
    return dict(memo)


def access_source() -> str:
    """How this viewer's access was decided: one of ACCESS_SOURCES, or 'no_identity' / 'off_sis'."""
    if _viewer():
        return str(viewer_access()["source"])
    return "no_identity" if is_sis() else "off_sis"


def reverify_role_admin() -> bool:
    """Write-time re-verification for a ROLE-sourced admin (query._entitlement_refusal calls it on
    every privileged write when access_source() == 'role').

    A FRESH admin-role lookup, memoized only when positive and for at most config.WRITE_RECHECK_S.
    False on a revoke, an empty answer or a failed lookup — and then this session's page memo is
    dropped, so the next call re-resolves (read-only) instead of waiting out ACCESS_TTL_S."""
    from app.config import WRITE_RECHECK_S

    name = _viewer()
    if not name:
        return False
    memo = st.session_state.get(_RECHECK_KEY)
    if isinstance(memo, dict) and memo.get("viewer") == name and _fresh(memo, WRITE_RECHECK_S):
        return True
    st.session_state.pop(_RECHECK_KEY, None)
    roster = _admin_roster(fresh=True)
    if roster["status"] == "ok" and _viewer_exact() in roster["users"]:   # exact, as in _resolve_identified
        st.session_state[_RECHECK_KEY] = {"viewer": name, "at": roster["at"]}
        return True
    st.session_state.pop(_ACCESS_KEY, None)
    return False


def forget_access() -> None:
    """Drop THIS session's access memos (the next call re-resolves with one fresh lookup). The
    warehouse runtime gives every viewer their own session, so this never touches another viewer."""
    for key in (_ACCESS_KEY, _ROSTER_KEY, _RECHECK_KEY):
        st.session_state.pop(key, None)


def access_info(*, roster: bool = False) -> dict:
    """Snapshot for Admin ▸ App access: the viewer's resolved access plus the admin-role roster.

    ``roster=True`` also resolves the ADMIN_ACCESS_ROLE roster (one memoized lookup) when this
    session has none — an allowlisted admin's own resolution never runs it. roster_status is
    'not_checked' until a lookup ran, else ok / unverified / lookup_failed (an empty roster is
    'unverified', never a clean "no members"). ``retry_s`` is the wait before the next lookup while the
    last one failed or listed no user (config.access_retry_s of its consecutive-failure count), else None:
    a good or absent roster has no retry pending (``ttl_s`` is its re-check)."""
    from app.config import (
        ACCESS_TTL_S,
        ADMIN_ACCESS_ROLE,
        OPERATOR_USERS,
        ROLE_ADMIN_ACCOUNT_LEVERS,
        VIEW_ACCESS_ROLE,
        access_retry_s,
    )

    access = viewer_access()
    if roster:
        _admin_roster()
    r = st.session_state.get(_ROSTER_KEY)
    r = r if isinstance(r, dict) else None
    try:
        age = max(0.0, _clock() - float(access.get("at") or 0.0))
    except (TypeError, ValueError):
        age = 0.0
    return {
        "viewer": access.get("viewer", ""),
        "profile": access.get("profile", ""),
        "operator": bool(access.get("operator")),
        "source": access.get("source", ""),
        "error": access.get("error", ""),
        "resolved_at": access.get("at"),
        "age_s": age,
        "admin_role": ADMIN_ACCESS_ROLE,
        "view_role": VIEW_ACCESS_ROLE,
        "account_levers": bool(ROLE_ADMIN_ACCOUNT_LEVERS),
        "allowlist": tuple(OPERATOR_USERS),
        "ttl_s": ACCESS_TTL_S,
        # the backoff actually in force (review: a fixed ACCESS_RETRY_S misstated every retry after the first)
        "retry_s": (access_retry_s(r.get("fails")) if r and r.get("status") in ACCESS_UNAVAILABLE_SOURCES
                    else None),
        "roster_status": str(r.get("status")) if r else "not_checked",
        "admin_users": tuple(r.get("users") or ()) if r else (),
        "nested_roles": tuple(r.get("roles") or ()) if r else (),
        "roster_at": r.get("at") if r else None,
        "roster_error": str(r.get("error") or "") if r else "",
        "roster_lookup": _lookup_label() if r else "",
    }


def recheck_access() -> dict:
    """Admin ▸ App access 'Re-check now': forget THIS session's memos and resolve again (one fresh lookup);
    returns the new access_info(roster=True). Only the clicking viewer's session is affected."""
    forget_access()
    return access_info(roster=True)
