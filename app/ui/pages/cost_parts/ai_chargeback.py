"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import streamlit as st

from app.config import core_object
from app.core.query import execute_statement, run
from app.core.sqlsafe import sql_literal, sql_number
from app.data import chargeback_sql, cortex_sql, cost_sql, mart_sql
from app.logic.cortex import classify_exceptions, enrich_user_rollup, rollup_summary
from app.logic.formulas import account_today, credits_to_usd, format_usd, safe_float
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    notify,
    panel_help,
    result_caption,
    styled_table,
)

_PAGE = "Cost & Contract"

_SERVICE_CATEGORY = {
    "WAREHOUSE_METERING": "Warehouse",
    "WAREHOUSE_METERING_READER": "Warehouse (reader)",
    "SNOWPIPE": "Serverless", "SNOWPIPE_STREAMING": "Serverless",
    "SERVERLESS_TASK": "Serverless", "SERVERLESS_ALERTS": "Serverless",
    "AUTOMATIC_CLUSTERING": "Serverless", "MATERIALIZED_VIEW": "Serverless",
    "SEARCH_OPTIMIZATION": "Serverless", "QUERY_ACCELERATION": "Serverless",
    "SNOWPARK_CONTAINER_SERVICES": "Serverless", "COPY_FILES": "Serverless",
    "REPLICATION": "Replication", "STORAGE": "Storage",
}


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

def _cortex_storage_tab(company: str, days: int, ai_rate: float, settings: dict) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Cortex / AI spend**")
        res = run(cost_sql.cortex_daily_spend(days), page=_PAGE, key=f"cortex_{days}",
                  tier="historical", source="ACCOUNT_USAGE.METERING_DAILY_HISTORY (AI services)")
        if guard(res, "No AI/Cortex service credits in this window."):
            df = res.df.copy()
            df["USD"] = df["CREDITS_BILLED"].map(safe_float) * ai_rate
            kpi_row([{"label": f"Cortex spend, {days}d", "value": format_usd(float(df["USD"].sum())),
                      "help": f"Billed AI-service credits x ${ai_rate:.2f}."}])
            charts.daily_stacked_usd(df, "DAY", "SERVICE_TYPE", "USD")
            result_caption(res)
    with right:
        st.markdown("**Storage by database**")
        res = run(cost_sql.storage_by_database(days, company, st.session_state.get("flt_database", "")), page=_PAGE,
                  key=f"storage_{company}_{days}", tier="historical",
                  source="ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY")
        if not res.ok or res.empty:
            res = run(cost_sql.storage_by_database_live(days, company,
                                                        st.session_state.get("flt_database", "")),
                      page=_PAGE, key=f"storage_live_{company}_{days}", tier="historical",
                      source="DATABASE_STORAGE_USAGE_HISTORY (live fallback)")
        if guard(res, "No storage rows for this scope."):
            df = res.df.copy()
            latest_day = df["DAY"].max()
            latest = df[df["DAY"] == latest_day].copy()
            latest["TB"] = latest["DB_BYTES"].map(safe_float) / (1024**4)
            rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
            latest["USD_MONTH"] = latest["TB"] * rate_tb
            total_tb = float(latest["TB"].sum())
            kpi_row([{"label": "Current storage", "value": f"{total_tb:,.2f} TB",
                      "delta": f"~{format_usd(total_tb * rate_tb)}/mo",
                      "help": f"${rate_tb:.2f}/TB/mo from SETTINGS. Display estimate."}])
            charts.bar_usd(latest.sort_values("USD_MONTH", ascending=False),
                           "DATABASE_NAME", "USD_MONTH", title="$/month (est.)")
            result_caption(res)

def _ai_users_tab(company: str, days: int, ai_rate: float, settings: dict, is_operator: bool) -> None:
    """Cortex Code user attribution — ported from the original AI & Cortex
    Monitor. Token credits are exact per user; projections and severities are
    computed in tested logic, and budget severities only exist when an AI
    budget is actually configured."""
    ai_budget = safe_float(settings.get("AI_MONTHLY_BUDGET_USD"))
    rollup_res = run(cortex_sql.cortex_code_user_rollup(days, company), page=_PAGE,
                     key=f"cortex_users_{company}_{days}", tier="historical",
                     source="ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY")
    if not guard(rollup_res,
                 "No Cortex Code usage (Snowsight or CLI) recorded in this window for this scope.",
                 setup_hint="If these views are not enabled in this account, this tab stays honest and empty."):
        return

    enriched = enrich_user_rollup(rollup_res.df, ai_rate, days)
    summary = rollup_summary(enriched, days)
    budget_kpi_item = (
        {"label": "AI monthly budget", "value": format_usd(ai_budget),
         "help": "AI_MONTHLY_BUDGET_USD from SETTINGS; drives the severity flags below."}
        if ai_budget > 0 else
        {"label": "AI monthly budget", "value": "Not configured",
         "help": "Set AI_MONTHLY_BUDGET_USD in Admin to enable budget-breach severities. Nothing is assumed."}
    )
    kpi_row([
        {"label": f"Active AI users ({days}d)", "value": f"{summary['active_users']:,}"},
        {"label": "Requests", "value": f"{summary['total_requests']:,}"},
        {"label": "Cortex Code spend", "value": format_usd(summary["spend_usd"]),
         "help": f"Exact token credits x ${ai_rate:.2f}/credit."},
        {"label": "Projected 30d", "value": format_usd(summary["projected_30d_usd"]),
         "help": "Window run-rate extended to 30 days."},
        budget_kpi_item,
    ])

    left, right = st.columns([1.1, 1.0])
    with left:
        st.markdown("**Cost by user (exact token credits)**")
        by_user = (enriched.groupby("USER_NAME", as_index=False)["SPEND_USD"].sum()
                   .sort_values("SPEND_USD", ascending=False))
        charts.bar_usd(by_user, "USER_NAME", "SPEND_USD", title="Spend (USD)", top_n=12)
    with right:
        st.markdown("**Daily usage by source**")
        daily_res = run(cortex_sql.cortex_code_daily(days, company), page=_PAGE,
                        key=f"cortex_daily_{company}_{days}", tier="historical",
                        source="ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY")
        if guard(daily_res, "No daily Cortex Code usage rows."):
            daily = daily_res.df.copy()
            daily["USD"] = daily["TOTAL_CREDITS"].map(safe_float) * ai_rate
            charts.daily_stacked_usd(daily, "DAY", "SOURCE", "USD")

    st.markdown("**User attribution detail**")
    st.dataframe(
        enriched[[c for c in ["USER_NAME", "EMAIL", "SOURCE", "ACTIVE_DAYS", "TOTAL_REQUESTS",
                   "TOTAL_CREDITS", "TOTAL_TOKENS", "CREDITS_PER_REQUEST", "SPEND_USD",
                   "PROJECTED_30D_USD", "FIRST_USAGE", "LAST_USAGE"] if c in enriched.columns]],
        hide_index=True, use_container_width=True,
        column_config={
            "SPEND_USD": st.column_config.NumberColumn("Spend $", format="$%.2f"),
            "PROJECTED_30D_USD": st.column_config.NumberColumn("Proj. 30d $", format="$%.2f"),
            "CREDITS_PER_REQUEST": st.column_config.NumberColumn("Cr/request", format="%.4f"),
        },
    )
    result_caption(rollup_res, note="Cortex Code token metering is exact per user; no allocation involved.")

    exceptions = classify_exceptions(enriched, ai_budget, ai_rate)
    st.markdown("**Exceptions**")
    if exceptions.empty:
        if ai_budget > 0:
            st.success("No users over 25% of the AI budget and no cost-per-request spikes.")
        else:
            st.info("No cost-per-request spikes. Configure AI_MONTHLY_BUDGET_USD to also flag budget pressure.")
    else:
        styled_table(
            exceptions[["SEVERITY", "SIGNAL", "USER_NAME", "SOURCE", "TOTAL_REQUESTS",
                         "CREDITS_PER_REQUEST", "PROJECTED_30D_USD"]],
        )
        with st.expander("Queue top exceptions to the Action Queue"):
            statements = []
            for _, r in exceptions.head(10).iterrows():
                title = f"Cortex {r['SIGNAL']}: {r['USER_NAME']} ({r['SOURCE']})"
                detail = (f"{int(r['TOTAL_REQUESTS'])} requests, projected 30d "
                          f"{format_usd(r['PROJECTED_30D_USD'])}, cr/request {r['CREDITS_PER_REQUEST']:.4f}.")
                statements.append(
                    f"INSERT INTO {core_object('ACTION_QUEUE')} (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, SOURCE, ESTIMATED_USD)\n"
                    f"VALUES ({sql_literal(company)}, {sql_literal(str(r['SEVERITY']).upper())}, {sql_literal(title)}, "
                    f"{sql_literal(detail)}, 'DBA / AI Governance', 'Cost & Contract > AI Users', "
                    f"{sql_number(r['PROJECTED_30D_USD'])});"
                )
            script = "\n".join(statements)
            st.code(script, language="sql")
            if is_operator and st.button("Execute inserts", key="cortex_queue_exec"):
                ok_all, count = True, 0
                for stmt in statements:
                    ok, _msg = execute_statement(stmt.replace("\n", " "), page=_PAGE)
                    ok_all, count = ok_all and ok, count + int(ok)
                (st.success if ok_all else st.error)(f"{count}/{len(statements)} action(s) queued.")
            elif not is_operator:
                st.caption("Copy and run as OVERWATCH_OPERATOR - in-app execution needs the operator role.")

    with st.expander("AI Functions usage (optional view)"):
        fn_res = run(cortex_sql.cortex_ai_functions_daily(days), page=_PAGE,
                     key=f"cortex_fn_{days}", tier="historical",
                     source="ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY")
        if fn_res.ok and not fn_res.empty:
            fn = fn_res.df.copy()
            fn["USD"] = fn["TOTAL_CREDITS"].map(safe_float) * ai_rate
            charts.daily_stacked_usd(fn, "DAY", "SOURCE", "USD")
            result_caption(fn_res)
        elif fn_res.ok:
            st.caption("No AI Functions usage in this window.")
        else:
            st.caption(f"View not available in this account/role: {fn_res.error}")

def _statement_export(company: str, rate: float) -> None:
    """Fragment: month picks and the zip build rerun this block only."""
    st.markdown("**Monthly statement export**")
    from datetime import timedelta

    today = account_today()
    this_month = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month = st.selectbox("Statement month", [prev, this_month], key="cb_month",
                         help="Prior month is the finance-ready one; current month is partial.")
    if st.button("Build department statements", key="cb_build"):
        import io
        import zipfile

        month_res = run(chargeback_sql.department_month_credits(month, company), page=_PAGE,
                        key=f"cb_month_{company}_{month}", tier="historical",
                        source="WAREHOUSE_METERING_HISTORY (calendar month)")
        if not month_res.usable():
            st.error(month_res.error or "No credits recorded for that month/scope.")
        else:
            frame = month_res.df.copy()
            frame["USD"] = frame["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
                summary = frame.groupby(["DEPARTMENT", "DEPT_OWNER"], as_index=False)["USD"].sum()
                bundle.writestr("00_summary.csv", summary.to_csv(index=False))
                for dept_name, block in frame.groupby("DEPARTMENT"):
                    safe_name = "".join(ch if ch.isalnum() else "_" for ch in str(dept_name))[:60]
                    bundle.writestr(f"{safe_name}.csv", block.to_csv(index=False))
                bundle.writestr(
                    "MANIFEST.txt",
                    f"OVERWATCH chargeback statements - {company} - {month}\n"
                    f"Rate: ${rate:.2f}/credit (CORE settings). Warehouse metering is exact; "
                    f"idle time bills to the owning department.\n"
                    f"Total: ${float(frame['USD'].sum()):,.2f} across "
                    f"{frame['DEPARTMENT'].nunique()} departments.",
                )
            st.download_button(
                "Download statements (.zip)", data=buffer.getvalue(),
                file_name=f"overwatch_chargeback_{company}_{month}.zip",
                mime="application/zip", key="cb_dl",
            )
            st.success(f"{frame['DEPARTMENT'].nunique()} department statements for {month}.")

def _chargeback_tab(company: str, days: int, rate: float, is_operator: bool) -> None:
    """Department chargeback: warehouse = billing truth, role = usage lens."""
    dept_res = run(chargeback_sql.department_window_credits(days, company), page=_PAGE,
                   key=f"cb_dept_{company}_{days}", tier="historical",
                   source="WAREHOUSE_METERING_HISTORY x DEPARTMENT_MAP")
    if not guard(dept_res, "No warehouse credits in this window.",
                 setup_hint="Not installed yet — an admin can verify on Admin → Migrations & freshness. Seed department names in DEPARTMENT_MAP."):
        return
    df = dept_res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    dept = df.groupby("DEPARTMENT", as_index=False)["USD"].sum().sort_values("USD", ascending=False)
    unmapped_usd = float(dept[dept["DEPARTMENT"] == "Unmapped"]["USD"].sum())
    total_usd = float(dept["USD"].sum())

    kpi_row([
        {"label": f"Chargeback total ({days}d)", "value": format_usd(total_usd),
         "help": "Exact warehouse metering x rate. Reconciles to the scoped spend by construction."},
        {"label": "Departments", "value": f"{dept['DEPARTMENT'].nunique()}"},
        {"label": "Unmapped", "value": format_usd(unmapped_usd),
         "delta": "map warehouses below" if unmapped_usd > 0 else "fully mapped",
         "delta_color": "inverse" if unmapped_usd > 0 else "normal",
         "help": "Credits from warehouses with no DEPARTMENT_MAP row. Should be $0."},
    ])
    charts.bar_usd(dept, "DEPARTMENT", "USD", title="Spend (USD, exact)")
    styled_table(
        df[["DEPARTMENT", "WAREHOUSE_NAME", "COMPANY", "CREDITS_TOTAL", "USD"]],
        column_config={"USD": st.column_config.NumberColumn("Spend $", format="$%.0f")},
    )
    result_caption(dept_res, note="Idle credits stay with the owning department - that is the point of chargeback.")

    st.markdown("**Role usage within warehouses (allocated)**")
    st.caption(
        "Elapsed-time share per role inside each warehouse x that warehouse's exact spend. "
        "Usage lens for conversations, not the billing number."
    )
    share_res = run(chargeback_sql.role_share_within_warehouse(days, company), page=_PAGE,
                    key=f"cb_share_{company}_{days}", tier="historical",
                    source="QUERY_HISTORY (elapsed share per warehouse)")
    if share_res.usable():
        wh_usd = df.set_index("WAREHOUSE_NAME")["USD"].to_dict()
        share = share_res.df.copy()
        share["ALLOCATED_USD"] = share.apply(
            lambda r: round(safe_float(r["ELAPSED_SHARE"]) * wh_usd.get(str(r["WAREHOUSE_NAME"]), 0.0), 2),
            axis=1,
        )
        by_role = (share.groupby("ROLE_NAME", as_index=False)["ALLOCATED_USD"].sum()
                   .sort_values("ALLOCATED_USD", ascending=False))
        charts.bar_usd(by_role, "ROLE_NAME", "ALLOCATED_USD", title="Allocated $ by role", top_n=12)
        with st.expander("Role detail per warehouse"):
            styled_table(
                share[["WAREHOUSE_NAME", "ROLE_NAME", "QUERY_COUNT", "ELAPSED_SHARE", "ALLOCATED_USD"]],
                column_config={
                    "ELAPSED_SHARE": st.column_config.NumberColumn("Share", format="%.3f"),
                    "ALLOCATED_USD": st.column_config.NumberColumn("Allocated $", format="$%.0f"),
                },
            )

    st.markdown("**Department budgets & pace**")
    panel_help(
        "Budgets live in DEPT_BUDGETS; the hourly scan raises COST_DEPT_BUDGET_PACE when a "
        "department runs ahead of pace (threshold on the Alerts page). Spend is the "
        "department's warehouses — exact billing, same as the table above."
    )
    bud = run(mart_sql.dept_budgets(), page=_PAGE, key="dept_budgets", tier="live",
              source="DEPT_BUDGETS")
    if bud.ok and not bud.empty:
        st.dataframe(bud.df, hide_index=True, use_container_width=True)
    elif bud.ok:
        st.info("No department budgets set yet — add one below and the pace alert goes live.")
    if is_operator:
        dmap = run(chargeback_sql.department_map(), page=_PAGE, key="cb_dmap_bud", tier="recent",
                   source="DEPARTMENT_MAP")
        dept_opts = (sorted(dmap.df["DEPARTMENT"].astype(str).unique())
                     if dmap.usable() and "DEPARTMENT" in dmap.df.columns else [])
        c_d, c_b = st.columns(2)
        pick_dept = c_d.selectbox("Department", dept_opts, key="bud_dept") if dept_opts else             c_d.text_input("Department", key="bud_dept_txt")
        bud_usd = c_b.number_input("Monthly budget USD (0 removes)", 0, 10_000_000, 0,
                                   step=500, key="bud_usd")
        if st.button("Save budget", key="bud_save", disabled=not pick_dept):
            if bud_usd > 0:
                stmt_b = (
                    f"MERGE INTO {core_object('DEPT_BUDGETS')} t "
                    f"USING (SELECT {sql_literal(str(pick_dept))} AS D) s ON t.DEPARTMENT = s.D "
                    f"WHEN MATCHED THEN UPDATE SET MONTHLY_BUDGET_USD = {sql_number(float(bud_usd))}, "
                    "UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = CURRENT_USER() "
                    f"WHEN NOT MATCHED THEN INSERT (DEPARTMENT, MONTHLY_BUDGET_USD) "
                    f"VALUES (s.D, {sql_number(float(bud_usd))});"
                )
            else:
                stmt_b = (f"DELETE FROM {core_object('DEPT_BUDGETS')} "
                          f"WHERE DEPARTMENT = {sql_literal(str(pick_dept))};")
            ok, msg = execute_statement(stmt_b, page=_PAGE)
            notify(ok, msg if not ok else f"Budget saved for {pick_dept}.")

    _statement_export(company, rate)

    with st.expander("Manage mapping"):
        map_res = run(chargeback_sql.department_map(), page=_PAGE, key="cb_map", tier="recent",
                      source="DEPARTMENT_MAP")
        if map_res.usable():
            styled_table(map_res.df, height=280)
        unmapped_whs = sorted(df[df["DEPARTMENT"] == "Unmapped"]["WAREHOUSE_NAME"].unique())
        c1, c2, c3 = st.columns(3)
        with c1:
            map_type = st.selectbox("Type", ["WAREHOUSE", "ROLE"], key="cb_map_type")
        with c2:
            default_name = unmapped_whs[0] if unmapped_whs and map_type == "WAREHOUSE" else ""
            name = st.text_input("Name", value=default_name, key="cb_map_name")
        with c3:
            department = st.text_input("Department", key="cb_map_dept")
        owner = st.text_input("Owner", value="DBA", key="cb_map_owner")
        merge_sql = (
            f"MERGE INTO {core_object('DEPARTMENT_MAP')} t\n"
            f"USING (SELECT {sql_literal(map_type)} AS MAP_TYPE, {sql_literal(name.upper())} AS NAME, "
            f"{sql_literal(department)} AS DEPARTMENT, {sql_literal(owner)} AS OWNER) s\n"
            "ON t.MAP_TYPE = s.MAP_TYPE AND t.NAME = s.NAME\n"
            "WHEN MATCHED THEN UPDATE SET DEPARTMENT = s.DEPARTMENT, OWNER = s.OWNER, "
            "UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = CURRENT_USER()\n"
            "WHEN NOT MATCHED THEN INSERT (MAP_TYPE, NAME, DEPARTMENT, OWNER) "
            "VALUES (s.MAP_TYPE, s.NAME, s.DEPARTMENT, s.OWNER);"
        )
        st.code(merge_sql, language="sql")
        if is_operator and name and department and st.button("Execute mapping", key="cb_map_exec"):
            ok, msg = execute_statement(merge_sql.replace("\n", " "), page=_PAGE)
            notify(ok, msg)
        elif not is_operator:
            st.caption("Copy and run as OVERWATCH_OPERATOR - in-app execution needs the operator role.")
