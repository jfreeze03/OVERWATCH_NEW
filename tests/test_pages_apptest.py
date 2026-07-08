"""Streamlit AppTest smoke: every page must render without exceptions.

The query layer is stubbed (empty-but-ok results), so this exercises layout,
state wiring, tabs, and honest empty states — the UI regressions plain unit
tests cannot see. Skipped automatically when streamlit isn't installed.
"""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.config import PAGES_BY_PROFILE  # noqa: E402
from app.core.result import QueryResult  # noqa: E402

_PAGES = PAGES_BY_PROFILE["DBA"]


def _fake_run(*_args, **kwargs):
    return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))


def _fake_execute(*_args, **_kwargs):
    return True, "stubbed"


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security
    from app.ui.pages.cost_parts import ai_chargeback, contract, optimize, spend

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_SYSADMINS")

    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))

    for module in (overview, control_room, cost, operations, alerts, security, admin,
                   spend, contract, ai_chargeback, optimize):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _fake_run)
        if hasattr(module, "execute_statement"):
            monkeypatch.setattr(module, "execute_statement", _fake_execute)
        if hasattr(module, "current_role"):
            monkeypatch.setattr(module, "current_role", lambda: "SNOW_SYSADMINS")
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))


def _entry():
    import app.main

    app.main.main()


def test_app_boots_without_exceptions():
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("page", _PAGES)
def test_each_page_renders(page):
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception
    nav = at.radio(key="_ow_nav_radio")
    nav.set_value(page)
    at.run()
    assert not at.exception, f"{page}: {at.exception}"
    # honest-empty pattern: the page produced *some* content, not a blank body
    assert at.title or at.markdown, page
