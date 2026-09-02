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


def idempotency_key(kind: str, payload: str) -> str:
    """Deterministic idempotency key for a V051 action proc.

    sha1 of (kind, payload, viewer, minute-bucket): a double-click inside the
    same minute is a DUPLICATE the proc no-ops; the same action a minute later
    is a new intent. account_now() (account time, testable) drives the bucket.
    """
    import hashlib

    from app.logic.formulas import account_now

    minute = account_now().strftime("%Y%m%d%H%M")
    raw = f"{kind}|{payload}|{viewer_name()}|{minute}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def content_request_key(kind: str, payload: str) -> str:
    """Deterministic, TIME-INDEPENDENT idempotency key for a content signature.

    Unlike idempotency_key (which folds a minute bucket for double-click dedup),
    this hashes only (kind, payload, viewer), so the SAME content always maps to
    the SAME key. Use it where a caller passes request_key as a STABLE content
    signature for at-least-once retry idempotency (workbench action lifecycle,
    decision-studio experiment settle): a lost-response retry — even one that
    crosses a minute boundary — then dedups into a no-op instead of writing a
    duplicate audit/comment row. A genuinely different action changes the payload
    and gets a new key. (bug-hunt round 5: the minute bucket silently defeated the
    retry-idempotency the two callers' own comments promised.)
    """
    import hashlib

    raw = f"{kind}|{payload}|{viewer_name()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]
