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
from app.core.identity import identity_sql
from app.core.query import bump_refresh_salt, execute_statement, query_telemetry, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.data import cost_sql, mart_sql, ops_sql, security_sql
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
    run_mart_first,
    selectable_table,
    snowsight_profile_column,
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
    36: "pattern-cost mart (measured $ per repeated statement)",
    37: "pattern mart v2: DATABASE_NAME grain + HLL users (compare env prep)",
    38: "ledger autobook (detected cost-lever changes settle themselves)",
    39: "pseudo-warehouse filter (CLOUD_SERVICES_ONLY out of the warehouse fact)",
    40: "freshness state table + 10-min snapshot (lookup, not 19 aggregates)",
    41: "loader efficiency: staged QH extract, xdim alloc fact, exec board v2, "
        "watermarks + nightly reconcile, loader-owned freshness, ops-diag + "
        "platform-score marts, posture riders",
    42: "codex r22: FACT_QUERY_DAILY, atomic extract + gated watermark, "
        "ops-diag backfill, purge coverage, AI fact usage stamps",
    43: "task retirement loader-side (fills/board/score/purge/reconcile/"
        "freshness + tables dropped, PIPE_TASK_FAILURES disabled) + r25 "
        "alert teeth (new-admin-network, egress spike)",
    44: "UNKNOWN classification (#18): evidence-based company both sides, "
        "COMPANY_SCOPE database mapping lever, board UNKNOWN scope",
    45: "owner correction: task monitoring restored (tables/procs/rule/"
        "refill; teeth + UNKNOWN scope kept); OVERWATCH_RM dropped",
    46: "storage truth: account tiers (stage/hybrid/archive) + per-DB "
        "monthly-average billing basis (COST_DB recon R3 / audit F1)",
    47: "pattern-cost mart includes Query Acceleration (Codex audit item 4)",
    48: "FACT_OBJECT_COST_DAILY object-cost ledger (measured split + serverless arms)",
    49: "write-target attribution (OBJECTS_MODIFIED joins the split; residual "
        "= no-read-no-write compute)",
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
        # r27 H2: keys the app no longer reads (retired features leave rows
        # behind — SCORE_PTS_TASK_FAIL_PER_PCT after V043, for instance).
        try:
            _known = {k for k in DEFAULT_SETTINGS if not k.startswith("_")}
            _orphans = sorted(set(res.df["KEY"].astype(str)) - _known)
            if _orphans:
                st.warning("Settings rows the app no longer reads (safe to delete): "
                           + ", ".join(_orphans))
        except (KeyError, TypeError):
            pass

    st.markdown("**Change a setting**")
    editable = [k for k in DEFAULT_SETTINGS if not k.startswith("_")]
    key = st.selectbox("Setting", editable, key="adm_setting_key")
    new_value = st.text_input("New value", key="adm_setting_value",
                              help="Numeric settings take numbers; dates are YYYY-MM-DD; blank clears.")
    update_sql = (
        f"UPDATE {core_object('SETTINGS')} SET VALUE = {sql_literal(new_value)}, "
        f"UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {identity_sql()} "
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
        st.caption("Executing requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS; anyone can copy the SQL for review.")


def _migrations_tab() -> None:
    res = run(mart_sql.schema_version(), page=_PAGE, key="schema_version", tier="metadata",  # r24 #8: changes only at migrations
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

    fh = run(mart_sql.flyway_history(), page=_PAGE, key="flyway_history", tier="recent",  # r24 #8: external ledger probe
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
    fresh = run_mart_first(
        mart_sql.source_freshness_state(), mart_sql.source_freshness(),
        page=_PAGE, key="adm_freshness",
        mart_source="SOURCE_FRESHNESS_STATE (10-min snapshot)",
        live_source="MART_SOURCE_FRESHNESS (19-aggregate view, pre-V040 fallback)",
        mart_tier="recent", live_tier="recent")   # state moves every 10 min (r14 #13)
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
        "and every app query carries an OVERWATCH query tag (no resource monitor since v4.45 — OVERWATCH_RM was suspending the warehouse mid-use)."
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


def _access_self_check() -> None:
    """r27 H3: probe every privileged source the app reads and hand back the
    exact missing grant — the next access error becomes a checklist row,
    not a debugging session."""
    st.markdown("**Access self-check**")
    st.caption("Probes each privileged source with a 1-row read. Run after a rebuild, "
               "a role change, or when any panel reports an access error.")
    if not st.button("Run access self-check", key="adm_access_check"):
        return
    probes = [
        ("ACCOUNT_USAGE core", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 1",
         "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_ACCOUNTADMINS; (roles.sql)"),
        ("LOGIN_HISTORY", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY LIMIT 1",
         "Covered by IMPORTED PRIVILEGES — if core is OK and this is not, contact Snowflake."),
        ("CREDENTIALS", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS LIMIT 1",
         "Newer accounts expose this view by default; older ones need Snowflake to enable it."),
        ("DATA_TRANSFER_HISTORY", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY LIMIT 1",
         "Covered by IMPORTED PRIVILEGES (Security -> Egress reads this)."),
        ("Trust Center findings", "SELECT 1 FROM SNOWFLAKE.TRUST_CENTER.FINDINGS LIMIT 1",
         "GRANT APPLICATION ROLE SNOWFLAKE.TRUST_CENTER_VIEWER TO ROLE SNOW_ACCOUNTADMINS;"),
        ("App schema", "SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SETTINGS LIMIT 1",
         "Run snowflake/roles.sql as SNOW_ACCOUNTADMINS."),
        ("Warehouse metadata", "SHOW WAREHOUSES", "USAGE/MONITOR on warehouses (roles.sql)."),
    ]
    rows = []
    for name, sql, fix in probes:
        r = run(sql, page=_PAGE, key=f"acc_{name}", tier="metadata", source=name,
                max_rows=0 if sql.startswith("SHOW") else 1)
        rows.append({"SOURCE": name, "STATUS": "OK" if r.ok else "BLOCKED",
                     "FIX": "" if r.ok else fix,
                     "ERROR": "" if r.ok else str(r.error)[:140]})
    _df = pd.DataFrame(rows)
    blocked = int((_df["STATUS"] == "BLOCKED").sum())
    if blocked == 0:
        st.success(f"All {len(_df)} sources reachable.")
    else:
        st.error(f"{blocked} source(s) blocked — fixes below.")
    styled_table(_df)
    st.divider()


def _observability_tab() -> None:
    _access_self_check()
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
        # r27 H4: repeated identical errors read as ONE family, not N rows.
        _e = sink.df.copy()
        try:
            _e["FAMILY"] = (_e["ERROR_TYPE"].astype(str) + " · "
                            + _e["ERROR_MESSAGE"].astype(str).str.slice(0, 60))
            grouped = (_e.groupby(["PAGE", "FAMILY"], as_index=False)
                       .agg(COUNT=("FAMILY", "size"), FIRST_SEEN=("LOGGED_AT", "min"),
                            LAST_SEEN=("LOGGED_AT", "max")))
            styled_table(grouped.sort_values("LAST_SEEN", ascending=False), height=240)
            with st.expander(f"Raw rows ({len(_e)})"):
                styled_table(_e)
        except (KeyError, TypeError):
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
    pivot = (df.groupby(["ACCOUNT_NAME", "SERVICE_TYPE"], as_index=False)["USAGE_IN_CURRENCY"].sum()
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
                "ORG_AI_USD": round(safe_float(orow.get("AI_USD")), 2),
                "ORG_STORAGE_USD": round(safe_float(orow.get("STORAGE_USD")), 2),
                "ORG_TRANSFER_USD": round(safe_float(orow.get("TRANSFER_USD")), 2),
                "ORG_ADJUSTMENT_USD": round(safe_float(orow.get("ADJUSTMENT_USD")), 2),
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
        "Cluster range", "Scaling policy", "Pause pipe", "Resume pipe", "Suspend task",
        "Resume task", "Disable user", "Re-enable user",
        "Cortex allowlist (ACCOUNT)", "Account statement timeout (ACCOUNT)",
    ], key="emg_action")

    stmt = ""
    try:
        if action in ("Suspend warehouse", "Resume warehouse", "Warehouse statement timeout",
                      "Cluster range", "Scaling policy"):
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
            st.caption("Copy the SQL; executing from the app requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")


def _emergency_extras(is_operator: bool) -> None:
    st.divider()
    st.markdown("**Running queries (kill-switch)**")
    panel_help(
        "Live in-flight statements via INFORMATION_SCHEMA (real time). Cancel needs "
        "ownership of the query or OPERATE on its warehouse; the attempt is audited "
        "either way. Suspending a warehouse does NOT kill these — this does."
    )
    if st.toggle("Show running queries now", key="emg_rq_toggle"):
        _rq_whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="emg_show_wh",
                      tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        _rq_names: list = []
        if _rq_whs.ok and not _rq_whs.empty:
            _rqdf = _rq_whs.df.copy()
            _rqdf.columns = [str(c).lower() for c in _rqdf.columns]
            if "name" in _rqdf.columns:
                _rq_names = sorted(_rqdf["name"].astype(str))
        _rq_pick = (st.selectbox("Warehouse to inspect", _rq_names, key="emg_rq_wh")
                    if _rq_names else st.text_input("Warehouse to inspect", key="emg_rq_wh_txt"))
        if not _rq_pick:
            st.caption("Pick a warehouse — the in-flight view is per warehouse "
                       "(current-user scoping is unavailable inside SiS).")
            return
        rq = run(ops_sql.running_queries(_rq_pick), page=_PAGE,
                 key=f"emg_running_{_rq_pick}", tier="live",
                 source=f"INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE ({_rq_pick}, live)",
                 max_rows=0)
        if rq.ok and rq.empty:
            st.success("Nothing running or queued right now.")
        elif guard(rq, ""):
            _rqdf, _rq_cfg = snowsight_profile_column(rq.df, _PAGE)
            sel_rq = selectable_table(_rqdf, key="emg_rq_sel", height=240,
                                      column_config=_rq_cfg or None)
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


@st.fragment
def _emergency_fragment(is_operator: bool) -> None:
    """Fragment: lever interactions rerun this section only."""
    _emergency_tab(is_operator)
    _emergency_extras(is_operator)


def _performance_tab() -> None:
    """Prove (or disprove) that the app is fast: its own statement stats."""
    st.caption(
        "Every statement family the app has run on WH_ALFA_OVERWATCH, grouped by "
        "parameterized hash — the slowest rows are the builders worth optimizing next."
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
        st.caption("Merging or retiring sections should follow this table, not opinions.")

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
            "Only fetches ≥2s or failed are persisted, plus a ~2% healthy sample "
            "(fire-and-forget, 60/session cap) — an EXCEPTION-WEIGHTED sample, so "
            "p50/p95 here read HIGHER than true fleet latency (r22 #20; weighted "
            "stats are queued). This is the regression surface across every user, "
            "not a complete census. The session table above shows only YOUR session."
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
                _tt[["PAGE", "P95_S", "SLOW_2S", "FAILED", "PAIN"]],  # r24: CACHE_HIT_PCT off — 0.0 by construction until weighted telemetry (review #3/#4)
                key="adm_tt_sel", height=160)
            # Codex r8 #1: click a target, see the slow keys behind the pain
            if _sel is not None:
                # selectable_table returns a POSITIONAL index into the frame
                # it displayed. v4.23.0 subscripted the int like a row, hit
                # TypeError, and the except below silently ate every click
                # (Joe 2026-07-11: "the screen flashes and does nothing").
                _pg = str(_tt.iloc[int(_sel)]["PAGE"])
                _det = None if fq_df is None else fq_df[fq_df["PAGE"].astype(str) == _pg]
                if _det is None or _det.empty:
                    st.caption(f"{_pg}: nothing slow or failed persisted for this page "
                               "in 7d — its pain is spread across sub-2s fetches.")
                else:
                    st.markdown(f"**{_pg} — the slow keys behind the pain (7d persisted)**")
                    styled_table(_det, height=200)
        except (KeyError, TypeError, ValueError) as exc:
            # never silent: a broken drill must say so, not flash and shrug
            st.caption(f"Tuning-target drill unavailable — {type(exc).__name__}: {str(exc)[:80]}")
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
                   "tracking — Streamlit cannot measure 'viewed' truthfully.")


def _canary_tab() -> None:
    st.caption(
        "Runs every registered SQL builder against the live account (1-row caps) to catch "
        "ACCOUNT_USAGE column drift or missing OVERWATCH objects before a user does. "
        "Failures are logged to APP_ERROR_LOG."
    )
    from app.data.canary import CANARIES, EXPECTED_GAPS

    st.markdown(f"**{len(CANARIES)} registered statements**")
    if st.button("Run canary now", key="adm_canary_run"):
        results = []
        progress = st.progress(0.0, text="Running canary...")
        for idx, (name, builder) in enumerate(CANARIES):
            res = run(builder(), page=_PAGE, key=f"canary_{name}", tier="live",
                      source=name, max_rows=1, probe=True)
            # r10 #4: classified from the RAW exception in run(). r11 #7: GAP
            # must be DECLARED per entry — an absent core object is drift and
            # FAILS; only account-feature absences read as a calm GAP.
            _gap = (res.error_kind in ("absent", "unknown_function")
                    and name in EXPECTED_GAPS)
            results.append({"CHECK": name,
                            "STATUS": "PASS" if res.ok else ("GAP" if _gap else "FAIL"),
                            "ROWS": len(res.df), "ERROR": res.error[:160]})
            progress.progress((idx + 1) / len(CANARIES), text=f"{name}")
        progress.empty()
        import pandas as _pd

        frame = _pd.DataFrame(results)
        failed = frame[frame["STATUS"] == "FAIL"]
        gaps = frame[frame["STATUS"] == "GAP"]
        if not gaps.empty:
            st.caption(f"{len(gaps)} GAP: declared account-feature absences (Cortex "
                       "subscription/region) — absence, not drift. Anything absent "
                       "WITHOUT a declaration fails instead.")
        if failed.empty:
            st.success(f"All {len(frame) - len(gaps)} applicable canary statements passed.")
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
    # r21 #7: merely opening this tab paid a 28d metering + 7d history scan.
    if not st.toggle("Run reconciliation", key="adm_recon_on",
                     help="Compares 28d metering and 7d query totals, mart vs live. "
                          "Cached for an hour once run."):
        return
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
            "explaining the move."
        )


def _metric_registry_tab() -> None:
    """Phase 1 (architectural): the single semantic contract for every cost
    number — method, grain, source, timezone, latency, formula version."""
    from app.logic import metric_registry as mr
    st.markdown("**Cost metric registry — what every number means**")
    st.caption(
        "Read a figure by its METHOD: BILLED ties to the invoice, METERED is "
        "exact usage (idle in, CS unadjusted), MEASURED is attributed compute "
        "(idle out), ALLOCATED is a share-based estimate, ESTIMATED is "
        "bytes/credits x a configured rate. Adding a cost metric without "
        "registering it here fails the drift-guard test."
    )
    _order = {m: i for i, m in enumerate(mr.METHODS)}
    _rows = sorted(mr.as_rows(), key=lambda r: _order.get(r["Method"], 99))
    styled_table(pd.DataFrame(_rows), height=460)
    st.caption(f"{len(_rows)} registered metrics · methods: {', '.join(mr.METHODS)}.")


@safe_page(_PAGE)
def render() -> None:
    page_header("Admin", "Settings, migrations, self-cost, canary, and app observability.", icon_name="admin")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    _context_section()
    section = lazy_sections(
        ["Settings", "Emergency", "Migrations & freshness", "Metrics", "App self-cost", "Org spend",
         "Performance", "Canary", "Errors & telemetry"], key="adm_section")
    if section == "Settings":
        _settings_tab(is_operator)
    elif section == "Emergency":
        _emergency_fragment(is_operator)
    elif section == "Migrations & freshness":
        _migrations_tab()
    elif section == "Metrics":
        _metric_registry_tab()
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
