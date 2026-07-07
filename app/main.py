"""App shell: sidebar navigation, global filters, page dispatch."""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="OVERWATCH — Snowflake Command Center",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from app.companies import COMPANIES, ENVIRONMENTS  # noqa: E402
from app.config import (  # noqa: E402
    APP_VERSION,
    DAY_WINDOW_OPTIONS,
    PAGES_BY_PROFILE,
    resolve_role_profile,
)
from app.core.query import bump_refresh_salt  # noqa: E402
from app.core.session import connection_available, current_role  # noqa: E402
from app.core.state import init_filters, remember_page, requested_page  # noqa: E402
from app.theme import inject_theme  # noqa: E402
from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security  # noqa: E402

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
    with st.sidebar:
        st.markdown('<div class="ow-kicker">OVERWATCH</div>', unsafe_allow_html=True)
        st.markdown(f"**Snowflake Command Center** · v{APP_VERSION}")
        st.caption(
            (f"Connected · role {role or 'unknown'} · {profile} view")
            if connected else "Not connected to Snowflake"
        )
        st.divider()

        default_page = requested_page(pages) or st.session_state.get("_ow_page") or pages[0]
        if default_page not in pages:
            default_page = pages[0]
        page = st.radio("Navigate", pages, index=pages.index(default_page), key="_ow_nav_radio")
        st.session_state["_ow_page"] = page
        remember_page(page)

        st.divider()
        st.markdown("**Scope**")
        st.selectbox("Company", COMPANIES, key="flt_company")
        st.selectbox("Environment", ENVIRONMENTS, key="flt_environment",
                     help="PROD = *_PRD and ALFA_EDW_PROD/MGM databases.")
        st.select_slider("Window (days)", options=list(DAY_WINDOW_OPTIONS), key="flt_days")
        st.text_input("Warehouse contains", key="flt_warehouse_contains")
        st.text_input("User contains", key="flt_user_contains")
        if st.button("Refresh data", use_container_width=True):
            bump_refresh_salt()
            st.rerun()
        st.caption("Account telemetry lags up to ~45 min; metering-daily up to 24h. Labels on every panel.")
    return page


def main() -> None:
    inject_theme()
    init_filters()

    connected = connection_available()
    role = current_role() if connected else ""
    profile = resolve_role_profile(role)
    pages = PAGES_BY_PROFILE.get(profile, PAGES_BY_PROFILE["ANALYST"])

    page = _sidebar(pages, role, profile, connected)

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
