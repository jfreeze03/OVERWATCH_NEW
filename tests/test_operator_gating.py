"""Operator-action gating: navigation by profile (AppTest) and the SQL-level
state gates that back the typed-confirmation UI."""

from __future__ import annotations

import pandas as pd
import pytest

from app.config import OPERATOR_PROFILES, PAGES_BY_PROFILE, resolve_role_profile

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402


def test_only_dba_profile_operates():
    assert OPERATOR_PROFILES == ("DBA",)
    assert "Admin" not in PAGES_BY_PROFILE["ANALYST"]
    assert "Admin" not in PAGES_BY_PROFILE["EXECUTIVE"]
    assert "Admin" not in PAGES_BY_PROFILE["MANAGER"]
    assert "Admin" in PAGES_BY_PROFILE["DBA"]
    assert resolve_role_profile("SNOW_PRI_GFR_PRD_ALFA_DTI") == "ANALYST"
    assert resolve_role_profile("OVERWATCH_MONITOR") == "ANALYST"


def _fake_run(*_args, **kwargs):
    return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))


@pytest.fixture
def _stub(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import components
    from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    for module in (overview, control_room, cost, operations, alerts, security, admin):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _fake_run)
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))

    def set_role(role):
        monkeypatch.setattr(main_mod, "current_role", lambda: role)
        for module in (cost, admin, alerts, operations, security):
            if hasattr(module, "current_role"):
                monkeypatch.setattr(module, "current_role", lambda: role)

    return set_role


def _entry():
    import app.main

    app.main.main()


def test_analyst_never_sees_admin_in_nav(_stub):
    _stub("SNOW_PRI_GFR_PRD_ALFA_DTI")  # -> ANALYST
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception
    options = list(at.radio(key="_ow_nav_radio").options)
    assert "Admin" not in options
    assert "Operations" in options


def test_executive_nav_is_read_only_surface(_stub):
    _stub("SNOW_PRI_GFR_PRD_ALFA_PDMWMGMT")  # -> EXECUTIVE
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception
    options = list(at.radio(key="_ow_nav_radio").options)
    assert "Admin" not in options and "Operations" not in options


def test_dba_gets_admin(_stub):
    _stub("SNOW_SYSADMINS")  # -> DBA
    at = AppTest.from_function(_entry, default_timeout=15)
    at.run()
    assert not at.exception
    assert "Admin" in list(at.radio(key="_ow_nav_radio").options)
