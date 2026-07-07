"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.errors import safe_page
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal, sql_number
from app.core.state import filters
from app.data import cortex_sql, cost_sql, mart_sql
from app.logic.actions import LEDGER_ESTIMATED, can_verify, ledger_totals
from app.logic.anomaly import anomaly_summary, flag_anomalies
from app.logic.cortex import classify_exceptions, enrich_user_rollup, rollup_summary
from app.logic.forecast import contract_pace
from app.logic.formulas import credits_to_usd, format_usd, pct_delta, safe_float
from app.ui import charts
from app.ui.components import guard, kpi_row, load_settings, page_header, result_caption

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


def _categorize(service: str) -> str:
    s = str(service or "").upper()
    if "CORTEX" in s or s.startswith("AI") or "INTELLIGENCE" in s:
        return "AI / Cortex"
    return _SERVICE_CATEGORY.get(s, "Other")


def _spend_tab(company: str, days: int, rate: float, ai_rate: float) -> None:
    res = run(cost_sql.metering_daily_by_service(days), page=_PAGE, key=f"metering_{days}",
              tier="historical", source="ACCOUNT_USAGE.METERING_DAILY_HISTORY")
    if not guard(res, "No metering rows in this window yet (the view lags up to 24h)."):
        return
    df = res.df.copy()
    df["CATEGORY"] = df["SERVICE_TYPE"].map(_categorize)
    df["RATE"] = df["CATEGORY"].map(lambda c: ai_rate if c == "AI / Cortex" else rate)
    df["USD"] = df["CREDITS_BILLED"].map(safe_float) * df["RATE"]
    df["ADJ_USD"] = df["CREDITS_ADJUSTMENT"].map(safe_float) * df["RATE"]

    billed_usd = float(df["USD"].sum())
    rebate_usd = float(df["ADJ_USD"].sum())  # negative or zero
    kpi_row([
        {"label": f"Billed spend, {days}d (account)", "value": format_usd(billed_usd),
         "help": "Billed credits x rate. Includes the cloud-services adjustment."},
        {"label": "Cloud-services rebate applied", "value": format_usd(abs(rebate_usd)),
         "help": "CREDITS_ADJUSTMENT_CLOUD_SERVICES — money the old dashboard ignored."},
        {"label": "Compute rate", "value": f"${rate:.2f}/cr", "help": "SETTINGS CREDIT_PRICE_USD."},
        {"label": "Cortex rate", "value": f"${ai_rate:.2f}/cr", "help": "SETTINGS AI_CREDIT_PRICE_USD."},
    ])
    st.caption("Account-wide by service (METERING_DAILY_HISTORY has no company grain; company split lives in Attribution).")
    charts.daily_stacked_usd(df, "DAY", "CATEGORY", "USD")
    result_caption(res)


def _attribution_tab(company: str, days: int, rate: float) -> None:
    wh = run(cost_sql.warehouse_window_vs_prior(days, company), page=_PAGE,
             key=f"wh_vs_prior_{company}_{days}", tier="historical",
             source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY")
    st.markdown("**By warehouse (exact metering)**")
    if guard(wh, "No warehouse credits in this window."):
        view = wh.df.copy()
        view["USD_CURRENT"] = view["CREDITS_CURRENT"].map(lambda c: credits_to_usd(c, rate))
        view["USD_PRIOR"] = view["CREDITS_PRIOR"].map(lambda c: credits_to_usd(c, rate))
        view["DELTA_PCT"] = view.apply(lambda r: pct_delta(r["USD_CURRENT"], r["USD_PRIOR"]), axis=1)
        st.dataframe(
            view[["WAREHOUSE_NAME", "COMPANY", "USD_CURRENT", "USD_PRIOR", "DELTA_PCT"]],
            hide_index=True, use_container_width=True,
            column_config={
                "USD_CURRENT": st.column_config.NumberColumn("Current $", format="$%.2f"),
                "USD_PRIOR": st.column_config.NumberColumn("Prior $", format="$%.2f"),
                "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            },
        )
        window_usd = float(view["USD_CURRENT"].sum())
        result_caption(wh, note="Both windows offset 24h for ACCOUNT_USAGE completeness.")

        st.markdown("**By user and database (allocated — estimate)**")
        st.caption(
            "Snowflake bills at warehouse grain. These split the scoped warehouse spend "
            f"({format_usd(window_usd)}) by query elapsed-time share; treat as directionally correct."
        )
        col_u, col_d = st.columns(2)
        for col, dim, label in ((col_u, "USER_NAME", "user"), (col_d, "DATABASE_NAME", "database")):
            with col:
                res = run(cost_sql.allocated_attribution(days, dim, company), page=_PAGE,
                          key=f"alloc_{dim}_{company}_{days}", tier="historical",
                          source="ACCOUNT_USAGE.QUERY_HISTORY (elapsed share)")
                if guard(res, f"No query history to allocate by {label}."):
                    alloc = res.df.copy()
                    alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"].map(safe_float) * window_usd
                    charts.bar_usd(alloc, "DIMENSION", "ALLOCATED_USD", title=f"Allocated $ by {label}")

    st.markdown("**Daily anomaly check (per warehouse)**")
    daily = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE,
                key=f"fact_wh_daily_{company}", tier="recent", source="FACT_WAREHOUSE_DAILY")
    if daily.usable():
        flagged = flag_anomalies(
            daily.df.assign(USD=lambda d: d["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))),
            "USD", group_col="WAREHOUSE_NAME",
        )
        hits = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")
        if hits:
            for h in hits[:5]:
                st.warning(f"{h['label']}: daily spend ${h['value']:,.0f} (robust z {h['z']:+.1f}) — investigate.")
        else:
            st.success("No daily spend anomalies in the last 30 days (median/MAD z < 3.5).")
    else:
        st.caption("Anomaly check needs FACT_WAREHOUSE_DAILY (V002) — 30 days of per-warehouse dailies.")


def _contract_tab(settings: dict) -> None:
    contract_credits = safe_float(settings.get("CONTRACT_CREDITS"))
    start_s = str(settings.get("CONTRACT_START_DATE") or "").strip()
    end_s = str(settings.get("CONTRACT_END_DATE") or "").strip()
    if contract_credits <= 0 or not start_s or not end_s:
        st.info(
            "Contract pacing is not configured. Set CONTRACT_CREDITS, CONTRACT_START_DATE and "
            "CONTRACT_END_DATE on the Admin page. Nothing is assumed."
        )
        return
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        st.error(f"Contract dates in SETTINGS are not YYYY-MM-DD: {start_s!r} / {end_s!r}.")
        return
    res = run(cost_sql.contract_consumed_credits(start_s), page=_PAGE, key="contract_consumed",
              tier="historical", source="ACCOUNT_USAGE.METERING_DAILY_HISTORY")
    if not guard(res, "No metering rows since the contract start."):
        return
    consumed = safe_float(res.df.iloc[0].get("CREDITS_BILLED_TO_DATE"))
    pace = contract_pace(consumed, contract_credits, start, end, date.today())
    if not pace.get("ok"):
        st.info(str(pace.get("reason")))
        return
    kpi_row([
        {"label": "Consumed", "value": f"{consumed:,.0f} cr", "delta": f"{pace['consumed_share']:.1f}% of contract"},
        {"label": "Contract clock", "value": f"{pace['time_share']:.1f}%", "help": f"{pace['days_remaining']} days remaining."},
        {"label": "Pace", "value": f"{pace['pace_ratio']:.2f}x",
         "delta": "burning fast" if pace["pace_ratio"] > 1 else "under pace",
         "delta_color": "inverse" if pace["pace_ratio"] > 1 else "normal"},
        {"label": "Projected term total", "value": f"{pace['projected_term_credits']:,.0f} cr",
         "delta": (f"+{pace['projected_overage_credits']:,.0f} cr overage" if pace["projected_overage_credits"] > 0 else "within contract"),
         "delta_color": "inverse" if pace["projected_overage_credits"] > 0 else "normal"},
    ])
    result_caption(res, note="Billed credits (cloud-services adjustment applied) since contract start.")


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
        res = run(cost_sql.storage_by_database(days, company), page=_PAGE,
                  key=f"storage_{company}_{days}", tier="historical",
                  source="ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY")
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

    enriched = enrich_user_rollup(rollup_res.df, ai_rate)
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
        enriched[["USER_NAME", "EMAIL", "SOURCE", "ACTIVE_DAYS", "TOTAL_REQUESTS",
                   "TOTAL_CREDITS", "CREDITS_PER_REQUEST", "SPEND_USD", "PROJECTED_30D_USD"]],
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
        st.dataframe(
            exceptions[["SEVERITY", "SIGNAL", "USER_NAME", "SOURCE", "TOTAL_REQUESTS",
                         "CREDITS_PER_REQUEST", "PROJECTED_30D_USD"]],
            hide_index=True, use_container_width=True,
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


def _savings_tab() -> None:
    res = run(mart_sql.savings_ledger(), page=_PAGE, key="savings_ledger",
              tier="live", source="SAVINGS_LEDGER")
    if not res.ok:
        st.info("Savings ledger not deployed yet (migration V005).")
        return
    totals = ledger_totals(res.df)
    kpi_row([
        {"label": "Verified savings", "value": format_usd(totals["verified_usd"]),
         "delta": f"{totals['verified_count']} items",
         "help": "Post-period proof attached. This is the number to quote."},
        {"label": "Estimated (unverified)", "value": format_usd(totals["estimated_usd"]),
         "delta": f"{totals['estimated_count']} items", "delta_color": "off",
         "help": "Never added to verified. Verify or reject each item."},
    ])
    if res.empty:
        st.info("Ledger is empty. Add an item below when an optimization ships.")
    else:
        st.dataframe(
            res.df[["CREATED_AT", "DESCRIPTION", "STATE", "ESTIMATED_USD", "VERIFIED_USD", "VERIFIED_BY"]],
            hide_index=True, use_container_width=True,
        )

    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES

    with st.expander("Add estimated savings item"):
        desc = st.text_input("Description", key="ledger_desc", max_chars=400)
        est = st.number_input("Estimated USD", min_value=0.0, step=50.0, key="ledger_est")
        proof = st.text_area("Proof query (required to verify later)", key="ledger_proof", height=80)
        insert_sql = (
            f"INSERT INTO {core_object('SAVINGS_LEDGER')} (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL)\n"
            f"VALUES ({sql_literal(desc)}, {sql_literal(LEDGER_ESTIMATED)}, {sql_number(est)}, {sql_literal(proof)});"
        )
        st.code(insert_sql, language="sql")
        if is_operator and desc and st.button("Execute insert", key="ledger_add_exec"):
            ok, msg = execute_statement(insert_sql, page=_PAGE)
            (st.success if ok else st.error)(msg)
        elif not is_operator:
            st.caption("Copy and run as OVERWATCH_OPERATOR — in-app execution needs the operator role.")

    if not res.empty:
        with st.expander("Verify an estimated item (proof required)"):
            estimated = res.df[res.df["STATE"].astype(str).str.upper() == LEDGER_ESTIMATED]
            if estimated.empty:
                st.caption("No ESTIMATED items to verify.")
            else:
                options = {f"{r['DESCRIPTION'][:60]} ({r['ITEM_ID'][:8]})": r for _, r in estimated.iterrows()}
                chosen = st.selectbox("Item", list(options), key="ledger_verify_pick")
                row = options[chosen]
                verified_usd = st.number_input("Verified USD (measured, post-period)",
                                               min_value=0.0, step=50.0, key="ledger_verified_usd")
                check = {"STATE": row["STATE"], "PROOF_SQL": row["PROOF_SQL"], "VERIFIED_USD": verified_usd}
                allowed, why = can_verify(check)
                update_sql = (
                    f"UPDATE {core_object('SAVINGS_LEDGER')}\n"
                    f"SET STATE = 'VERIFIED', VERIFIED_USD = {sql_number(verified_usd)}, "
                    f"VERIFIED_AT = CURRENT_TIMESTAMP(), VERIFIED_BY = CURRENT_USER()\n"
                    f"WHERE ITEM_ID = {sql_literal(row['ITEM_ID'])};"
                )
                st.code(update_sql, language="sql")
                if not allowed:
                    st.warning(why)
                elif is_operator and st.button("Execute verification", key="ledger_verify_exec"):
                    ok, msg = execute_statement(update_sql, page=_PAGE)
                    (st.success if ok else st.error)(msg)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    page_header("Cost & Contract",
                "Where the money goes, whether the contract holds, and what savings are proven.",
                scope_note=f"{f['company']} · last {f['days']} days")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    tab_spend, tab_attr, tab_contract, tab_ai, tab_users, tab_savings = st.tabs(
        ["Spend", "Attribution", "Contract", "Cortex & Storage", "AI Users", "Savings ledger"]
    )
    with tab_spend:
        _spend_tab(f["company"], f["days"], rate, ai_rate)
    with tab_attr:
        _attribution_tab(f["company"], f["days"], rate)
    with tab_contract:
        _contract_tab(settings)
    with tab_ai:
        _cortex_storage_tab(f["company"], f["days"], ai_rate, settings)
    with tab_users:
        _ai_users_tab(f["company"], f["days"], ai_rate, settings, is_operator)
    with tab_savings:
        _savings_tab()
