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
from app.data import chargeback_sql, cost_sql, mart_sql, ops_sql, security_sql
from app.logic import remediation
from app.logic.formulas import safe_float
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    panel_help,
    result_caption,
    selectable_table,
    styled_table,
)

_PAGE = "Admin"
_EXPECTED_MIGRATIONS = {
    1: "core", 2: "facts", 3: "marts", 4: "alerts", 5: "actions", 6: "pipeline sla",
    7: "automation", 8: "chargeback", 9: "credentials", 10: "change impact",
    11: "proactive alerts", 12: "routing + anomaly sweep", 13: "user prefs",
    14: "lifecycle hardening", 15: "DT pilot + backups", 16: "closing loops",
    17: "hardening v7", 18: "delivery first-class", 19: "scoping fixes", 20: "credentials column",
    21: "precision + telemetry", 22: "delivery per route", 23: "prod-scoped volume",
    24: "warehouse change scorecard", 25: "break-glass policy", 26: "teams-safe delivery",
    27: "mart family + telemetry rider",
    28: "credential expiry 10d (rule + posture bucket)",
    29: "loader fix: role/schema-hour GROUP BY",
    30: "loader fix 2 (UDF outside aggregation) + posture inputs",
    31: "change-impact scan v2 + tag-coverage mart",
    32: "incident object (tables + lineage + auto-declare)",
    33: "change attribution (CHANGED_BY + DEPLOY_ACTORS)",
    34: "route company filter (sender v4, ALFA-only for now)",
    35: "lock-wait mart (page views never scan LOCK_WAIT_HISTORY)",
}
# tests/test_perf_budgets.py locks this dict against snowflake/migrations/ —
# adding a migration without updating it fails CI (Codex r3 #1: the panel
# reported "all applied" while V021-V025 were missing from the expectation).


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
        styled_table(res.df)
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
            notify(ok, msg)
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
        styled_table(res.df)
    missing = [f"V{n:03d} ({name})" for n, name in _EXPECTED_MIGRATIONS.items() if n not in applied]
    if missing:
        st.warning("Missing migrations: " + ", ".join(missing) + ". Run them in order (DEPLOYMENT.md).")
    else:
        st.success(f"All {len(_EXPECTED_MIGRATIONS)} migrations applied. App {APP_VERSION} expects exactly these.")

    fh = run(mart_sql.flyway_history(), page=_PAGE, key="flyway_history", tier="live",
             source="flyway_schema_history (Flyway ledger)", probe=True)
    if fh.usable():
        st.markdown("**Flyway deploy history** — the transport's own ledger")
        styled_table(fh.df, height=220)
        st.caption("Flyway owns WHAT ran WHEN once adopted; SCHEMA_VERSION above stays "
                   "the app's contract check (and the in-file guards stay as defense "
                   "against Snowsight bypass). Adoption runbook: docs/FLYWAY_ADOPTION.md.")
    else:
        st.caption("Flyway not detected — SCHEMA_VERSION above is authoritative. When "
                   "procurement lands, docs/FLYWAY_ADOPTION.md is the adoption runbook; "
                   "this panel lights up on its own once flyway_schema_history exists.")

    st.markdown("**Telemetry freshness**")
    fresh = run(mart_sql.source_freshness(), page=_PAGE, key="adm_freshness", tier="live",
                source="MART_SOURCE_FRESHNESS")
    if guard(fresh, "Freshness view empty — have the loader tasks run yet?",
             setup_hint="Tasks resume at the end of V004. Check SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH."):
        styled_table(fresh.df)
        with st.expander("Why stale? — diagnose without reading raw errors"):
            # The deploy-gap week (2026-07): stale marts meant a failing
            # loader, a never-run backfill, or a suspended task. Map each
            # stale source to its likeliest cause from evidence we hold.
            errs = run(mart_sql.app_error_log(100), page=_PAGE, key="adm_stale_errs",
                       tier="live", source="APP_ERROR_LOG")
            try:
                stale = fresh.df[fresh.df["HOURS_SINCE_LOAD"].astype(float) > 26]
            except (KeyError, TypeError, ValueError):
                stale = fresh.df.iloc[0:0]
            if stale.empty:
                st.success("Nothing stale past 26h — the loaders are keeping up.")
            for _, s in stale.iterrows():
                name = str(s["SOURCE_NAME"])
                hint = ""
                if float(s.get("ROW_COUNT", 0) or 0) == 0:
                    hint = ("never filled — run the backfill "
                            "(RUNBOOK: SP_LOAD_MARTS_V27 HOURLY 90, then DAILY 3).")
                if errs.ok and not errs.empty:
                    _m = errs.df[errs.df.apply(
                        lambda r, _n=name: _n in str(r.get("CONTEXT", ""))
                        or _n in str(r.get("ERROR_MESSAGE", "")), axis=1)]
                    if not _m.empty:
                        _r0 = _m.iloc[0]
                        hint = (f"last loader error {_r0['LOGGED_AT']}: "
                                f"{str(_r0['ERROR_MESSAGE'])[:160]}")
                st.markdown(f"- **{name}** — {float(s['HOURS_SINCE_LOAD']):.0f}h since load. "
                            + (hint or "no matching error logged — check SHOW TASKS "
                                       "(tasks suspend if a migration half-applied)."))


_SCAN_NOTE = ("First load scans ACCOUNT_USAGE directly (a few seconds on a cold "
              "cache); results cache for an hour, so repeat views are instant.")


def _self_cost_tab() -> None:
    st.caption(
        "The monitoring app must never become the cost problem: WH_ALFA_OVERWATCH is XSMALL with a "
        "30-credit monthly resource monitor, and every app query carries an OVERWATCH query tag."
    )
    st.caption(_SCAN_NOTE)
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
        styled_table(df)
        result_caption(res)


def _observability_tab() -> None:
    st.markdown("**Recent app errors (this session)**")
    buffer = error_buffer()
    if not buffer:
        st.success("No errors recorded in this session.")
    else:
        styled_table(pd.DataFrame(buffer)[["at", "page", "type", "message"]])
    sink = run(mart_sql.app_error_log(100), page=_PAGE, key="error_sink", tier="live",
               source="APP_ERROR_LOG")
    st.markdown("**Persisted error log (all sessions)**")
    if sink.ok and sink.empty:
        st.success("Error sink is empty.")
    elif guard(sink, "", setup_hint="Sink table comes from V001."):
        styled_table(sink.df)

    st.markdown("**Query telemetry (this session)**")
    telemetry = query_telemetry()
    if telemetry.empty:
        st.caption("No queries have run yet this session.")
    else:
        styled_table(telemetry.sort_values("at", ascending=False))

    if st.button("Refresh all cached data", key="adm_refresh"):
        bump_refresh_salt()
        st.rerun()


def _org_spend_tab() -> None:
    """Accounts Spend Summary from ORGANIZATION_USAGE (currency, per account)."""
    st.caption(
        "Org-level billed spend in currency per account and usage type — the same source "
        "as Snowsight's Accounts Spend Summary (USAGE_IN_CURRENCY_DAILY, lags up to 24-72h)."
    )
    st.caption(_SCAN_NOTE)
    res = run(cost_sql.org_usage_in_currency(30), page=_PAGE, key="org_spend",
              tier="historical", source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY")
    if not res.ok:
        st.info(
            "ORGANIZATION_USAGE is not visible to this role/account. Grant the "
            "ORGANIZATION_USAGE_VIEWER application role (or enable org views on this account) "
            f"to light this up. Detail: {res.error}"
        )
        return
    if res.empty:
        st.info("No org usage rows in the last 30 days.")
        return
    df = res.df.copy()
    df["USAGE_IN_CURRENCY"] = pd.to_numeric(df["USAGE_IN_CURRENCY"], errors="coerce").fillna(0)
    currency = str(df["CURRENCY"].dropna().iloc[0]) if df["CURRENCY"].notna().any() else "USD"
    total = float(df["USAGE_IN_CURRENCY"].sum())
    by_account = (df.groupby("ACCOUNT_NAME", as_index=False)["USAGE_IN_CURRENCY"].sum()
                  .sort_values("USAGE_IN_CURRENCY", ascending=False))
    kpi_row([
        {"label": f"Org spend (30d, {currency})", "value": f"{total:,.0f}",
         "help": "Billed currency across every account in the organization."},
        {"label": "Accounts", "value": f"{by_account['ACCOUNT_NAME'].nunique()}"},
        {"label": "Largest account",
         "value": str(by_account.iloc[0]["ACCOUNT_NAME"]) if not by_account.empty else "n/a",
         "delta": f"{float(by_account.iloc[0]['USAGE_IN_CURRENCY']):,.0f} {currency}" if not by_account.empty else None,
         "delta_color": "off"},
    ])
    from app.ui import charts as _charts

    _charts.daily_stacked_usd(
        df.rename(columns={"USAGE_IN_CURRENCY": "USD"}), "DAY", "ACCOUNT_NAME", "USD")
    st.caption(f"Amounts are {currency} from the org rate card, not credits x app rate.")
    pivot = (df.groupby(["ACCOUNT_NAME", "USAGE_TYPE"], as_index=False)["USAGE_IN_CURRENCY"].sum()
             .sort_values(["ACCOUNT_NAME", "USAGE_IN_CURRENCY"], ascending=[True, False]))
    st.dataframe(pivot, hide_index=True, use_container_width=True,
                 column_config={"USAGE_IN_CURRENCY": st.column_config.NumberColumn(
                     f"Spend ({currency})", format="%.2f")})
    result_caption(res)

    st.divider()
    st.markdown("**Billing truth vs app model (this account)**")
    st.caption(
        "Org rate-card dollars for THIS account vs the app's credits x configured rate. "
        "The compute bucket should track closely; the residual is rate-card reality "
        "(storage, transfer, serverless, discounts), not a bug in either number."
    )
    org_m = run(cost_sql.org_account_month_usd(2), page=_PAGE, key="org_month_this",
                tier="historical", source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (this account)")
    model_m = run(mart_sql.fact_daily_spend(70), page=_PAGE, key="fact_daily_45",
                  tier="recent", source="FACT_METERING_DAILY")
    if not org_m.usable():
        st.info("Needs ORGANIZATION_USAGE visibility (see the note above).")
    elif not model_m.usable():
        st.info("Needs the daily metering facts (V002) for the model side.")
    else:
        rate_now = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
        mdf = model_m.df.copy()
        mdf["MONTH"] = pd.to_datetime(mdf["DAY"], errors="coerce").dt.to_period("M").dt.to_timestamp()
        model_by_month = mdf.groupby("MONTH")["CREDITS_BILLED"].sum() * rate_now
        odf = org_m.df.copy()
        odf["MONTH"] = pd.to_datetime(odf["MONTH"], errors="coerce")
        rows_rc = []
        for _, orow in odf.iterrows():
            month = orow["MONTH"]
            model_usd = float(model_by_month.get(month, 0.0))
            org_usd = safe_float(orow.get("COMPUTE_USD"))
            drift = (100.0 * (model_usd - org_usd) / org_usd) if org_usd else None
            rows_rc.append({
                "MONTH": month.strftime("%Y-%m") if pd.notna(month) else "?",
                "ORG_COMPUTE_USD": round(org_usd, 2),
                "APP_MODEL_USD": round(model_usd, 2),
                "DELTA_PCT": round(drift, 2) if drift is not None else None,
                "ORG_TOTAL_USD": round(safe_float(orow.get("TOTAL_USD")), 2),
            })
        styled_table(pd.DataFrame(rows_rc), column_config={
            "DELTA_PCT": st.column_config.NumberColumn("Model vs org %", format="%.2f%%")})
        st.caption(
            f"Model = FACT_METERING_DAILY billed credits x ${rate_now:.2f} (SETTINGS). The current "
            "month is partial on both sides; judge the prior month. A steady gap means the "
            "contract rate in SETTINGS no longer matches the rate card — fix it on Settings."
        )


_EMERGENCY_CATALOG = """
| Lever | Statement | When |
|---|---|---|
| Suspend warehouse | `ALTER WAREHOUSE <wh> SUSPEND` | Runaway spend — the kill-switch. Billing stops when running queries end. |
| Resume warehouse | `ALTER WAREHOUSE <wh> RESUME` | After the fix. |
| Statement timeout (WH) | `SET STATEMENT_TIMEOUT_IN_SECONDS = n` | Queries running for hours; caps every new statement on that warehouse. |
| Cluster range | `SET MIN/MAX_CLUSTER_COUNT` | Multi-cluster fan-out burning credits, or raise it during a queue emergency. |
| Scaling policy | `SET SCALING_POLICY = ECONOMY` | Slows cluster spawn during bursty-but-tolerant loads. |
| Resource monitor quota | `ALTER RESOURCE MONITOR ... SET CREDIT_QUOTA = n` | The hard monthly brake; SUSPEND_IMMEDIATE trigger kills at the cap. |
| Attach monitor | `SET RESOURCE_MONITOR = <rm>` | Unmonitored warehouse found during an incident. |
| Warehouse size | `SET WAREHOUSE_SIZE = <size>` | Down = cost triage; up = performance firefight (use the remediation panel's resize). |
| Auto-suspend | `SET AUTO_SUSPEND = 60` | Idle-burn discovered mid-incident (remediation panel). |
| Pause pipe | `ALTER PIPE ... SET PIPE_EXECUTION_PAUSED = TRUE` | Ingestion flood / bad file loop. |
| Suspend task | `ALTER TASK <root> SUSPEND` | Runaway or failing task graph (suspend the ROOT). |
| Disable user | `ALTER USER <u> SET DISABLED = TRUE` | Compromised credentials — kills new sessions immediately. |
| Cortex model allowlist | `ALTER ACCOUNT SET CORTEX_MODELS_ALLOWLIST = 'None'` | AI spend kill-switch (Cortex Code / LLM functions). **Account-level: run as SNOW_ACCOUNTADMINS.** |
| Account stmt timeout | `ALTER ACCOUNT SET STATEMENT_TIMEOUT_IN_SECONDS = n` | Global default cap. **Account-level.** |
| Network policy | `ALTER ACCOUNT SET NETWORK_POLICY = <p>` | Access lockdown. **Account-level; not generated here — coordinate before locking yourself out.** |
"""


def _emergency_tab(is_operator: bool) -> None:
    """On-the-fly incident levers: generate exact SQL, confirm, execute, audit."""
    st.caption(
        "Every execution writes a REMEDIATION_LOG audit row (append-only). Warehouse/"
        "pipe/task/user levers run under your role; ACCOUNT-level levers (Cortex "
        "allowlist, account timeout) need SNOW_ACCOUNTADMINS — the SQL is still "
        "generated here for copy-paste."
    )
    panel_help(
        "The catalogue below is the education; the generator builds exact statements "
        "with validated identifiers. Suspending a warehouse does not kill in-flight "
        "queries — pair with a statement timeout when something is stuck. Resource "
        "monitor quota changes take effect immediately; Cortex allowlist changes "
        "apply account-wide within minutes."
    )
    with st.expander("Known emergency levers (reference)", expanded=False):
        st.markdown(_EMERGENCY_CATALOG)

    whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="emg_show_wh",
              tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    wh_names = []
    if whs.ok and not whs.empty:
        wdf = whs.df.copy()
        wdf.columns = [str(c).lower() for c in wdf.columns]
        if "name" in wdf.columns:
            wh_names = sorted(wdf["name"].astype(str))

    action = st.selectbox("Lever", [
        "Suspend warehouse", "Resume warehouse", "Warehouse statement timeout",
        "Cluster range", "Scaling policy", "Resource monitor quota",
        "Attach resource monitor", "Pause pipe", "Resume pipe", "Suspend task",
        "Resume task", "Disable user", "Re-enable user",
        "Cortex allowlist (ACCOUNT)", "Account statement timeout (ACCOUNT)",
    ], key="emg_action")

    stmt = ""
    try:
        if action in ("Suspend warehouse", "Resume warehouse", "Warehouse statement timeout",
                      "Cluster range", "Scaling policy", "Attach resource monitor"):
            wh = (st.selectbox("Warehouse", wh_names, key="emg_wh") if wh_names
                  else st.text_input("Warehouse", key="emg_wh_txt"))
            if action == "Suspend warehouse" and wh:
                stmt = remediation.suspend_warehouse(wh)
            elif action == "Resume warehouse" and wh:
                stmt = remediation.resume_warehouse(wh)
            elif action == "Warehouse statement timeout" and wh:
                secs = st.number_input("Timeout seconds (0 = no cap)", 0, 604800, 3600,
                                       step=300, key="emg_secs")
                stmt = remediation.statement_timeout_fix(wh, int(secs))
            elif action == "Cluster range" and wh:
                c1, c2 = st.columns(2)
                lo = c1.number_input("Min clusters", 1, 10, 1, key="emg_min")
                hi = c2.number_input("Max clusters", 1, 10, 1, key="emg_max")
                stmt = remediation.cluster_range_fix(wh, int(lo), int(hi))
            elif action == "Scaling policy" and wh:
                pol = st.radio("Policy", ["ECONOMY", "STANDARD"], horizontal=True, key="emg_pol")
                stmt = remediation.scaling_policy_fix(wh, pol)
            elif action == "Attach resource monitor" and wh:
                mon = st.text_input("Resource monitor name", "OVERWATCH_RM", key="emg_mon")
                if mon:
                    stmt = remediation.attach_resource_monitor(wh, mon)
        elif action == "Resource monitor quota":
            mon = st.text_input("Resource monitor name", "OVERWATCH_RM", key="emg_mon2")
            quota = st.number_input("Credit quota / month", 1, 100000, 30, key="emg_quota")
            if mon:
                stmt = remediation.resource_monitor_quota(mon, int(quota))
        elif action in ("Pause pipe", "Resume pipe"):
            fqn = st.text_input("Pipe (DB.SCHEMA.PIPE)", key="emg_pipe")
            parts = [p for p in fqn.split(".") if p.strip()]
            if len(parts) == 3:
                stmt = remediation.pause_pipe(*parts, paused=(action == "Pause pipe"))
        elif action in ("Suspend task", "Resume task"):
            fqn = st.text_input("Task (DB.SCHEMA.TASK — suspend the ROOT of a graph)",
                                key="emg_task")
            parts = [p for p in fqn.split(".") if p.strip()]
            if len(parts) == 3:
                stmt = remediation.suspend_task_fqn(*parts, resume=(action == "Resume task"))
        elif action in ("Disable user", "Re-enable user"):
            usr = st.text_input("User name", key="emg_user")
            if usr:
                stmt = remediation.disable_user(usr, disabled=(action == "Disable user"))
        elif action == "Cortex allowlist (ACCOUNT)":
            choice = st.radio("Allowlist", ["None (block all AI)", "All (restore)",
                                            "Pinned models"], key="emg_cx")
            if choice.startswith("None"):
                stmt = remediation.cortex_allowlist("None")
            elif choice.startswith("All"):
                stmt = remediation.cortex_allowlist("All")
            else:
                models = st.text_input("Model list (comma-separated)", "llama3.1-8b",
                                       key="emg_cx_models")
                if models:
                    stmt = remediation.cortex_allowlist(models)
        elif action == "Account statement timeout (ACCOUNT)":
            secs = st.number_input("Timeout seconds", 0, 604800, 7200, step=600, key="emg_asecs")
            stmt = remediation.account_statement_timeout(int(secs))
    except ValueError as exc:
        st.error(str(exc))

    if stmt:
        st.code(stmt, language="sql")
        if "ALTER ACCOUNT" in stmt:
            st.warning("ACCOUNT-level: execute as SNOW_ACCOUNTADMINS. Copy the SQL if this "
                       "session's role lacks the privilege.")
        if is_operator:
            confirm = st.text_input("Type EMERGENCY to confirm execution", key="emg_confirm")
            if st.button("Execute + audit", key="emg_exec", disabled=(confirm != "EMERGENCY")):
                ok, msg = execute_statement(stmt, page=_PAGE)
                log_sql = (
                    f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                    "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE) "
                    f"SELECT 'EMERGENCY', {sql_literal(action)}, {sql_literal(stmt[:4000])}, "
                    f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}"
                )
                execute_statement(log_sql, page=_PAGE)
                notify(ok, msg)
        else:
            st.caption("Copy the SQL; executing from the app requires OVERWATCH_OPERATOR.")


def _emergency_extras(is_operator: bool) -> None:
    st.divider()
    st.markdown("**Running queries (kill-switch)**")
    panel_help(
        "Live in-flight statements via INFORMATION_SCHEMA (real time). Cancel needs "
        "ownership of the query or OPERATE on its warehouse; the attempt is audited "
        "either way. Suspending a warehouse does NOT kill these — this does."
    )
    if st.toggle("Show running queries now", key="emg_rq_toggle"):
        rq = run(ops_sql.running_queries(), page=_PAGE, key="emg_running", tier="live",
                 source="INFORMATION_SCHEMA.QUERY_HISTORY (live)", max_rows=0)
        if rq.ok and rq.empty:
            st.success("Nothing running or queued right now.")
        elif guard(rq, ""):
            sel_rq = selectable_table(rq.df, key="emg_rq_sel", height=240)
            if sel_rq is not None and is_operator:
                qrow = rq.df.iloc[int(sel_rq)]
                qid = str(qrow["QUERY_ID"])
                st.code(f"SELECT SYSTEM$CANCEL_QUERY('{qid}');", language="sql")
                confirm_q = st.text_input("Type CANCEL to confirm", key="emg_rq_confirm")
                if st.button("Cancel query + audit", key="emg_rq_exec",
                             disabled=(confirm_q != "CANCEL")):
                    ok, msg = execute_statement(
                        f"SELECT SYSTEM$CANCEL_QUERY({sql_literal(qid)})", page=_PAGE)
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE) "
                        f"SELECT 'CANCEL_QUERY', {sql_literal(qid)}, "
                        f"{sql_literal('SYSTEM$CANCEL_QUERY ' + qid)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}",
                        page=_PAGE)
                    notify(ok, msg)

    st.divider()
    st.markdown("**Budget ↔ resource-monitor sync**")
    panel_help(
        "Budgets are intent (SETTINGS / DEPT_BUDGETS); resource monitors are enforcement. "
        "This suggests a quota per monitor from the budgets of the departments whose "
        "warehouses it guards (quota = budget ÷ credit rate), and applies it in one click."
    )
    mons = run("SHOW RESOURCE MONITORS LIMIT 100", page=_PAGE, key="emg_show_rm",
               tier="metadata", source="SHOW RESOURCE MONITORS", max_rows=0)
    whs2 = run(security_sql.show_warehouses_sql(), page=_PAGE, key="emg_show_wh2",
               tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    bud2 = run(mart_sql.dept_budgets(), page=_PAGE, key="emg_budgets", tier="live",
               source="DEPT_BUDGETS")
    dmap2 = run(chargeback_sql.department_map(), page=_PAGE, key="emg_dmap", tier="recent",
                source="DEPARTMENT_MAP")
    settings2 = load_settings(_PAGE)
    rate2 = safe_float(settings2.get("CREDIT_PRICE_USD"), 3.68)
    if mons.ok and not mons.empty and whs2.ok and not whs2.empty and bud2.ok and not bud2.empty             and dmap2.usable():
        wdf2 = whs2.df.copy()
        wdf2.columns = [str(c).lower() for c in wdf2.columns]
        mdf2 = dmap2.df.copy()
        j = wdf2.merge(mdf2[mdf2["MAP_TYPE"].astype(str) == "WAREHOUSE"],
                       left_on=wdf2["name"].astype(str).str.upper(),
                       right_on=mdf2["NAME"].astype(str).str.upper(), how="inner")
        j = j.merge(bud2.df, on="DEPARTMENT", how="inner")
        if not j.empty and "resource_monitor" in j.columns:
            j = j[~j["resource_monitor"].astype(str).str.lower().isin(("null", "", "none"))]
            sug = (j.groupby("resource_monitor")["MONTHLY_BUDGET_USD"].sum()
                    .reset_index())
            sug["SUGGESTED_QUOTA_CREDITS"] = (sug["MONTHLY_BUDGET_USD"] / rate2).round(0)
            styled_table(sug.rename(columns={"resource_monitor": "MONITOR"}))
            if is_operator and not sug.empty:
                pick_m = st.selectbox("Monitor", sorted(sug["resource_monitor"].astype(str)),
                                      key="emg_sync_mon")
                row_m = sug[sug["resource_monitor"].astype(str) == pick_m].iloc[0]
                quota = int(row_m["SUGGESTED_QUOTA_CREDITS"])
                stmt_m = remediation.resource_monitor_quota(pick_m, quota)
                st.code(stmt_m, language="sql")
                confirm_m = st.text_input("Type SYNC to confirm", key="emg_sync_confirm")
                if st.button("Apply quota + audit", key="emg_sync_exec",
                             disabled=(confirm_m != "SYNC")):
                    ok, msg = execute_statement(stmt_m, page=_PAGE)
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE) "
                        f"SELECT 'MONITOR_SYNC', {sql_literal(pick_m)}, {sql_literal(stmt_m)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}",
                        page=_PAGE)
                    notify(ok, msg)
        else:
            st.info("No monitored warehouses map to budgeted departments yet.")
    else:
        st.caption("Needs: resource monitors, department budgets (Cost > Chargeback), and the "
                   "warehouse map. Suggestions appear once all three exist.")


@st.fragment
def _emergency_fragment(is_operator: bool) -> None:
    """Fragment: lever interactions rerun this section only."""
    _emergency_tab(is_operator)
    _emergency_extras(is_operator)


def _performance_tab() -> None:
    """Prove (or disprove) that the app is fast: its own statement stats."""
    st.caption(
        "Every statement family the app has run on WH_ALFA_OVERWATCH, grouped by "
        "parameterized hash — the slowest rows are the builders worth optimizing next. "
        "Section navigation is lazy and filters no longer cold the cache, so most "
        "interactions should be cache hits."
    )
    telemetry = query_telemetry()
    if not telemetry.empty:
        served = len(telemetry)
        fast = int((telemetry["elapsed_ms"] < 50).sum())
        kpi_row([
            {"label": "Statements this session", "value": f"{served:,}"},
            {"label": "Served in <50ms", "value": f"{fast / served * 100:.0f}%",
             "help": "Approximates the cache-hit rate: sub-50ms answers never left Streamlit's cache."},
            {"label": "Failed", "value": f"{int((~telemetry['ok']).sum())}",
             "delta_color": "inverse" if (~telemetry["ok"]).any() else "off"},
        ])
    st.caption(_SCAN_NOTE)
    res = run(mart_sql.app_statement_stats(7), page=_PAGE, key="app_stmt_stats",
              tier="historical", source="ACCOUNT_USAGE.QUERY_HISTORY (WH_ALFA_OVERWATCH)")
    if guard(res, "No statements on the app warehouse in the last 7 days.",
             setup_hint="Stats appear once the app and its tasks have run against WH_ALFA_OVERWATCH."):
        st.dataframe(res.df, hide_index=True, use_container_width=True,
                     column_config={
                         "MEDIAN_S": st.column_config.NumberColumn("Median s", format="%.2f"),
                         "P95_S": st.column_config.NumberColumn("p95 s", format="%.2f"),
                         "AVG_GB_SCANNED": st.column_config.NumberColumn("Avg GB scanned", format="%.3f"),
                     })
        result_caption(res)
        st.caption("Includes the loader/scan tasks — they share the warehouse by design.")

    st.markdown("**Page adoption (30d)**")
    usage = run(mart_sql.app_usage_summary(30), page=_PAGE, key="app_usage", tier="recent",
                source="APP_USAGE")
    if usage.ok and usage.empty:
        st.info("No visits logged yet (logging starts after V016 + a roles.sql re-run).")
    elif guard(usage, "", setup_hint="APP_USAGE comes with migration V016; re-run roles.sql for the grant."):
        styled_table(usage.df)
        st.caption("Curation calls (merge/kill sections) should follow this table, not opinions.")

    st.markdown("**Fleet slow/failed fetches (all viewers, 7d)**")
    fq = run(mart_sql.fleet_query_stats(7), page=_PAGE, key="fleet_qstats", tier="recent",
             source="APP_QUERY_TELEMETRY (V021)")
    if not fq.ok:
        st.info("Needs migration V021 + a roles.sql re-run (APP_QUERY_TELEMETRY INSERT grant).")
    elif fq.empty:
        st.success("No slow (≥2s) or failed fetches persisted in 7 days — every viewer is "
                   "riding the cache.")
    else:
        styled_table(fq.df, height=280)
        st.caption(
            "Only fetches ≥2s or failed are persisted (sampled, fire-and-forget, 60/session cap) "
            "— this is the regression surface across every user, not a complete census. "
            "The session table above shows only YOUR session."
        )
    _perf_rider_panels(fq.df if fq.ok and not fq.empty else None)


def _perf_rider_panels(fq_df=None) -> None:
    """V027 telemetry-rider readouts (Codex r6 #8, #12, #19)."""
    st.markdown("**Fleet telemetry by page (7d)**")
    tbp = run(mart_sql.telemetry_by_page(7), page=_PAGE, key="tel_by_page", tier="recent",
              source="APP_QUERY_TELEMETRY (persisted = slow/failed + 2% sample)")
    if tbp.usable():
        styled_table(tbp.df, height=260, column_config={
            "CACHE_HIT_PCT": st.column_config.NumberColumn("Cache hit %", format="%.1f%%"),
        })
        st.caption("Cache-hit % covers PERSISTED fetches only (slow/failed always + the 2% "
                   "healthy sample) and rows new enough to carry CACHE_HIT — a floor, not a census.")
        # Ranked next-tuning-targets (Codex r7 #3, minus the speculative
        # "likely fix" text): pain = p95 x slow-count, from the same frame.
        _tt = tbp.df.copy()
        try:
            _tt["PAIN"] = (_tt["P95_S"].astype(float) * _tt["SLOW_2S"].astype(float)).round(1)
            _tt = _tt.sort_values("PAIN", ascending=False).head(5)
            st.markdown("**Next tuning targets** — pain = p95 x slow fetches; "
                        "the telemetry picks, not opinions.")
            _sel = selectable_table(
                _tt[["PAGE", "P95_S", "SLOW_2S", "FAILED", "CACHE_HIT_PCT", "PAIN"]],
                key="adm_tt_sel", height=160)
            # Codex r8 #1: click a target, see the slow keys behind the pain
            if _sel is not None and fq_df is not None:
                _pg = str(_sel["PAGE"])
                _det = fq_df[fq_df["PAGE"].astype(str) == _pg]
                if not _det.empty:
                    st.markdown(f"**{_pg} — the slow keys behind the pain (7d persisted)**")
                    styled_table(_det, height=200)
        except (KeyError, TypeError, ValueError):
            pass
    else:
        st.caption("Per-page telemetry appears after V027 and a day of traffic.")

    st.markdown("**Usage events (30d) & remediation acceptance (90d)**")
    ue = run(mart_sql.usage_event_summary(30), page=_PAGE, key="usage_events", tier="recent",
             source="APP_USAGE.EVENT_KIND (V027 rider)")
    if ue.usable():
        styled_table(ue.df, height=190)
        st.caption("page_visit dominates by design; the interaction kinds (acks, resolves, "
                   "exports, remediations) are the operator-effectiveness signal.")
    acc = run(mart_sql.acceptance_funnel(90), page=_PAGE, key="acceptance_funnel", tier="recent",
              source="REMEDIATION_LOG + SAVINGS_LEDGER")
    if acc.usable():
        a = acc.df.iloc[0]
        def _n(k):
            try:
                return f"{float(a.get(k) or 0):,.0f}"
            except (TypeError, ValueError):
                return "0"
        kpi_row([
            {"label": "Fixes executed / copied / failed",
             "value": f"{_n('FIXES_EXECUTED')} / {_n('FIXES_COPIED')} / {_n('FIXES_FAILED')}"},
            {"label": "Savings est -> verified / rejected",
             "value": f"{_n('SAVINGS_ESTIMATED')} -> {_n('SAVINGS_VERIFIED')} / {_n('SAVINGS_REJECTED')}"},
            {"label": "Verified savings (90d)",
             "value": f"${float(a.get('VERIFIED_USD') or 0):,.0f}"},
        ])
        st.caption("Generated -> executed -> verified, from audit rows. No impression "
                   "tracking — Streamlit cannot measure viewed truthfully (r5 #4 decision).")


def _canary_tab() -> None:
    st.caption(
        "Runs every registered SQL builder against the live account (1-row caps) to catch "
        "ACCOUNT_USAGE column drift or missing OVERWATCH objects before a user does. "
        "Failures are logged to APP_ERROR_LOG."
    )
    from app.data.canary import CANARIES

    st.markdown(f"**{len(CANARIES)} registered statements**")
    if st.button("Run canary now", key="adm_canary_run"):
        results = []
        progress = st.progress(0.0, text="Running canary...")
        for idx, (name, builder) in enumerate(CANARIES):
            res = run(builder(), page=_PAGE, key=f"canary_{name}", tier="live",
                      source=name, max_rows=1)
            results.append({"CHECK": name, "STATUS": "PASS" if res.ok else "FAIL",
                            "ROWS": len(res.df), "ERROR": res.error[:160]})
            progress.progress((idx + 1) / len(CANARIES), text=f"{name}")
        progress.empty()
        import pandas as _pd

        frame = _pd.DataFrame(results)
        failed = frame[frame["STATUS"] == "FAIL"]
        if failed.empty:
            st.success(f"All {len(frame)} canary statements passed.")
        else:
            st.error(f"{len(failed)} of {len(frame)} canary statements failed — see errors below.")
        st.session_state["_adm_canary_results"] = frame
    stored = st.session_state.get("_adm_canary_results")
    if stored is not None:
        from app.ui.components import styled_table as _styled

        view = stored.copy()
        view["STATUS"] = view["STATUS"].map({"PASS": "SUCCESS", "FAIL": "FAILED"})
        view = view.rename(columns={"STATUS": "EXECUTION_STATUS"})
        _styled(view, height=420)

    st.divider()
    st.markdown("**Mart reconciliation — do the numbers MATCH the source?**")
    st.caption(
        "Freshness proves the loaders ran; this compares mart totals against live "
        "ACCOUNT_USAGE over the same complete window. ±2% is normal late-arrival noise; "
        "beyond ±5%, re-run the backfill for that window (snowflake/backfill_365.sql, scoped)."
    )
    recon = run(mart_sql.mart_vs_live_recon(), page=_PAGE, key="mart_recon", tier="historical",
                source="FACT_* vs METERING_DAILY_HISTORY / QUERY_HISTORY")
    if guard(recon, "Reconciliation needs the facts (V002) installed.",
             setup_hint="Runs the mart and the live aggregate side by side; deploy marts first."):
        rdf = recon.df.copy()
        rdf["STATE"] = rdf["DRIFT_PCT"].map(
            lambda d: "OK" if abs(safe_float(d)) <= 2 else ("WARN" if abs(safe_float(d)) <= 5 else "BAD"))
        styled_table(rdf, column_config={
            "DRIFT_PCT": st.column_config.NumberColumn("Drift %", format="%.2f%%")})
        worst = rdf["DRIFT_PCT"].map(lambda d: abs(safe_float(d))).max()
        if worst > 5:
            st.error("Mart drift beyond ±5%: chargeback and exec numbers are off until the "
                     "backfill re-runs. This is exactly what this panel exists to catch.")
        elif worst > 2:
            st.warning("Mart drift in the 2-5% band — usually late-arriving metering rows; "
                       "re-check tomorrow before re-running backfills.")
        result_caption(recon)

    st.divider()
    st.markdown("**Fire-drill scoreboard — does the page reach a human?**")
    from app.logic.drill import drill_report
    drills = run(mart_sql.drill_history(14), page=_PAGE, key="drill_hist", tier="recent",
                 source="ALERT_EVENTS (OPS_ALERT_DRILL)")
    if not drills.ok:
        st.info("Drill history unavailable: " + drills.error)
    else:
        report = drill_report(drills.df if not drills.empty else None)
        if not report["ran"]:
            st.info("No drills yet — enable the monthly fire drill with the opt-in "
                    "snowflake/alert_drill.sql (one synthetic CRITICAL on the 1st; "
                    "the notify chain must deliver it and on-call must ACK it).")
        else:
            last = report["last"]
            kpi_row([
                {"label": "Drill streak", "value": f"{report['streak_months']} month(s)",
                 "severity": "ok" if report["streak_months"] >= 1 else "bad",
                 "help": "Consecutive months where the drill was DELIVERED and ACKED."},
                {"label": "Last drill delivered",
                 "value": "yes" if last["delivered"] else "NO",
                 "severity": "ok" if last["delivered"] else "bad"},
                {"label": "Time to ack",
                 "value": f"{last['mtta_min']:.0f} min" if last["mtta_min"] is not None else "not acked",
                 "severity": "ok" if last["acked"] else "warn"},
            ])
            styled_table(drills.df, height=200)
            st.caption("Resolve drills as EXPECTED — they're excluded from rule precision.")

    st.divider()
    st.markdown("**Restated days — did a reported number move after close?**")
    rest = run(mart_sql.metering_restatements(60), page=_PAGE, key="restatements",
               tier="recent", source="FACT_METERING_DAILY LOAD_TS lag")
    if rest.ok and rest.empty:
        st.success("No metering day was restated ≥48h after close in the last 60 days — "
                   "numbers reported from this app have stayed put.")
    elif guard(rest, ""):
        styled_table(rest.df, height=220)
        st.caption(
            "These days' metering changed ≥48h after the day ended (late-arriving rows or "
            "re-runs). If finance got a figure before the restatement, this is the receipt "
            "explaining the move. v1 flags restated days; first-reported snapshots would "
            "need a snapshot fact."
        )


@safe_page(_PAGE)
def render() -> None:
    page_header("Admin", "Settings, migrations, self-cost, canary, and app observability.", icon_name="admin")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    _context_section()
    section = lazy_sections(
        ["Settings", "Emergency", "Migrations & freshness", "App self-cost", "Org spend",
         "Performance", "Canary", "Errors & telemetry"], key="adm_section")
    if section == "Settings":
        _settings_tab(is_operator)
    elif section == "Emergency":
        _emergency_fragment(is_operator)
    elif section == "Migrations & freshness":
        _migrations_tab()
    elif section == "App self-cost":
        _self_cost_tab()
    elif section == "Org spend":
        _org_spend_tab()
    elif section == "Performance":
        _performance_tab()
    elif section == "Canary":
        _canary_tab()
    else:
        _observability_tab()
