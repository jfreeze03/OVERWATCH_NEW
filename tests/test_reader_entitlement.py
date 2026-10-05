"""Read-only tiers: non-admin viewers see the monitor, change nothing.

Page visibility on owner's-rights Streamlit-in-Snowflake keys on the VIEWER
(st.user), not CURRENT_ROLE() (which is the app owner's role for everyone).
v4.374.0 mapped the 5 admins to DBA and the 4 ETL users to the read-only READER
profile. v4.610.0 (owner decision 2026-10-05) replaced the per-user pins: the 5
admins are DBA through OPERATOR_USERS alone (a DBA pin is forbidden), a direct
SNOW_PRI_GFR_PRD_ALFA_DSA grantee is DBA too, and every other identified viewer --
the ETL team (SNOW_PRI_GFR_PRD_ALFA_DTI) included -- gets the view-only MONITOR
profile (Cost Intelligence + Operations). An unidentified SiS viewer fails CLOSED
to MONITOR, never the owner's DBA. Write entitlement stays a separate axis: the
ETL team is deliberately NOT operators. tests/test_role_access.py covers the role
lookup itself.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.config import (
    NO_IDENTITY_PROFILE,
    OPERATOR_PROFILES,
    PAGES_BY_PROFILE,
    VIEWER_PROFILES,
    VIEWER_UNKNOWN_PROFILE,
    is_operator_user,
    resolve_viewer_profile,
)

_ADMINS = ("H21427", "E22292", "KEBARR1", "CLROY", "N22514")
_ETL = ("GRTHOMP1", "SUDEVAX", "TV5073", "VS4229")


# ---------------------------------------------------------------------------
# The READER page set: everything EXCEPT Admin, Alerts, Ask (owner ask 2026-08-31).
# Kept for an explicit pin; no viewer lands on it by default since v4.610.0.
# ---------------------------------------------------------------------------
def test_reader_profile_excludes_admin_alerts_ask():
    reader = PAGES_BY_PROFILE["READER"]
    for hidden in ("Admin", "Alerts", "Ask"):
        assert hidden not in reader, hidden
    for shown in ("Brief", "Overview", "Control Room", "Cost Intelligence",
                  "Operations", "Proof", "Security"):
        assert shown in reader, shown
    assert reader[0] == "Brief"


# ---------------------------------------------------------------------------
# The MONITOR page set: exactly Cost Intelligence + Operations (owner 2026-10-05)
# ---------------------------------------------------------------------------
def test_monitor_profile_is_the_two_page_view_surface():
    monitor = PAGES_BY_PROFILE["MONITOR"]
    assert monitor == ("Cost Intelligence", "Operations")
    for hidden in ("Brief", "Overview", "Control Room", "Proof", "Alerts", "Security", "Admin", "Ask"):
        assert hidden not in monitor, hidden
    assert monitor[0] == "Cost Intelligence"     # its landing page


# ---------------------------------------------------------------------------
# The pure viewer -> pin map: no admin pins, no ETL pins (v4.610.0)
# ---------------------------------------------------------------------------
def test_no_viewer_is_pinned_any_more():
    for u in (*_ADMINS, *_ETL):
        assert resolve_viewer_profile(u) is None, u


def test_resolve_viewer_profile_is_case_insensitive(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "VIEWER_PROFILES", {"GRTHOMP1": "READER"})
    assert resolve_viewer_profile("GrThOmP1") == "READER"


def test_resolve_viewer_profile_unmapped_and_blank_return_none():
    # None means "no explicit pin" — the caller (viewer_access) turns a non-blank
    # unpinned viewer into MONITOR and a blank one into the role fallback.
    assert resolve_viewer_profile("SOMEONE_NEW") is None
    assert resolve_viewer_profile("") is None
    assert resolve_viewer_profile("   ") is None


def test_unknown_and_unidentified_viewers_get_least_privilege_not_dba():
    assert VIEWER_UNKNOWN_PROFILE == NO_IDENTITY_PROFILE == "MONITOR"
    for profile in (VIEWER_UNKNOWN_PROFILE, NO_IDENTITY_PROFILE):
        assert "Admin" not in PAGES_BY_PROFILE[profile]
        assert profile not in OPERATOR_PROFILES


# ---------------------------------------------------------------------------
# Write axis stays independent: the 5 admins operate, the ETL team never does
# ---------------------------------------------------------------------------
def test_operator_users_are_the_five_admins_only():
    for u in _ADMINS:
        assert is_operator_user(u), u
    for u in _ETL:
        assert not is_operator_user(u), u


def test_viewer_profiles_never_pin_an_admin_profile():
    # OPERATOR_USERS alone means admin (v4.610.0): a DBA pin would be a second, drifting admin list
    assert not [k for k, v in VIEWER_PROFILES.items() if v in OPERATOR_PROFILES]


# ---------------------------------------------------------------------------
# session.active_profile(): viewer-first, fail-closed on SiS, role fallback off-SiS
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_session_state():
    import streamlit as _st
    _st.session_state.clear()
    yield
    _st.session_state.clear()


def test_active_profile_admin_viewer_gets_dba(monkeypatch):
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "H21427")
    assert sess.active_profile("") == "DBA"


def test_active_profile_etl_viewer_gets_monitor(monkeypatch):
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "grthomp1")   # case-insensitive
    assert sess.active_profile("") == "MONITOR"


def test_active_profile_identified_unmapped_fails_closed_to_monitor(monkeypatch):
    # a real person who opens the app but is not an admin must NOT inherit the
    # owner's DBA surface even when the owner-role arg is DBA
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "BRAND_NEW_USER")
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "MONITOR"


def test_active_profile_off_sis_falls_back_to_role(monkeypatch):
    # no viewer identity + not SiS (local dev / tests): preserve today's behavior
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "")
    monkeypatch.setattr(sess, "is_sis", lambda: False)
    assert sess.active_profile("SNOW_SYSADMINS") == "DBA"


def test_active_profile_sis_without_identity_fails_closed(monkeypatch):
    # unresolved identity WHILE on SiS must not grant the owner's DBA surface
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "")
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "MONITOR"


def test_is_operator_sis_without_identity_fails_closed(monkeypatch):
    # The WRITE axis must fail closed too: an unidentified SiS viewer executes with
    # the app owner's role, so the role->profile fallback would treat them as the
    # owner-operator and expose owner-privileged writes. Mirrors active_profile().
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "")
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    assert sess.is_operator() is False


def test_is_operator_identified_admin_holds_on_sis(monkeypatch):
    # An identified allowlisted operator is entitled by st.user regardless of runtime,
    # and never waits on (or pays for) the admin-role lookup.
    import app.core.identity as ident
    import app.core.session as sess

    def _no_lookup():
        raise AssertionError("an allowlisted admin must not run the role lookup")

    monkeypatch.setattr(ident, "viewer_name", lambda: _ADMINS[0])
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    monkeypatch.setattr(sess, "_admin_role_rows", _no_lookup)
    assert sess.is_operator() is True


# ---------------------------------------------------------------------------
# End-to-end: a MONITOR viewer's nav shows only Cost Intelligence + Operations and a
# forced _ow_page='Admin' still cannot render Admin (the dispatch hard-block)
# ---------------------------------------------------------------------------
st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402


def _fake_run(*_args, **kwargs):
    return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))


@pytest.fixture
def _reader_app(monkeypatch):
    import app.core.identity as ident
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import (
        admin,
        alerts,
        control_room,
        cost,
        operations,
        overview,
        security,
    )
    from app.ui.pages.cost_parts import ai_chargeback, contract, optimize, spend

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_ACCOUNTADMINS")
    # the viewer is an ETL user -> active_profile resolves to MONITOR (off-SiS: no role lookup)
    monkeypatch.setattr(ident, "viewer_name", lambda: "GRTHOMP1")

    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    for module in (overview, control_room, cost, operations, alerts, security, admin,
                   spend, contract, ai_chargeback, optimize):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _fake_run)
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))


def _entry():
    import app.main

    app.main.main()


def _nav_options(at) -> list[str]:
    opts: list[str] = []
    for r in at.radio:
        if str(getattr(r, "key", "") or "").startswith("_ow_nav_"):
            opts.extend(list(r.options))
    return opts


def test_monitor_nav_shows_only_cost_intelligence_and_operations(_reader_app):
    at = AppTest.from_function(_entry, default_timeout=20)
    at.run()
    assert not at.exception, at.exception
    options = _nav_options(at)
    assert sorted(options) == ["Cost Intelligence", "Operations"]
    assert at.session_state["_ow_page"] == "Cost Intelligence"     # MONITOR's landing page


def test_monitor_cannot_render_admin_via_stale_page(_reader_app):
    # simulate a stale/deep-link _ow_page='Admin' for a MONITOR viewer: the
    # dispatch hard-block must force it back to an in-profile page
    at = AppTest.from_function(_entry, default_timeout=20)
    at.session_state["_ow_page"] = "Admin"
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["_ow_page"] != "Admin"
