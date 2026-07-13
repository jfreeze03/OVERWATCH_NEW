"""Viewer identity for owner's-rights SiS (r27 #4, Snowflake-doc verified).

In an owner's-rights Streamlit-in-Snowflake app, CURRENT_USER() returns the
app OWNER — st.user is the viewer (both runtimes). Every per-user row
(prefs, usage telemetry, audit actors, verification stamps) goes through
identity_sql() so DBAs stop collapsing into one username. Outside SiS
(local dev, tests, older runtimes) st.user is absent and the expression
falls back to CURRENT_USER().
"""

from __future__ import annotations

import streamlit as st

from app.core.sqlsafe import sql_literal


def viewer_name() -> str:
    """The Snowflake username of the person viewing the app ('' if unknown)."""
    user_obj = getattr(st, "user", None)
    if user_obj is None:
        return ""
    try:
        u = getattr(user_obj, "user_name", None)
    except (AttributeError, KeyError, RuntimeError):
        return ""      # older runtime: st.user unavailable outside SiS auth
    return str(u) if u else ""


def identity_sql() -> str:
    """SQL expression for the viewing user, safe in any runtime."""
    v = viewer_name()
    return sql_literal(v) if v else "CURRENT_USER()"
