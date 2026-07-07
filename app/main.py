"""App shell: sidebar navigation, global filters, page dispatch."""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="OVERWATCH — Snowflake Command Center",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from app.companies import COMPANIES, ENVIRONMENTS, database_options  # noqa: E402
from app.config import (  # noqa: E402
    APP_VERSION,
    DAY_WINDOW_OPTIONS,
    PAGES_BY_PROFILE,
    resolve_role_profile,
)
from app.core.query import bump_refresh_salt  # noqa: E402
from app.core.session import connection_available, current_role  # noqa: E402
from app.core.state import (  # noqa: E402
    consume_pending_navigation,
    init_filters,
    remember_page,
    requested_page,
)
from app.theme import inject_theme  # noqa: E402
from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security  # noqa: E402

_PAGE_ICONS = {
    "Overview": "📊",
    "Control Room": "🎛️",
    "Cost & Contract": "💰",
    "Operations": "🔧",
    "Alerts": "🚨",
    "Security": "🔐",
    "Admin": "⚙️",
}

_RENDERERS = {
    "Overview": overview.render,
    "Control Room": control_room.render,
    "Cost & Contract": cost.render,
    "Operations": operations.render,
    "Alerts": alerts.render,
    "Security": security.render,
    "Admin": admin.render,
}


def _sidebar(pages: tuple[str, ...], role: str, profile: str, connected: bool) -> str:
    """Navigation-only sidebar; scope filters live in the top bar (original-app layout)."""
    with st.sidebar:
        st.markdown(
            '<div class="ow-brand"><span class="ow-brand-dot"></span>'
            '<span class="ow-kicker">OVERWATCH</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f"**Snowflake Command Center** · v{APP_VERSION}")
        st.caption(
            (f"Connected · role {role or 'unknown'} · {profile} view")
            if connected else "Not connected to Snowflake"
        )
        st.divider()

        default_page = requested_page(pages) or st.session_state.get("_ow_page") or pages[0]
        if default_page not in pages:
            default_page = pages[0]
        st.caption("Navigate")
        page = st.radio("Navigate", pages, index=pages.index(default_page),
                        key="_ow_nav_radio", label_visibility="collapsed",
                        format_func=lambda p: f"{_PAGE_ICONS.get(p, '•')} {p}")
        st.session_state["_ow_page"] = page
        remember_page(page)

        st.divider()
        if st.button("Refresh data", use_container_width=True):
            bump_refresh_salt()
            st.rerun()
        st.caption("Account telemetry lags up to ~45 min; metering-daily up to 24h. Labels on every panel.")
    return page


def _topbar_scope() -> None:
    """Triage filter strip above every page, like the original OVERWATCH."""
    box = st.container(border=True)
    with box:
        st.markdown('<div class="ow-kicker">Triage filters</div>', unsafe_allow_html=True)
        _topbar_scope_controls()


def _topbar_scope_controls() -> None:
    c_company, c_env, c_days, c_db = st.columns([1.0, 1.0, 1.2, 1.4])
    with c_company:
        st.selectbox("Company", COMPANIES, key="flt_company")
    with c_env:
        st.selectbox("Environment", ENVIRONMENTS, key="flt_environment",
                     help="PROD = *_PRD and ALFA_EDW_PROD/MGM databases.")
    with c_days:
        st.select_slider("Window (days)", options=list(DAY_WINDOW_OPTIONS), key="flt_days")
    with c_db:
        db_options = ["", *database_options(st.session_state.get("flt_company", COMPANIES[0]))]
        if st.session_state.get("flt_database") not in db_options:
            st.session_state["flt_database"] = ""
        st.selectbox("Database", db_options, key="flt_database",
                     format_func=lambda v: v or "All databases",
                     help="Applies to query, task, DDL, attribution, and storage panels.")
    c_wh, c_user, c_schema = st.columns([1.2, 1.2, 1.2])
    with c_wh:
        st.text_input("Warehouse contains", key="flt_warehouse_contains")
    with c_user:
        st.text_input("User contains", key="flt_user_contains")
    with c_schema:
        st.text_input("Schema contains", key="flt_schema_contains",
                      help="Case-insensitive match where the source has schema grain.")


def main() -> None:
    consume_pending_navigation()
    inject_theme()
    init_filters()

    connected = connection_available()
    role = current_role() if connected else ""
    profile = resolve_role_profile(role)
    pages = PAGES_BY_PROFILE.get(profile, PAGES_BY_PROFILE["ANALYST"])

    page = _sidebar(pages, role, profile, connected)
    if connected:
        _topbar_scope()

    if not connected:
        st.title("OVERWATCH")
        st.error("No Snowflake connection.")
        st.markdown(
            "- **Streamlit-in-Snowflake:** the session is injected automatically — if you see this "
            "in SiS, the app's owner role lost access.\n"
            "- **Local dev:** add `[connections.snowflake]` to `.streamlit/secrets.toml` "
            "(see DEPLOYMENT.md).\n"
        )
        if st.button("Retry connection"):
            st.cache_resource.clear()
            st.session_state.pop("_ow_current_role", None)
            st.rerun()
        return

    _RENDERERS[page]()


if __name__ == "__main__":
    main()
