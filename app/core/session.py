"""Snowflake session management.

SiS-first: get_active_session() (each viewer's own role) with a
st.connection("snowflake") fallback for local dev. Query tag and statement
timeout are tracked as attributes ON the session object — a recycled
connection can never inherit stale session_state flags (old-app finding M4).
"""

from __future__ import annotations

import re

import streamlit as st

from app.config import APP_QUERY_TAG_PREFIX

_TAG_ATTR = "_ow_query_tag"
_TIMEOUT_ATTR = "_ow_stmt_timeout"
_ALTER_SUPPORT_ATTR = "_ow_alter_session_supported"  # None unknown / True / False
_SIS_ATTR = "_ow_is_sis"
_TAG_MAX = 200


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


@st.cache_resource(show_spinner=False)
def _connect():
    """One Snowpark session per server process/user context."""
    try:
        from snowflake.snowpark.context import get_active_session

        session = get_active_session()  # Streamlit-in-Snowflake
        # SiS executes inside an owner's-rights procedure where ALTER SESSION
        # raises "Unsupported statement type 'ALTER_SESSION'". Mark it up
        # front so we never spray failed statements into QUERY_HISTORY.
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
    """Session statement timeout; no-op in SiS (warehouse timeout is the backstop)."""
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
        rows = get_session().sql("SELECT CURRENT_ROLE() AS R, CURRENT_USER() AS U").collect()
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
    any viewer. Entitle from st.user (the actual viewer) checked against the
    explicit config.OPERATOR_USERS allowlist instead. Snowflake RBAC remains the
    REAL boundary — a non-privileged role's write still fails server-side; this
    only decides what the app OFFERS.

    Off-SiS (local dev, tests, older runtimes) st.user is absent so
    viewer_name() == "": fall back to the role->profile check there, so local
    development and AppTest keep working exactly as before.
    """
    from app.config import OPERATOR_PROFILES, is_operator_user, resolve_role_profile
    from app.core.identity import viewer_name

    viewer = viewer_name()
    if viewer:
        return is_operator_user(viewer)
    # No viewer identity (off-SiS): the owner's-rights ambiguity does not apply,
    # so the role-based check is safe and preserves local-dev/test behavior.
    return resolve_role_profile(current_role()) in OPERATOR_PROFILES
