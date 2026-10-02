"""Every page renderer runs inside the safe_page error boundary (ARCHITECTURE.md, app/ui/pages/__init__.py).

The V028 split (6329789e: cost.py -> a dispatcher + cost_parts/) silently dropped Cost Intelligence's
@safe_page, so from then on a Python-side bug there (a KeyError on a missing column, say) escaped as
Streamlit's raw traceback: record_error never ran, nothing reached the session error ring or
APP_ERROR_LOG, and the copyable ref + Retry / Open-error-log controls were missing. main.py calls
_RENDERERS[page]() with no try of its own, so the decorator is the only boundary -- lock it for every page.
"""

from __future__ import annotations


def test_every_registered_renderer_carries_the_safe_page_boundary():
    from app import main

    missing = sorted(name for name, fn in main._RENDERERS.items() if not hasattr(fn, "__wrapped__"))
    assert missing == [], f"page renderer(s) without @safe_page: {missing}"


def test_cost_render_contains_a_python_side_failure(monkeypatch):
    """Behavioural: a raising Cost Intelligence body is recorded and labeled, never re-raised."""
    from app.core import errors
    from app.ui.pages import cost

    recorded: list[tuple[str, str]] = []
    shown: list[str] = []

    def _boom():
        raise KeyError("RULE_ID")

    def _record(page, exc, context=""):
        recorded.append((page, repr(exc)))
        return "OW-TEST-REF"

    monkeypatch.setattr(cost, "filters", _boom)
    monkeypatch.setattr(errors, "record_error", _record)
    monkeypatch.setattr(errors.st, "error", lambda msg, *a, **k: shown.append(str(msg)))
    monkeypatch.setattr(errors, "_recovery_controls", lambda ref, *, key: None)

    assert cost.render() is None                     # contained, not raised
    assert recorded == [("Cost Intelligence", "KeyError('RULE_ID')")]
    assert shown == ["Cost Intelligence could not finish rendering."]


def test_boundary_page_label_matches_the_renderer_key():
    """safe_page's label is the page's own _PAGE, the same string the nav and record_error use."""
    from app import main
    from app.ui.pages import cost

    assert main._RENDERERS[cost._PAGE] is cost.render
