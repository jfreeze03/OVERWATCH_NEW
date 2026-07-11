"""App shell: sidebar navigation, global filters, page dispatch."""

from __future__ import annotations

import time

import streamlit as st

st.set_page_config(
    page_title="OVERWATCH — Snowflake Command Center",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from app.companies import COMPANIES, ENVIRONMENTS, databases_for  # noqa: E402
from app.config import (  # noqa: E402
    APP_VERSION,
    DAY_WINDOW_OPTIONS,
    PAGES_BY_PROFILE,
    resolve_role_profile,
)
from app.core.query import (  # noqa: E402
    bump_refresh_salt,
    execute_statement,
    execute_statement_async,
    run,
)
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
from app.ui.components import mark_refreshed, notify  # noqa: E402
from app.ui.icons import icon  # noqa: E402
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

# Nav labels are plain text (st.radio can't render markup); the sidebar CSS
# active-rail shows position, and each page's header carries its SVG icon.
# This removes the inconsistent emoji CoCo flagged, cleanly.

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


def _sidebar(pages: tuple[str, ...], role: str, profile: str, connected: bool,
             health_vals: dict | None = None) -> str:
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
        if connected:
            from app.ui.components import last_refreshed_note
            st.markdown(
                f'<div style="font-size:0.72rem;color:var(--ow-ink-mute);margin-top:2px">'
                f'{icon("refresh", 11)} {last_refreshed_note()}</div>', unsafe_allow_html=True)
        st.divider()

        default_page = requested_page(pages) or st.session_state.get("_ow_page") or pages[0]
        if default_page not in pages:
            default_page = pages[0]
        st.caption("Navigate")
        page = st.radio("Navigate", pages, index=pages.index(default_page),
                        key="_ow_nav_radio", label_visibility="collapsed")
        st.session_state["_ow_page"] = page
        remember_page(page)

        st.divider()
        _global_jump(pages)
        _health_strip(health_vals)
        if st.button("Refresh data", use_container_width=True):
            bump_refresh_salt()
            # Re-resolve the role too: a grant/role change mid-session should
            # be picked up here, not only on a full browser reload.
            st.session_state.pop("_ow_current_role", None)
            st.session_state.pop("_ow_current_user", None)
            mark_refreshed()
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


@st.fragment
def _views_popover() -> None:
    """Saved filter views + default landing (USER_PREFS, V013)."""
    from app.core.state import request_navigation
    from app.data import prefs_sql
    from app.ui.components import legend_popover
    legend_popover()
    with st.popover("Views"):
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
                from app.ui.components import log_ui_event
                log_ui_event("saved_view_apply")
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
        compact = st.toggle("Compact density", key="views_density",
                            value=st.session_state.get("_ow_density") == "compact",
                            help="Tighter cards and tables for triage screens; "
                                 "hierarchy and colors unchanged.")
        st.session_state["_ow_density"] = "compact" if compact else "comfortable"
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


def _log_usage(page: str, render_ms: int | None = None) -> None:
    """Usage analytics (APP_USAGE). First paint per page logs RENDER_MS (the
    p95 the OPS_SLOW_RENDER sentinel checks); same-page reruns log a sampled
    (10%) EVENT_KIND='rerun' row with RENDER_MS NULL so interaction volume is
    measurable WITHOUT polluting the first-paint p95 (V027 rider; the scan
    gains an IS_RERUN filter with V028). Best-effort; degrades to the
    pre-V027 column shape, then off entirely."""
    if st.session_state.get("_ow_usage_off"):
        return
    is_rerun = st.session_state.get("_ow_last_logged") == page
    if is_rerun:
        import random as _random
        if _random.random() >= 0.10:
            return
        kind, ms = "rerun", "NULL"
    else:
        st.session_state["_ow_last_logged"] = page
        kind = "page_visit"
        ms = "NULL" if render_ms is None else str(max(0, min(int(render_ms), 600000)))
    if not st.session_state.get("_ow_usage_oldshape"):
        ok = execute_statement_async(
            "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS, EVENT_KIND, IS_RERUN) "
            f"SELECT {sql_literal(str(page)[:80])}, {ms}, {sql_literal(kind)}, "
            f"{'TRUE' if is_rerun else 'FALSE'}", page="Sidebar")
        if ok:
            return
        st.session_state["_ow_usage_oldshape"] = True
    if is_rerun:
        return
    ok = execute_statement_async(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) "
        f"SELECT {sql_literal(str(page)[:80])}, {ms}", page="Sidebar")
    if not ok:
        st.session_state["_ow_usage_off"] = True


def _global_jump(pages: tuple) -> None:
    """Jump-to: pages, databases, warehouses, alert rules — one box."""
    from app.companies import ALFA_DATABASES, TREXIS_DATABASES, TREXIS_WAREHOUSES

    options = [f"Page · {p}" for p in pages]
    options += [f"DB · {d}" for d in sorted(set(ALFA_DATABASES) | set(TREXIS_DATABASES))]
    # Live targets (SHOW WAREHOUSES + alert rules) load on demand: a normal
    # page paint pays ZERO queries for the jump box (Codex #3). Static pages,
    # databases, and the known Trexis warehouses are always offered; picking
    # the loader row fetches the full account list once per session.
    if bool(st.session_state.get("_ow_jump_loaded")):
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
    else:
        options += [f"WH · {w}" for w in TREXIS_WAREHOUSES]
        options.append("More · load all warehouses & alert rules…")
    pick = st.selectbox("Jump to", options, index=None, placeholder="Jump to…",
                        key="_ow_jump", label_visibility="collapsed")
    if not pick:
        return
    kind, _, name = pick.partition(" · ")
    if kind == "More":
        st.session_state["_ow_jump_loaded"] = True
        st.session_state.pop("_ow_jump", None)
        st.rerun()
    elif kind == "Page":
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
    import html as _html

    color = _STRIP_COLORS.get(state, _STRIP_COLORS["MUTED"])
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{color};'
        f'display:inline-block;flex:none;" role="img" aria-label="{_html.escape(state)}"></span>'
        f'<span style="font-size:0.8rem;opacity:0.85;">{_html.escape(text)}</span></div>',
        unsafe_allow_html=True,
    )


def _health_values() -> dict[str, tuple[str, str]]:
    """One fetch+parse of the health-strip mart, shared by the sidebar strip,
    the persistent status bar, and the top bar (they used to parse it thrice
    with three different source labels)."""
    res = run(mart_sql.health_strip(), page="Sidebar", key="health_strip", tier="live",
              source="ALERT_EVENTS + MART_SOURCE_FRESHNESS + FACT_METERING_DAILY")
    if not res.ok or res.empty:
        return {}
    return {str(r["METRIC"]): (str(r["VALUE"]), str(r["STATE"])) for _, r in res.df.iterrows()}


def _health_strip(vals: dict | None = None) -> None:
    """Always-visible pulse: criticals, telemetry freshness, MTD credits.
    You should not have to visit Overview to know something is red."""
    vals = _health_values() if vals is None else vals
    if not vals:
        return
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


def _persistent_status_bar(vals: dict | None = None) -> None:
    """The 3-4 numbers that matter, on every page (CoCo high item)."""
    from app.ui.components import status_bar
    vals = _health_values() if vals is None else vals
    if not vals:
        return
    crit, _ = vals.get("OPEN_CRITICAL", ("0", "OK"))
    stale, stale_state = vals.get("STALEST_SOURCE_H", ("-1", "MUTED"))
    mtd, _ = vals.get("MTD_CREDITS", ("", ""))
    _sev = {"BAD": "bad", "WARN": "warn", "OK": "ok", "INFO": "info", "MUTED": ""}
    from app.core.state import filters as _flt
    _f = _flt()
    stats = [
        {"k": "Scope", "v": f"{_f['company']} · {_f['environment']} · {_f['days']}d",
         "icon": "refresh", "sev": ""},
        {"k": "Open criticals", "v": crit, "icon": "alerts",
         "sev": "bad" if crit not in ("0", "") else "ok"},
        {"k": "Telemetry age", "v": (f"{stale}h" if stale != "-1" else "n/a"),
         "icon": "clock", "sev": _sev.get(stale_state, "")},
    ]
    if mtd:
        try:
            stats.append({"k": "MTD credits", "v": f"{float(mtd):,.0f}", "icon": "cost", "sev": "info"})
        except (TypeError, ValueError):
            pass
    status_bar(stats)


def _topbar_scope(health_vals: dict | None = None) -> None:
    """Triage filter strip above every page, like the original OVERWATCH."""
    box = st.container(border=True)
    with box:
        head_l, head_m, head_r = st.columns([3.6, 1.4, 1])
        with head_l:
            st.markdown('<div class="ow-kicker">Triage filters</div>', unsafe_allow_html=True)
        with head_m:
            vals = _health_values() if health_vals is None else health_vals
            stale_h = vals.get("STALEST_SOURCE_H", ("-1", ""))[0] if vals else "-1"
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
        # Options honor BOTH scopes: ALFA + PROD offers exactly the two PROD
        # databases, not the whole family (live finding, 2026-07-08).
        db_options = ["", *databases_for(
            st.session_state.get("flt_company", COMPANIES[0]),
            st.session_state.get("flt_environment", ENVIRONMENTS[0]))]
        if st.session_state.get("flt_database") not in db_options:
            st.session_state["flt_database"] = ""
        st.selectbox("Database", db_options, key="flt_database",
                     format_func=lambda v: v or "All databases",
                     help="Applies to query, task, DDL, attribution, and storage panels. "
                          "Options track the Company and Environment filters.")
    # Collapsed by default (Codex r4 #1): the scope row above answers 90% of
    # visits; the contains-filters open automatically whenever one is active
    # so a live filter can never hide.
    _adv_on = any(str(st.session_state.get(k) or "").strip() for k in
                  ("flt_warehouse_contains", "flt_user_contains", "flt_schema_contains"))
    with st.expander("More filters — warehouse / user / schema contains", expanded=_adv_on):
        c_wh, c_user, c_schema = st.columns([1.2, 1.2, 1.2])
        with c_wh:
            st.text_input("Warehouse contains", key="flt_warehouse_contains")
        with c_user:
            st.text_input("User contains", key="flt_user_contains")
        with c_schema:
            st.text_input("Schema contains", key="flt_schema_contains",
                          help="Case-insensitive match where the source has schema grain.")


def main() -> None:
    _main_started = time.perf_counter()  # full render incl. chrome (Codex #18)
    consume_pending_navigation()
    inject_theme()
    if "_ow_refreshed_at" not in st.session_state:
        mark_refreshed()
    init_filters()

    connected = connection_available()
    role = current_role() if connected else ""   # hydrates role+user scope keys
    # Codex r9 #1 (real): the USER_PREFS read used to run BEFORE identity
    # hydrated, so it cached under the anonymous scope — and since the SQL
    # text is identical across users, one user's prefs frame could serve
    # another in-process. Identity first, THEN the first cached read.
    _apply_default_landing()
    profile = resolve_role_profile(role)
    pages = PAGES_BY_PROFILE.get(profile, PAGES_BY_PROFILE["ANALYST"])

    # One health fetch + parse per rerun, shared by the sidebar strip, the
    # top bar, and the status bar (Codex #1 — was fetched/parsed three times).
    health_vals = _health_values() if connected else {}
    page = _sidebar(pages, role, profile, connected, health_vals)
    if connected:
        _topbar_scope(health_vals)

    if not connected:
        st.title("OVERWATCH")
        st.error("No Snowflake connection.")
        st.markdown(
            "- **Streamlit-in-Snowflake:** the session is injected automatically — if you see this "
            "in SiS, the app's owner role lost access.\n"
            "- **Local dev:** add `[connections.snowflake]` to `.streamlit/secrets.toml` "
            "(see DEPLOYMENT.md).\n"
        )
        from app.core.session import connection_error
        reason = connection_error()
        if reason:
            with st.expander("Connection error detail"):
                st.code(reason)
        if st.button("Retry connection"):
            st.cache_resource.clear()
            st.session_state.pop("_ow_current_role", None)
            st.rerun()
        return

    if page != "Brief":  # Brief is already the compact status view
        _persistent_status_bar(health_vals)
    _RENDERERS[page]()
    # RENDER_MS now spans sidebar/topbar/status chrome too, not just the page
    # body — chrome overhead was invisible in APP_USAGE (Codex #18).
    _log_usage(page, int((time.perf_counter() - _main_started) * 1000))


if __name__ == "__main__":
    main()
