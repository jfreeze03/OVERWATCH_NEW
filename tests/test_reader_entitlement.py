"""Read-only tier (v4.374.0): non-admin viewers see the monitor, change nothing.

Page visibility on owner's-rights Streamlit-in-Snowflake keys on the VIEWER
(st.user), not CURRENT_ROLE() (which is the app owner's role for everyone). The
5 admins map to DBA, the 4 ETL users to the read-only READER profile (no
Admin/Alerts/Ask), and any identified-but-unmapped SiS viewer fails CLOSED to
READER — never the owner's DBA. Write entitlement (OPERATOR_USERS) stays a
separate axis: the ETL team is deliberately NOT operators.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.config import (
    PAGES_BY_PROFILE,
    VIEWER_PROFILES,
    VIEWER_UNKNOWN_PROFILE,
    is_operator_user,
    resolve_viewer_profile,
)

_ADMINS = ("H21427", "E22292", "KEBARR1", "CLROY", "N22514")
_ETL = ("GRTHOMP1", "SUDEVAX", "TV5073", "VS4229")


# ---------------------------------------------------------------------------
# The READER page set: everything EXCEPT Admin, Alerts, Ask (owner ask 2026-08-31)
# ---------------------------------------------------------------------------
def test_reader_profile_excludes_admin_alerts_ask():
    reader = PAGES_BY_PROFILE["READER"]
    for hidden in ("Admin", "Alerts", "Ask"):
        assert hidden not in reader, hidden
    for shown in ("Brief", "Overview", "Control Room", "Cost & Contract",
                  "Operations", "Decision Studio", "Security"):
        assert shown in reader, shown
    # ETL explicitly wanted Operations visible; it is (writes there are is_operator-gated)
    assert "Operations" in reader
    assert reader[0] == "Brief"                 # lands on Brief like every profile


# ---------------------------------------------------------------------------
# The pure viewer -> profile map (mirrors is_operator_user's case-insensitivity)
# ---------------------------------------------------------------------------
def test_resolve_viewer_profile_maps_admins_and_etl():
    for u in _ADMINS:
        assert resolve_viewer_profile(u) == "DBA", u
    for u in _ETL:
        assert resolve_viewer_profile(u) == "READER", u


def test_resolve_viewer_profile_is_case_insensitive():
    assert resolve_viewer_profile("h21427") == "DBA"
    assert resolve_viewer_profile("GrThOmP1") == "READER"


def test_resolve_viewer_profile_unmapped_and_blank_return_none():
    # None means "no explicit mapping" — the caller (active_profile) turns a
    # non-blank unmapped viewer into READER and a blank one into the role fallback.
    assert resolve_viewer_profile("SOMEONE_NEW") is None
    assert resolve_viewer_profile("") is None
    assert resolve_viewer_profile("   ") is None


def test_unknown_viewer_default_is_least_privilege_not_dba():
    assert VIEWER_UNKNOWN_PROFILE == "READER"
    assert "Admin" not in PAGES_BY_PROFILE[VIEWER_UNKNOWN_PROFILE]


# ---------------------------------------------------------------------------
# Write axis stays independent: the 5 admins operate, the ETL team never does
# ---------------------------------------------------------------------------
def test_operator_users_are_the_five_admins_only():
    for u in _ADMINS:
        assert is_operator_user(u), u
    for u in _ETL:
        assert not is_operator_user(u), u


def test_viewer_profiles_admins_match_operator_users():
    # every viewer mapped to DBA is also an operator, and vice-versa — the two
    # lists are separate but must not silently drift for the admin team
    dba_viewers = {k.upper() for k, v in VIEWER_PROFILES.items() if v == "DBA"}
    assert dba_viewers == {u.upper() for u in _ADMINS}


# ---------------------------------------------------------------------------
# session.active_profile(): viewer-first, fail-closed on SiS, role fallback off-SiS
# ---------------------------------------------------------------------------
def test_active_profile_admin_viewer_gets_dba(monkeypatch):
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "H21427")
    assert sess.active_profile("") == "DBA"


def test_active_profile_etl_viewer_gets_reader(monkeypatch):
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "grthomp1")   # case-insensitive
    assert sess.active_profile("") == "READER"


def test_active_profile_identified_unmapped_fails_closed_to_reader(monkeypatch):
    # a real person who opens the app but isn't listed must NOT inherit the
    # owner's DBA surface even when the owner-role arg is DBA
    import app.core.identity as ident
    import app.core.session as sess
    monkeypatch.setattr(ident, "viewer_name", lambda: "BRAND_NEW_USER")
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "READER"


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
    assert sess.active_profile("SNOW_ACCOUNTADMINS") == "READER"


# ---------------------------------------------------------------------------
# End-to-end: a READER viewer's nav hides Admin/Alerts/Ask and a forced
# _ow_page='Admin' still cannot render Admin (the dispatch hard-block)
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
    # the viewer is an ETL user -> active_profile resolves to READER
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


def test_reader_nav_hides_admin_alerts_ask_but_shows_operations(_reader_app):
    at = AppTest.from_function(_entry, default_timeout=20)
    at.run()
    assert not at.exception, at.exception
    options = _nav_options(at)
    for hidden in ("Admin", "Alerts", "Ask"):
        assert hidden not in options, hidden
    assert "Operations" in options


def test_reader_cannot_render_admin_via_stale_page(_reader_app):
    # simulate a stale/deep-link _ow_page='Admin' for a READER viewer: the
    # dispatch hard-block must force it back to an in-profile page
    at = AppTest.from_function(_entry, default_timeout=20)
    at.session_state["_ow_page"] = "Admin"
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["_ow_page"] != "Admin"
