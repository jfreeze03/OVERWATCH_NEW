"""Wave 2 #46 locks: standardized page-failure recovery (ref + Retry + Open-Errors)."""
import re
from pathlib import Path
from types import SimpleNamespace

from app.core import errors

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_record_error_returns_a_stamped_reference(monkeypatch):
    # #46: record_error mints a short, copyable reference, stamps it on the buffer
    # entry, and prepends it into the persisted CONTEXT so the operator-visible ref
    # matches its APP_ERROR_LOG row.
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))
    ref = errors.record_error("Cost & Contract", ValueError("boom"), context="page render")
    assert re.fullmatch(r"OW-\d{8}-\d{6}-[0-9A-F]{6}", ref)
    entry = errors.st.session_state[errors._BUFFER_KEY][-1]
    assert entry["ref"] == ref
    assert ref in entry["context"] and "page render" in entry["context"]


def test_same_error_is_stable_within_a_second(monkeypatch):
    # the hash half is deterministic for the same error, so a re-raise reads as the
    # same incident (only the timestamp half moves).
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))
    a = errors.record_error("P", ValueError("x"))
    b = errors.record_error("P", ValueError("x"))
    assert a.split("-")[-1] == b.split("-")[-1]   # identical 6-char digest


def test_record_error_survives_a_hostile_str(monkeypatch):
    # The error boundary must be bulletproof: an exception whose __str__ itself raises
    # (e.g. a driver error that lazily formats a missing attribute) must NOT abort
    # ref-minting/buffering and defeat safe_page. record_error renders the message once,
    # guarded, and still returns a valid stamped ref.
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))

    class _Hostile(RuntimeError):
        def __str__(self):  # deliberately raising
            raise RuntimeError("cannot render me")

    ref = errors.record_error("Operations", _Hostile(), context="render")
    assert re.fullmatch(r"OW-\d{8}-\d{6}-[0-9A-F]{6}", ref)
    entry = errors.st.session_state[errors._BUFFER_KEY][-1]
    assert entry["type"] == "_Hostile"
    assert "unavailable" in entry["message"]      # the guarded fallback text
    assert ref in entry["context"]


def test_record_error_ref_survives_session_unavailable(monkeypatch):
    # The Snowflake sink is best-effort: get_cached_session() may return None (local dev,
    # a dropped connection). The ring buffer + ref must still be produced.
    import app.core.session as sess
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(sess, "get_cached_session", lambda: None)
    ref = errors.record_error("Brief", ValueError("no conn"))
    assert re.fullmatch(r"OW-\d{8}-\d{6}-[0-9A-F]{6}", ref)
    assert errors.st.session_state[errors._BUFFER_KEY][-1]["ref"] == ref


def test_safe_page_offers_a_standard_recovery_block():
    src = _src("app/core/errors.py")
    assert "def _recovery_controls(" in src
    assert "ref = record_error(page_name" in src
    assert "_recovery_controls(ref, key=page_name)" in src
    assert "st.info(" not in src.split("def safe_page", 1)[1]   # prose info replaced
    # the block carries a copyable ref, a Retry, and an Admin-gated nav
    block = src.split("def _recovery_controls(", 1)[1].split("\ndef ", 1)[0]
    assert "st.code(ref" in block
    assert "bump_refresh_salt()" in block and "st.rerun()" in block
    assert '"Admin" in PAGES_BY_PROFILE.get(active_profile()' in block
    assert 'request_navigation("Admin", "Errors & telemetry")' in block


def test_recovery_imports_stay_lazy_to_avoid_the_query_import_cycle():
    # query.py imports errors.py at module load, so errors.py must NOT import
    # state/session/query at module level — every recovery import is inside a function.
    header = _src("app/core/errors.py").split("def format_snowflake_error", 1)[0]
    for banned in ("from app.core.state", "from app.core.query", "from app.core.session",
                   "from app.ui"):
        assert banned not in header, banned


def test_admin_error_table_shows_the_reference():
    src = _src("app/ui/pages/admin.py")
    assert 'reindex(columns=["at", "ref", "page", "type", "message"])' in src
