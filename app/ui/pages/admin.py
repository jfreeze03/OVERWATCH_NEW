"""Admin — settings, migration status, self-cost, error log, telemetry.

Everything that was wrongly parked on the old app's executive page lives
here, where the people who can act on it will look for it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import (
    APP_VERSION,
    DEFAULT_SETTINGS,
    OPERATOR_PROFILES,
    core_object,
    resolve_role_profile,
)
from app.core.errors import error_buffer, safe_page
from app.core.query import bump_refresh_salt, execute_statement, query_telemetry, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.data import mart_sql
from app.ui.components import guard, kpi_row, load_settings, page_header, result_caption

_PAGE = "Admin"
_EXPECTED_MIGRATIONS = {
    1: "core", 2: "facts", 3: "marts", 4: "alerts", 5: "actions", 6: "pipeline sla",
}


def _context_section() -> None:
    ctx = run(
        "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_REGION() AS REGION, CURRENT_ROLE() AS ROLE, "
        "CURRENT_WAREHOUSE() AS WAREHOUSE, CURRENT_VERSION() AS SNOWFLAKE_VERSION",
        page=_PAGE, key="context", tier="metadata", source="session context",
    )
    if ctx.usable():
        row = ctx.df.iloc[0]
        kpi_row([
            {"label": "Role", "value": str(row.get("ROLE", "?"))},
            {"label": "Warehouse", "value": str(row.get("WAREHOUSE", "?") or "none")},
            {"label": "Account", "value": str(row.get("ACCOUNT", "?"))},
            {"label": "App version", "value": APP_VERSION},
        ])
    elif not ctx.ok:
        st.error(f"No Snowflake session: {ctx.error}")


def _settings_tab(is_operator: bool) -> None:
    settings = load_settings(_PAGE)
    st.caption(f"Values from: {settings.get('_source')}. Rates confirmed 2026-07: $3.68 compute / $2.20 Cortex.")
    res = run(mart_sql.settings(), page=_PAGE, key="settings_table", tier="live",
              source="SETTINGS")
    if guard(res, "SETTINGS is empty.", setup_hint="Run migration V001 to create and seed it."):
        st.dataframe(res.df, hide_index=True, use_container_width=True)
        result_caption(res)

    st.markdown("**Change a setting**")
    editable = [k for k in DEFAULT_SETTINGS if not k.startswith("_")]
    key = st.selectbox("Setting", editable, key="adm_setting_key")
    new_value = st.text_input("New value", key="adm_setting_value",
                              help="Numeric settings take numbers; dates are YYYY-MM-DD; blank clears.")
    update_sql = (
        f"UPDATE {core_object('SETTINGS')} SET VALUE = {sql_literal(new_value)}, "
        "UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = CURRENT_USER() "
        f"WHERE KEY = {sql_literal(key)};"
    )
    st.code(update_sql, language="sql")
    if is_operator:
        confirm = st.text_input("Type the setting key to confirm", key="adm_setting_confirm")
        if st.button("Execute update", key="adm_setting_exec", disabled=(confirm != key)):
            ok, msg = execute_statement(update_sql, page=_PAGE)
            (st.success if ok else st.error)(msg)
            if ok:
                st.caption("New value takes effect within one cache cycle (≤5 min) or after Refresh.")
    else:
        st.caption("Executing requires the OVERWATCH_OPERATOR role; anyone can copy the SQL for review.")


def _migrations_tab() -> None:
    res = run(mart_sql.schema_version(), page=_PAGE, key="schema_version", tier="live",
              source="SCHEMA_VERSION")
    if not res.ok:
        st.error(f"Cannot read SCHEMA_VERSION: {res.error}")
        st.info("Run snowflake/migrations/V001__core.sql first.")
        return
    applied = set()
    if not res.empty:
        applied = {int(v) for v in pd.to_numeric(res.df["VERSION"], errors="coerce").dropna()}
        st.dataframe(res.df, hide_index=True, use_container_width=True)
    missing = [f"V{n:03d} ({name})" for n, name in _EXPECTED_MIGRATIONS.items() if n not in applied]
    if missing:
        st.warning("Missing migrations: " + ", ".join(missing) + ". Run them in order (DEPLOYMENT.md).")
    else:
        st.success(f"All {len(_EXPECTED_MIGRATIONS)} migrations applied. App {APP_VERSION} expects exactly these.")

    st.markdown("**Telemetry freshness**")
    fresh = run(mart_sql.source_freshness(), page=_PAGE, key="adm_freshness", tier="live",
                source="MART_SOURCE_FRESHNESS")
    if guard(fresh, "Freshness view empty — have the loader tasks run yet?",
             setup_hint="Tasks resume at the end of V004. Check SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH."):
        st.dataframe(fresh.df, hide_index=True, use_container_width=True)


def _self_cost_tab() -> None:
    st.caption(
        "The monitoring app must never become the cost problem: WH_ALFA_OVERWATCH is XSMALL with a "
        "30-credit monthly resource monitor, and every app query carries an OVERWATCH query tag."
    )
    res = run(mart_sql.app_self_cost(14), page=_PAGE, key="self_cost", tier="historical",
              source="ACCOUNT_USAGE.QUERY_HISTORY (QUERY_TAG LIKE 'OVERWATCH%')")
    if guard(res, "No tagged OVERWATCH queries in the last 14 days (fresh install, or tags disabled)."):
        df = res.df.copy()
        total = int(pd.to_numeric(df["APP_QUERIES"], errors="coerce").fillna(0).sum())
        failed = int(pd.to_numeric(df["FAILED"], errors="coerce").fillna(0).sum())
        kpi_row([
            {"label": "App queries (14d)", "value": f"{total:,}"},
            {"label": "Failed", "value": f"{failed:,}",
             "delta_color": "inverse" if failed else "off"},
        ])
        st.dataframe(df, hide_index=True, use_container_width=True)
        result_caption(res)


def _observability_tab() -> None:
    st.markdown("**Recent app errors (this session)**")
    buffer = error_buffer()
    if not buffer:
        st.success("No errors recorded in this session.")
    else:
        st.dataframe(pd.DataFrame(buffer)[["at", "page", "type", "message"]],
                     hide_index=True, use_container_width=True)
    sink = run(mart_sql.app_error_log(100), page=_PAGE, key="error_sink", tier="live",
               source="APP_ERROR_LOG")
    st.markdown("**Persisted error log (all sessions)**")
    if sink.ok and sink.empty:
        st.success("Error sink is empty.")
    elif guard(sink, "", setup_hint="Sink table comes from V001."):
        st.dataframe(sink.df, hide_index=True, use_container_width=True)

    st.markdown("**Query telemetry (this session)**")
    telemetry = query_telemetry()
    if telemetry.empty:
        st.caption("No queries have run yet this session.")
    else:
        st.dataframe(telemetry.sort_values("at", ascending=False),
                     hide_index=True, use_container_width=True)

    if st.button("Refresh all cached data", key="adm_refresh"):
        bump_refresh_salt()
        st.rerun()


@safe_page(_PAGE)
def render() -> None:
    page_header("Admin", "Settings, migrations, self-cost, and app observability.")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    _context_section()
    tab_settings, tab_migrations, tab_cost, tab_obs = st.tabs(
        ["Settings", "Migrations & freshness", "App self-cost", "Errors & telemetry"]
    )
    with tab_settings:
        _settings_tab(is_operator)
    with tab_migrations:
        _migrations_tab()
    with tab_cost:
        _self_cost_tab()
    with tab_obs:
        _observability_tab()
