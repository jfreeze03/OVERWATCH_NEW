"""Bug-hunt c09 R1-194: Brief's Fires and Asks render a FAILED read by its kind.

A timeout of the open-alerts feed or the action-queue read used to say "Alerting not installed yet." /
"Action queue not installed yet." on the phone-first morning page. Now a non-absence failure renders
empty_state('unavailable', ...) with the error, and only a true absence (app.core.result.is_setup_absence) keeps
the setup wording. Rendered through the shaped AppTest harness (its autouse stub fixture is imported below).
"""

from __future__ import annotations

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402
from tests.test_pages_shaped import (  # noqa: E402, F401  (_stub_shaped: the harness's autouse stub fixture)
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
_FEEDS = ("events", "acts_", "brief_events_", "brief_actions_")


def _render_brief(monkeypatch, kind: str):
    from app.ui.pages import brief

    failed = QueryResult(ok=False, error=f"stub {kind}", error_kind=kind)

    def _batch(specs, **k):
        out = _shaped_batch(specs, **k)
        for s in specs:
            if str(s.get("key", "")).startswith(_FEEDS):
                out[s["key"]] = failed
        return out

    def _run(*a, **k):
        return failed if str(k.get("key", "")).startswith(_FEEDS) else _shaped_run(*a, **k)

    monkeypatch.setattr(brief, "run_batch", _batch)
    monkeypatch.setattr(brief, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Brief")
    at.run()
    assert not at.exception, at.exception
    return [str(i.value) for i in at.info], [str(e.value) for e in at.error]


@_SKIP
@pytest.mark.parametrize("kind", ["timeout", "other", "missing_column"])
def test_failed_fires_and_asks_are_unavailable_not_not_installed(monkeypatch, kind):
    infos, errors = _render_brief(monkeypatch, kind)
    assert "Alerting not installed yet." not in infos and "Action queue not installed yet." not in infos
    assert any("Couldn't read open alerts right now." in e for e in errors)
    assert any("Couldn't read the action queue right now." in e for e in errors)


@_SKIP
@pytest.mark.parametrize("kind", ["absent", "privilege", "unknown_function"])
def test_absent_alerting_and_queue_keep_the_setup_wording(monkeypatch, kind):
    infos, errors = _render_brief(monkeypatch, kind)
    assert "Alerting not installed yet." in infos and "Action queue not installed yet." in infos
    assert not any("Couldn't read" in e for e in errors)
