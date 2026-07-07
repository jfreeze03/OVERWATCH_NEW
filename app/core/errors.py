"""Central error boundary and sink.

Contract (ARCHITECTURE.md): nothing is swallowed invisibly. Every caught
exception is recorded to the in-session ring buffer and best-effort written to
CORE.APP_ERROR_LOG; the Admin page displays both. This module and the other
core runtime modules are the only sanctioned broad-except sites (ruff BLE001
enforces that elsewhere).
"""

from __future__ import annotations

import re
import traceback
from datetime import datetime
from functools import wraps

import streamlit as st

from app.config import core_object

_BUFFER_KEY = "_ow_error_buffer"
_BUFFER_MAX = 100


def format_snowflake_error(error: object, max_len: int = 300) -> str:
    """Short, user-safe rendering of a Snowflake/driver error."""
    text = str(error or "").strip()
    if not text:
        return "Snowflake returned an empty error."
    lower = text.lower()
    if "does not exist or not authorized" in lower or "insufficient privileges" in lower:
        return "The current role cannot access this object. If OVERWATCH setup is new, run the migrations and roles.sql."
    if "invalid identifier" in lower:
        match = re.search(r"invalid identifier '?\"?([A-Za-z0-9_.\"]+)", text)
        ident = match.group(1) if match else "a column"
        return f"This Snowflake edition/account does not expose {ident} here."
    if "timeout" in lower:
        return "The query hit its statement timeout. Narrow the window or filters and retry."
    text = re.sub(r"^\(\d+\):?\s*[0-9a-f-]*:?\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def record_error(page: str, error: BaseException, context: str = "") -> None:
    """Ring-buffer the error and best-effort persist it to the Snowflake sink."""
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "page": str(page)[:80],
        "type": type(error).__name__[:200],
        "message": str(error)[:2000],
        "context": str(context)[:2000],
        "trace": traceback.format_exc(limit=6)[:4000],
    }
    try:
        buffer = st.session_state.setdefault(_BUFFER_KEY, [])
        buffer.append(entry)
        del buffer[:-_BUFFER_MAX]
    except Exception:
        pass  # session not available (import-time failure); nothing else to do

    try:  # best-effort off-box sink; never blocks or raises into the UI
        from app.core.session import get_cached_session
        from app.core.sqlsafe import sql_literal

        session = get_cached_session()
        if session is not None:
            session.sql(
                f"INSERT INTO {core_object('APP_ERROR_LOG')} "
                "(PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME) VALUES ("
                f"{sql_literal(entry['page'])}, {sql_literal(entry['type'])}, "
                f"{sql_literal(entry['message'])}, {sql_literal(entry['context'])}, "
                "CURRENT_ROLE())"
            ).collect()
    except Exception:
        pass  # the ring buffer above still has it; Admin page shows it


def error_buffer() -> list[dict]:
    try:
        return list(st.session_state.get(_BUFFER_KEY, []))
    except Exception:
        return []


def safe_page(page_name: str):
    """Decorator: pages render inside this boundary.

    On failure: record (buffer + sink), then show a labeled, honest error —
    never a blank page, never a fake fallback.
    """

    def decorator(render_fn):
        @wraps(render_fn)
        def wrapper(*args, **kwargs):
            try:
                return render_fn(*args, **kwargs)
            except Exception as exc:
                record_error(page_name, exc, context="page render")
                st.error(f"{page_name} could not finish rendering.")
                st.caption(format_snowflake_error(exc))
                st.info(
                    "The failure was logged (Admin > Error log). Other pages are unaffected; "
                    "refresh after fixing the cause."
                )
                return None

        return wrapper

    return decorator
