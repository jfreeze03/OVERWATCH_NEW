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

        return get_active_session()  # Streamlit-in-Snowflake
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


def connection_available() -> bool:
    try:
        get_session()
        return True
    except Exception:
        return False


def _apply_base_parameters(session) -> None:
    if getattr(session, _TAG_ATTR, None) is None:
        try:
            session.sql(
                f"ALTER SESSION SET QUERY_TAG = '{APP_QUERY_TAG_PREFIX}', TIMEZONE = 'UTC'"
            ).collect()
            setattr(session, _TAG_ATTR, APP_QUERY_TAG_PREFIX)
        except Exception:
            setattr(session, _TAG_ATTR, "")  # tag is attribution, not correctness


def apply_query_tag(session, tag: str) -> None:
    """Set QUERY_TAG only when it changes; tracked on the session object."""
    tag = (tag or APP_QUERY_TAG_PREFIX)[:_TAG_MAX]
    if getattr(session, _TAG_ATTR, None) == tag:
        return
    try:
        safe = tag.replace("'", "''")
        session.sql(f"ALTER SESSION SET QUERY_TAG = '{safe}'").collect()
        setattr(session, _TAG_ATTR, tag)
    except Exception:
        pass  # attribution only; the query itself still runs


def apply_statement_timeout(session, seconds: int) -> None:
    """Set a bounded statement timeout; tracked on the session object."""
    seconds = max(10, min(int(seconds), 900))
    if getattr(session, _TIMEOUT_ATTR, None) == seconds:
        return
    try:
        session.sql(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}").collect()
        setattr(session, _TIMEOUT_ATTR, seconds)
    except Exception:
        pass  # warehouse-level timeout still applies as the backstop


def current_role() -> str:
    """CURRENT_ROLE for navigation profiles; cached per Streamlit session."""
    cached = st.session_state.get("_ow_current_role")
    if cached is not None:
        return str(cached)
    role = ""
    try:
        rows = get_session().sql("SELECT CURRENT_ROLE() AS R").collect()
        role = str(rows[0]["R"] or "").upper() if rows else ""
    except Exception:
        role = ""
    st.session_state["_ow_current_role"] = role
    return role
