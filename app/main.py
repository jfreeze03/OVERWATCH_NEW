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
from app.core.query import bump_refresh_salt, execute_statement, run  # noqa: E402
from app.core.session import connection_available, current_role  # noqa: E402
from app.core.sqlsafe import sql_literal  # noqa: E402
from app.core.state import (  # noqa: E402
    consume_pending_navigation,
    init_filters,
    remember_page,
    request_navigation,
    requested_page,
)
from app.data import mart_sql, security_sql  # noqa: E402
from app.theme import inject_theme  # noqa: E402
from app.ui.components import notify  # noqa: E402
from app.ui.pages import (  # noqa: E402
    admin,
    alerts,
    brief,
    control_room,
    cost,
    operations,
    overview,
    security,
)

_PAGE_ICONS = {
    "Brief": "☀️",
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
    "Brief": brief.render,
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
        _log_usage(page)

        st.divider()
        _global_jump(pages)
        _health_strip()
        if st.button("Refresh data", use_container_width=True):
            bump_refresh_salt()
            st.rerun()
        st.caption("Account telemetry lags up to ~45 min; metering-daily up to 24h. Labels on every panel.")
    return page


def _current_view_payload() -> str:
    import json

    from app.core.state import filters
    from app.logic.navigate import PAGE_SECTION_KEYS

    page = str(st.session_state.get("_ow_page") or "")
    section_key = PAGE_SECTION_KEYS.get(page, "")
    return json.dumps({
        "page": page,
        "section": str(st.session_state.get(section_key) or "") if section_key else "",
        "filters": filters(),
    })


def _parse_view(raw: str) -> dict | None:
    import json

    try:
        data = json.loads(raw or "")
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _views_popover() -> None:
    """Saved filter views + default landing (USER_PREFS, V013)."""
    from app.core.state import request_navigation
    from app.data import prefs_sql

    with st.popover("💾 Views"):
        prefs = run(prefs_sql.user_prefs(), page="Views", key="user_prefs", tier="live",
                    source="USER_PREFS")
        views: dict[str, str] = {}
        has_default = False
        if prefs.ok and not prefs.empty:
            for _, row in prefs.df.iterrows():
                key = str(row["PREF_KEY"])
                if key.startswith("VIEW:"):
                    views[key[5:]] = str(row["PREF_VALUE"] or "")
                elif key == "DEFAULT_VIEW":
                    has_default = True
        elif not prefs.ok:
            st.caption("Saved views need migration V013 (and a roles.sql re-run).")

        if views:
            pick = st.selectbox("Saved views", sorted(views), key="views_pick")
            c1, c2, c3 = st.columns(3)
            if c1.button("Apply", key="views_apply", use_container_width=True):
                data = _parse_view(views.get(pick, ""))
                if data:
                    request_navigation(str(data.get("page") or st.session_state.get("_ow_page") or ""),
                                       str(data.get("section") or ""),
                                       dict(data.get("filters") or {}))
            if c2.button("Set default", key="views_default", use_container_width=True,
                         help="This view loads automatically when you open the app."):
                ok, msg = execute_statement(
                    prefs_sql.upsert_pref_sql("DEFAULT_VIEW", views.get(pick, "")), page="Views")
                notify(ok, msg if not ok else f"'{pick}' is now your landing view.")
            if c3.button("Delete", key="views_delete", use_container_width=True):
                ok, msg = execute_statement(prefs_sql.delete_pref_sql(f"VIEW:{pick}"), page="Views")
                notify(ok, msg if not ok else f"Deleted '{pick}'.")

        name = st.text_input("Save current filters as", key="views_name", max_chars=40,
                             placeholder="e.g. Trexis prod 7d")
        clean = name.strip()
        if st.button("Save view", key="views_save",
                     disabled=not (clean and prefs_sql.VIEW_NAME_RE.match(clean))):
            ok, msg = execute_statement(
                prefs_sql.upsert_pref_sql(f"VIEW:{clean}", _current_view_payload()), page="Views")
            notify(ok, msg if not ok else f"Saved '{clean}' (page, section, and filters).")
        st.divider()
        current_tz = st.session_state.get("_ow_display_tz") or prefs_sql.DISPLAY_TIMEZONES[0]
        tz_idx = (prefs_sql.DISPLAY_TIMEZONES.index(current_tz)
                  if current_tz in prefs_sql.DISPLAY_TIMEZONES else 0)
        tz_pick = st.selectbox("Display timezone", prefs_sql.DISPLAY_TIMEZONES, index=tz_idx,
                               key="views_tz",
                               help="Display-only: tables and the timeline convert; SQL, alerts, "
                                    "and exports stay in account time (America/Chicago).")
        if st.button("Save timezone", key="views_tz_save"):
            st.session_state["_ow_display_tz"] = tz_pick
            ok, msg = execute_statement(prefs_sql.upsert_pref_sql("DISPLAY_TZ", tz_pick), page="Views")
            notify(ok, msg if not ok else f"Times will display in {tz_pick}.")
        if has_default and st.button("Clear default landing", key="views_clear_default"):
            ok, msg = execute_statement(prefs_sql.delete_pref_sql("DEFAULT_VIEW"), page="Views")
            notify(ok, msg if not ok else "Default cleared — app opens on Overview again.")


def _apply_default_landing() -> None:
    """Once per session: land on the user's saved default view. An explicit
    ?page= deep link always wins over the default."""
    if st.session_state.get("_ow_default_applied"):
        return
    st.session_state["_ow_default_applied"] = True
    try:
        if st.query_params.get("page"):
            return
    except Exception:  # noqa: BLE001
        pass
    from app.core.state import consume_pending_navigation
    from app.data import prefs_sql

    prefs = run(prefs_sql.user_prefs(), page="Views", key="user_prefs", tier="live",
                source="USER_PREFS")
    if not prefs.ok or prefs.empty:
        return
    tz_pref = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                    if str(r["PREF_KEY"]) == "DISPLAY_TZ"), "")
    if tz_pref:
        st.session_state["_ow_display_tz"] = tz_pref
    raw = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                if str(r["PREF_KEY"]) == "DEFAULT_VIEW"), "")
    data = _parse_view(raw)
    if data:
        st.session_state["_ow_nav_pending"] = {
            "page": str(data.get("page") or ""),
            "section": str(data.get("section") or ""),
            "filters": dict(data.get("filters") or {}),
        }
        consume_pending_navigation()  # pre-widget: applies immediately, no rerun


def _log_usage(page: str) -> None:
    """Usage analytics (APP_USAGE): one row per page change per session.
    Best-effort — first failure disables logging for the session."""
    if st.session_state.get("_ow_usage_off") or st.session_state.get("_ow_last_logged") == page:
        return
    st.session_state["_ow_last_logged"] = page
    ok, _msg = execute_statement(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE) "
        f"SELECT {sql_literal(str(page)[:80])}", page="Sidebar")
    if not ok:
        st.session_state["_ow_usage_off"] = True


def _global_jump(pages: tuple) -> None:
    """Jump-to: pages, databases, warehouses, alert rules — one box."""
    from app.companies import ALFA_DATABASES, TREXIS_DATABASES, TREXIS_WAREHOUSES

    options = [f"Page · {p}" for p in pages]
    options += [f"DB · {d}" for d in sorted(set(ALFA_DATABASES) | set(TREXIS_DATABASES))]
    wh_names = list(TREXIS_WAREHOUSES)
    whs = run(security_sql.show_warehouses_sql(), page="Sidebar", key="jump_wh",
              tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    if whs.ok and not whs.empty:
        wdf = whs.df.copy()
        wdf.columns = [str(c).lower() for c in wdf.columns]
        if "name" in wdf.columns:
            wh_names = sorted(set(wdf["name"].astype(str)))
    options += [f"WH · {w}" for w in wh_names]
    rules = run(mart_sql.alert_rules(), page="Sidebar", key="jump_rules", tier="recent",
                source="ALERT_CONFIG")
    if rules.usable() and "RULE_ID" in rules.df.columns:
        options += [f"Rule · {r}" for r in sorted(rules.df["RULE_ID"].astype(str))]
    pick = st.selectbox("Jump to", options, index=None, placeholder="Jump to…",
                        key="_ow_jump", label_visibility="collapsed")
    if not pick:
        return
    kind, _, name = pick.partition(" · ")
    if kind == "Page":
        request_navigation(name)
    elif kind == "DB":
        request_navigation("Operations", "Queries", {"database": name})
    elif kind == "WH":
        request_navigation("Operations", "Warehouses", {"warehouse_contains": name})
    elif kind == "Rule":
        request_navigation("Alerts", "Rules")


_STRIP_COLORS = {"OK": "#22c55e", "WARN": "#f59e0b", "BAD": "#ef4444",
                 "INFO": "#38bdf8", "MUTED": "#94a3b8"}


def _strip_line(state: str, text: str) -> None:
    color = _STRIP_COLORS.get(state, _STRIP_COLORS["MUTED"])
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{color};'
        f'display:inline-block;flex:none;" role="img" aria-label="{state}"></span>'
        f'<span style="font-size:0.8rem;opacity:0.85;">{text}</span></div>',
        unsafe_allow_html=True,
    )


def _health_strip() -> None:
    """Always-visible pulse: criticals, telemetry freshness, MTD credits.
    You should not have to visit Overview to know something is red."""
    res = run(mart_sql.health_strip(), page="Sidebar", key="health_strip", tier="live",
              source="ALERT_EVENTS + MART_SOURCE_FRESHNESS + FACT_METERING_DAILY")
    if not res.ok or res.empty:
        return
    vals = {str(r["METRIC"]): (str(r["VALUE"]), str(r["STATE"])) for _, r in res.df.iterrows()}
    crit, crit_state = vals.get("OPEN_CRITICAL", ("0", "OK"))
    if crit_state == "BAD":
        if st.button(f"{crit} open critical(s) →", key="strip_crit", use_container_width=True,
                     type="primary"):
            request_navigation("Alerts", "Open events")
    else:
        _strip_line("OK", "No open criticals")
    stale, stale_state = vals.get("STALEST_SOURCE_H", ("-1", "MUTED"))
    if stale != "-1":
        _strip_line(stale_state, f"Stalest telemetry: {stale}h")
    mtd, _ = vals.get("MTD_CREDITS", ("", ""))
    if mtd:
        _strip_line("INFO", f"MTD: {float(mtd):,.0f} credits")


def _topbar_scope() -> None:
    """Triage filter strip above every page, like the original OVERWATCH."""
    box = st.container(border=True)
    with box:
        head_l, head_m, head_r = st.columns([3.6, 1.4, 1])
        with head_l:
            st.markdown('<div class="ow-kicker">Triage filters</div>', unsafe_allow_html=True)
        with head_m:
            strip_res = run(mart_sql.health_strip(), page="Sidebar", key="health_strip",
                            tier="live", source="freshness")
            if strip_res.ok and not strip_res.empty:
                vals = {str(r["METRIC"]): str(r["VALUE"]) for _, r in strip_res.df.iterrows()}
                stale_h = vals.get("STALEST_SOURCE_H", "-1")
                if stale_h not in ("-1", ""):
                    st.caption(f"Telemetry ≤ {stale_h}h old")
        with head_r:
            _views_popover()
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
    _apply_default_landing()
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
