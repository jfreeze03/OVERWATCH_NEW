"""Streamlit AppTest smoke: every page must render without exceptions.

The query layer is stubbed (empty-but-ok results), so this exercises layout,
state wiring, tabs, and honest empty states — the UI regressions plain unit
tests cannot see. Skipped automatically when streamlit isn't installed.
"""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from packaging.version import parse as _parse_version  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.config import PAGES_BY_PROFILE  # noqa: E402
from app.core.result import QueryResult  # noqa: E402

_PAGES = PAGES_BY_PROFILE["DBA"]

# streamlit < 1.55.0 ships an AppTest-harness bug (NOT an app bug): the internal
# ButtonGroup class behind st.segmented_control/st.pills does not wrap a *single*-select
# widget's scalar value in a list, so ButtonGroup.indices char-iterates the section
# switcher's stored string ("SLO scorecard" -> 'S','L','O', ...) and AppTest.run() raises
# `ValueError: content: "S" is not in list` before the app's own nav logic ever runs. The
# app renders correctly at runtime on those versions; only the test harness mis-introspects.
# Fixed in streamlit 1.55.0 (verified by bisect: 1.54.0 fails, 1.55.0 passes). The
# floor-compat CI job pins streamlit==1.52.2 (still < 1.55.0), and only the multi-run
# nav test below trips it (the 2-run test_each_page_renders survives). Guarding just
# that test keeps the floor gate green while the test still runs on the modern
# lint-and-test job and locally.
_APPTEST_BUTTONGROUP_OK = _parse_version(st.__version__) >= _parse_version("1.55.0")


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


def _nav_to(at, page: str) -> None:
    """Select a page through the rec14 workflow-grouped nav (the target sits in one
    of the _ow_nav_Watch/Analyze/Govern/More radios); setting it fires the
    _nav_pick callback that updates _ow_page and clears the sibling groups."""
    for r in at.radio:
        if str(getattr(r, "key", "") or "").startswith("_ow_nav_") and page in list(r.options):
            r.set_value(page)
            return
    raise AssertionError(f"page {page!r} not offered in any nav group")


def _ss(at, key):
    # AppTest's session_state proxy has no .get (attribute access maps to key access),
    # so item-access + `in` is required here — not the SIM401-suggested .get().
    return at.session_state[key] if key in at.session_state else None  # noqa: SIM401


def _selected_pages(at, current: str) -> set:
    """The live per-group radios are keyed `_ow_nav_{group}_{current}`; exactly one
    should hold a page. (Reading session_state sidesteps AppTest's KeyError on
    `.value` for dynamically-keyed widgets; AppTest's session_state has no `.get`.)"""
    from app.config import PAGES_BY_PROFILE, nav_groups_for
    sel = set()
    for group, _ in nav_groups_for(PAGES_BY_PROFILE["DBA"]):
        v = _ss(at, f"_ow_nav_{group}_{current}")
        if v:
            sel.add(v)
    return sel


@pytest.mark.skipif(
    not _APPTEST_BUTTONGROUP_OK,
    reason="streamlit<1.55 AppTest ButtonGroup single-select bug char-iterates the section "
    "switcher's scalar value; app is correct at runtime, harness fixed in streamlit 1.55.0",
)
def test_nav_single_select_across_groups():
    # multi-select bug: clicking a page in a DIFFERENT group (Analyze) than the current
    # one (Watch) must navigate AND leave exactly ONE group highlighted — the old
    # stale-sibling-key path left two selected and could get stuck.
    at = AppTest.from_function(_entry, default_timeout=20)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost & Contract")          # Watch -> Analyze
    at.run()
    assert not at.exception
    assert _ss(at, "_ow_page") == "Cost & Contract"
    assert _selected_pages(at, "Cost & Contract") == {"Cost & Contract"}
    _nav_to(at, "Admin")                    # Analyze -> Govern
    at.run()
    assert not at.exception
    assert _ss(at, "_ow_page") == "Admin"
    assert _selected_pages(at, "Admin") == {"Admin"}


@pytest.mark.parametrize("page", _PAGES)
def test_each_page_renders(page):
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception
    _nav_to(at, page)
    at.run()
    assert not at.exception, f"{page}: {at.exception}"
    # honest-empty pattern: the page produced *some* content, not a blank body
    assert at.title or at.markdown, page
