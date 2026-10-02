"""Central error boundary and sink.

Contract (ARCHITECTURE.md): nothing is swallowed invisibly. Every caught
exception is recorded to the in-session ring buffer and best-effort written to
APP_ERROR_LOG; the Admin page displays both. This module and the other
core runtime modules are the only sanctioned broad-except sites (ruff BLE001
enforces that elsewhere).
"""

from __future__ import annotations

import hashlib
import os
import re
import traceback
from datetime import datetime
from functools import wraps

import streamlit as st

from app.config import APP_VERSION, core_object

_BUFFER_KEY = "_ow_error_buffer"
_BUFFER_MAX = 100
_CONTEXT_MAX = 2000          # APP_ERROR_LOG.CONTEXT VARCHAR(2000), V001__core.sql:137
_TB_MAX = 400
# c09 R1-006: set by record_error when a recorded failure says the Snowflake session expired;
# the sidebar's 'Refresh data' pops it and, off SiS only, drops the cached session to reconnect.
SESSION_EXPIRED_KEY = "_ow_session_expired"


def _session_expired_text(lower: str) -> bool:
    """The ONE session-expired test (lower-cased raw error text): format_snowflake_error's
    message and record_error's reconnect flag must never disagree on what 'expired' means."""
    return "session no longer exists" in lower or ("token" in lower and "expired" in lower)


def _viewer_for_log() -> str:
    """#50: the viewing user for APP_ERROR_LOG.CONTEXT; never raises, '—' when unknown.

    ROLE_NAME stores CURRENT_ROLE() (the owner role under owner's-rights SiS), so the
    viewer travels in CONTEXT. The import stays lazy: query.py imports this module."""
    try:
        from app.core.identity import viewer_name
        who = re.sub(r"[\s·]+", "_", str(viewer_name() or "").strip())[:64]
        return who or "—"
    except Exception:
        return "—"


def _tb_tail(error: BaseException, frames: int = 3, max_len: int = _TB_MAX) -> str:
    """'admin.py:1276 _observability_tab < ...' innermost first; app/ frames preferred; '' if never raised.

    Reads error.__traceback__ (not format_exc): record_error is also called outside an
    except block (query._entitlement_refusal), where format_exc says 'NoneType: None'."""
    try:
        tb = traceback.extract_tb(getattr(error, "__traceback__", None))
        app_f = [f for f in tb if re.search(r"[\\/]app[\\/]", f.filename or "")]
        pick = (app_f or list(tb))[-frames:]
        return " < ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(pick))[:max_len]
    except Exception:
        return ""


def _context_line(ref: str, context: str, viewer: str, tb: str) -> str:
    """ref=… · viewer=… · v<build> · <context> · tb=… within the 2000-char column."""
    head = f"ref={ref} · viewer={viewer} · v{APP_VERSION}"
    tail = f" · tb={tb}" if tb else ""
    room = _CONTEXT_MAX - len(head) - len(tail)
    mid = f" · {context}"[:room] if context and room > 3 else ""
    return (head + mid + tail)[:_CONTEXT_MAX]     # the caller context is cut first; ref and tb always survive


def format_snowflake_error(error: object, max_len: int = 300) -> str:
    """Short, user-safe rendering of a Snowflake/driver error.

    run() invokes this in its except path (to fill QueryResult.error), so like
    _classify_error and record_error it must survive an exception whose __str__ itself
    raises — otherwise run() re-raises and breaks its 'never raises' contract."""
    try:
        text = str(error or "").strip()
    except Exception:
        return f"<{type(error).__name__}: message unavailable>"
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
    if _session_expired_text(lower):
        # c09 R1-006: name a remedy that works where the user is. 'Refresh data' used to be the only
        # advice, but it only bumped the read salt -- the expired session stayed cached. In SiS a browser
        # reload starts a fresh app instance; off SiS (local dev) Refresh now drops the cached session
        # (only after record_error saw this expiry -- SESSION_EXPIRED_KEY).
        return ("The Snowflake session expired. Reload the app in your browser to reconnect "
                "(local dev: press 'Refresh data' in the sidebar).")
    text = re.sub(r"^\(\d+\):?\s*[0-9a-f-]*:?\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def record_error(page: str, error: BaseException, context: str = "") -> str:
    """Ring-buffer the error and best-effort persist it to the Snowflake sink.

    Returns a short error REFERENCE (also stored on the buffer entry and prepended
    into the persisted CONTEXT) so the UI can show a copyable id the operator can
    quote when reporting the failure (Wave 2 #46). The ref is a timestamp + a short
    hash of the error, so the same operator-visible ref matches the APP_ERROR_LOG row.

    #50: the CONTEXT shape is `ref=… · viewer=… · v<build> · <context> · tb=…` —
    the viewing user (ROLE_NAME stays CURRENT_ROLE(), the owner), the app build, the
    caller's context, and a short innermost-first app-frame traceback. When it must
    be cut to the column, the caller context goes first; ref and tb always survive.
    app.logic.app_telemetry.parse_error_context reads it back for Admin.
    """
    # R2-051: account time, so the ref's date/time and Admin's 'at' column agree with APP_ERROR_LOG.LOGGED_AT
    # (a Central CURRENT_TIMESTAMP() default) instead of running 5-6 hours (and every Central evening, a
    # calendar day) ahead on the UTC process clock. Guarded: the boundary must never raise.
    try:
        from app.logic.formulas import account_now

        at = account_now()
    except Exception:
        at = datetime.now()
    # The error boundary must be bulletproof: a hostile/buggy __str__ (e.g. a driver
    # exception that lazily formats a None attribute) must NOT raise out of record_error
    # and defeat safe_page. Render the message once, guarded.
    try:
        _msg = str(error)
    except Exception:  # a raising __str__ must not be allowed to crash the boundary
        _msg = f"<{type(error).__name__}: message unavailable>"
    _digest = hashlib.sha1(
        f"{type(error).__name__}|{_msg}|{traceback.format_exc(limit=3)}".encode("utf-8", "replace")
    ).hexdigest()[:6].upper()
    ref = f"OW-{at.strftime('%Y%m%d-%H%M%S')}-{_digest}"
    try:
        _ctx = _context_line(ref, context, _viewer_for_log(), _tb_tail(error))
    except Exception:
        _ctx = (f"ref={ref} · {context}" if context else f"ref={ref}")[:2000]
    entry = {
        "at": at.isoformat(timespec="seconds"),
        "ref": ref,
        "page": str(page)[:80],
        "type": type(error).__name__[:200],
        "message": _msg[:2000],
        "context": _ctx,
        "trace": traceback.format_exc(limit=6)[:4000],
    }
    try:
        buffer = st.session_state.setdefault(_BUFFER_KEY, [])
        buffer.append(entry)
        del buffer[:-_BUFFER_MAX]
        if _session_expired_text(_msg.lower()):
            st.session_state[SESSION_EXPIRED_KEY] = True   # c09 R1-006: Refresh may now reconnect
    except Exception:
        pass  # session not available (import-time failure); nothing else to do

    try:  # best-effort off-box sink; never blocks or raises into the UI
        from app.core.session import get_cached_session, statement_params, submit_collect
        from app.core.sqlsafe import sql_literal

        session = get_cached_session()
        if session is not None:
            statement = session.sql(
                f"INSERT INTO {core_object('APP_ERROR_LOG')} "
                "(PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME) VALUES ("
                f"{sql_literal(entry['page'])}, {sql_literal(entry['type'])}, "
                f"{sql_literal(entry['message'])}, {sql_literal(entry['context'])}, "
                "CURRENT_ROLE())"
            )
            # Next-Fifty #7 Slice B: the sink's INSERT is the app's own statement too — tag it.
            params = statement_params(session, page=entry["page"], tier="write")
            try:
                # Async: an error path should not pay a SECOND blocking round
                # trip just to log the first failure.
                submit_collect(session, statement, params, nowait=True)
            except AttributeError:  # older Snowpark: no async API
                submit_collect(session, statement, params)
    except Exception:
        pass  # the ring buffer above still has it; Admin page shows it
    return ref


def error_buffer() -> list[dict]:
    try:
        return list(st.session_state.get(_BUFFER_KEY, []))
    except Exception:
        return []


def _recovery_controls(ref: str, *, key: str) -> None:
    """Wave 2 #46: one consistent recovery affordance under a page-render failure —
    a copyable error reference (also written to APP_ERROR_LOG.CONTEXT), a Retry that
    re-runs the page after bumping the read salt, and an Admin-gated jump to the error
    log. Every import here is LAZY: query.py imports this module at load, so a
    module-level import of state/session/config would be an import cycle.
    """
    st.caption("Quote this reference if you report it — it's in this session's error "
               "list (Admin ▸ Errors & telemetry), and in the persisted log when the "
               "connection was live. Other pages are unaffected.")
    if ref:
        st.code(ref, language=None)   # st.code carries a built-in one-click copy button
    cols = st.columns(2)
    with cols[0]:
        if st.button("↻ Retry", key=f"_ow_retry_{key}", width="stretch"):
            from app.core.query import bump_refresh_salt  # lazy: query imports this module
            bump_refresh_salt()
            st.rerun()
    with cols[1]:
        from app.config import PAGES_BY_PROFILE
        from app.core.session import active_profile
        # Only offer the jump to a viewer who can actually open Admin —
        # request_navigation clamps Admin -> Overview for everyone else, which
        # would silently strand them somewhere they didn't ask to go.
        if "Admin" in PAGES_BY_PROFILE.get(active_profile(), ()) and st.button(
                "Open error log →", key=f"_ow_errnav_{key}", width="stretch"):
            from app.core.state import request_navigation
            request_navigation("Admin", "Errors & telemetry")   # self-reruns


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
                ref = record_error(page_name, exc, context="page render")
                st.error(f"{page_name} could not finish rendering.")
                friendly = format_snowflake_error(exc)
                if isinstance(exc, (KeyError, ValueError, TypeError, AttributeError, IndexError)):
                    # Python-side bug, not a Snowflake failure: say so, with the
                    # type, so triage starts in the right place (e.g. the
                    # KeyError('RULE_ID') class of crash).
                    friendly = f"{type(exc).__name__}: {friendly} — app bug, not a Snowflake failure."
                st.caption(friendly)
                # #46: one standardized recovery block (copyable ref + Retry +
                # Admin-gated Open-Errors) instead of the old prose st.info.
                _recovery_controls(ref, key=page_name)
                return None

        return wrapper

    return decorator
