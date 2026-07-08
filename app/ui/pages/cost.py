"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.errors import safe_page
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal, sql_number
from app.core.state import filters
from app.data import chargeback_sql, cortex_sql, cost_sql, insights_sql, mart_sql, ops_sql, security_sql
from app.logic import contract_planner, remediation, steering
from app.logic.actions import LEDGER_ESTIMATED, can_verify, ledger_totals
from app.logic.ai_prompts import idle_warehouse_prompt
from app.logic.anomaly import anomaly_summary, flag_anomalies
from app.logic.cortex import classify_exceptions, enrich_user_rollup, rollup_summary
from app.logic.forecast import contract_pace
from app.logic.formulas import account_today, credits_to_usd, format_usd, pct_delta, safe_float
from app.logic.insights import flag_repeat_candidates, idle_advisor, idle_suspend_sql, storage_movers
from app.logic.sizing import price_per_run_bounds, simulate_scenario, size_recommendations, sizing_summary
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    blast_radius,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    panel_help,
    result_caption,
    section_header,
    selectable_table,
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


def _categorize(service: str) -> str:
    s = str(service or "").upper()
    if "CORTEX" in s or s.startswith("AI") or "INTELLIGENCE" in s:
        return "AI / Cortex"
    return _SERVICE_CATEGORY.get(s, "Other")


def _spend_tab(company: str, days: int, rate: float, ai_rate: float) -> None:
    # Hot path: the daily metering fact carries the same columns; fall back
    # to live ACCOUNT_USAGE only when the fact has no rows yet.
    res = run(mart_sql.fact_metering_by_service(days), page=_PAGE, key=f"metering_fact_{days}",
              tier="recent", source="FACT_METERING_DAILY (mart, loaded hourly)")
    if not res.ok or res.empty:
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

    st.markdown("**Cloud-services health by warehouse**")
    st.caption(
        "Cloud services above ~10% of a warehouse's credits means many tiny queries, "
        "metadata-heavy patterns, or compile-heavy SQL. The COST_CLOUD_SVC_RATIO alert "
        "fires at the ELEVATED threshold (editable on the Alerts page)."
    )
    csr = run(cost_sql.cloud_services_ratio_by_warehouse(days, company), page=_PAGE,
              key=f"cs_ratio_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY")
    if guard(csr, "No warehouse metering in this window."):
        styled_table(csr.df, height=260)
        result_caption(csr)
        elevated = csr.df[csr.df["STATUS"].astype(str) == "ELEVATED"]
        if not elevated.empty:
            st.markdown("**Why is it elevated? Compile-heavy query families**")
            comp = run(cost_sql.compile_heavy_families(days, company), page=_PAGE,
                       key=f"compile_fams_{company}_{days}", tier="historical",
                       source="ACCOUNT_USAGE.QUERY_HISTORY (COMPILATION_TIME)")
            if guard(comp, "No query family with 20+ runs averages >0.5s compile time — "
                           "the ratio driver is likely many tiny/metadata queries instead."):
                st.dataframe(comp.df, hide_index=True, use_container_width=True)
                result_caption(comp)


def _attribution_tab(company: str, days: int, rate: float, database: str = "", schema_contains: str = "") -> None:
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
                res = run(cost_sql.allocated_attribution(days, dim, company, database, schema_contains), page=_PAGE,
                          key=f"alloc_{dim}_{company}_{days}", tier="historical",
                          source="ACCOUNT_USAGE.QUERY_HISTORY (elapsed share)")
                if guard(res, f"No query history to allocate by {label}."):
                    vdf = res.df.copy()
                    usd_col = next((c for c in vdf.columns if str(c).upper().endswith('_USD') or str(c).upper() == 'ALLOCATED_USD'), None)
                    label_col = vdf.columns[0]
                    if usd_col is not None and len(vdf) > 1:
                        charts.waterfall_usd(vdf, label_col, usd_col)
                        st.caption('Waterfall: how the window total builds up, largest contributors first (allocated).')
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
        st.caption("Anomaly flags appear once 30 days of per-warehouse daily facts have loaded.")


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
    pace = contract_pace(consumed, contract_credits, start, end, account_today())
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

    st.markdown("**Steering to commit — the levers, in dollars per day**")
    idle_lv = run(insights_sql.idle_warehouse_analysis(30, "ALL"), page=_PAGE,
                  key="steer_idle", tier="historical",
                  source="idle advisor (30d, account-wide)")
    pats_lv = run(insights_sql.expensive_patterns_usd(30, "ALL", 10), page=_PAGE,
                  key="steer_pats", tier="historical",
                  source="recurring patterns (30d, account-wide)")
    rate_st = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    levers: dict = {}
    if idle_lv.usable():
        adv_st = idle_advisor(idle_lv.df, rate_st, 30)
        levers["Auto-suspend tuning (idle burn)"] = float(
            adv_st["PROJECTED_MONTHLY_IDLE_USD"].sum())
    if pats_lv.usable():
        top5 = pats_lv.df.head(5)
        levers["Top-5 recurring patterns (cache/materialize)"] = float(
            (pd.to_numeric(top5["CREDITS_PER_DAY"], errors="coerce").fillna(0) * rate_st).sum() * 30)
    plan = steering.steering_plan(
        projected_term_credits=pace["projected_term_credits"],
        contract_credits=contract_credits,
        days_remaining=pace["days_remaining"],
        rate_usd=rate_st, levers_monthly_usd=levers,
    )
    if not plan.get("ok"):
        st.info(str(plan.get("verdict")))
    else:
        (st.success if plan["gap_usd"] <= 0 or plan["coverage_pct"] >= 100 else st.warning)(
            plan["verdict"])
        if plan["rows"]:
            styled_table(pd.DataFrame(plan["rows"]), height=140)
        st.caption(
            "Lever estimates come straight from the idle advisor and recurring-pattern "
            "panels (execute them on Optimization & Savings). Estimates, not promises — "
            "the savings verifier proves them after the fact."
        )

    st.divider()
    st.markdown("**Renewal planner (what-if)**")
    panel_help(
        "Straight-line scenarios from the trailing 30-day burn — no seasonality is "
        "invented. Recommended commit = term consumption plus your buffer. Use it to "
        "walk into the renewal with a number instead of a feeling."
    )
    burn_res = run(mart_sql.fact_daily_spend(30), page=_PAGE, key="planner_burn",
                   tier="recent", source="FACT_METERING_DAILY")
    if guard(burn_res, "Need the metering fact loaded to plan (run the hourly task once)."):
        rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
        bdf = burn_res.df.copy()
        daily_usd = float(pd.to_numeric(bdf["CREDITS_BILLED"], errors="coerce").fillna(0).mean()) * rate_now
        remaining_usd = max(0.0, (contract_credits - consumed) * rate_now)
        col1, col2, col3 = st.columns(3)
        term_months = col1.slider("Next term (months)", 12, 36, 12, step=6, key="plan_term")
        buffer_pct = col2.slider("Safety buffer %", 0, 40, 15, step=5, key="plan_buffer")
        extra_credits = col3.number_input("What-if: add load (credits/day)", 0, 10000, 0,
                                          step=10, key="plan_extra",
                                          help="Hypothetical new workload (e.g. a planned XL "
                                               "warehouse). Reprojects every scenario and the "
                                               "exhaustion date.")
        daily_usd_adj = daily_usd + float(extra_credits) * rate_now
        rows = contract_planner.plan_scenarios(daily_usd_adj, term_months, buffer_pct, remaining_usd)
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={
                         "TERM_CONSUMPTION_USD": st.column_config.NumberColumn("Term consumption", format="$%.0f"),
                         "RECOMMENDED_COMMIT_USD": st.column_config.NumberColumn("Recommended commit", format="$%.0f"),
                         "DAILY_BURN_USD": st.column_config.NumberColumn("Daily burn", format="$%.2f"),
                     })
        st.caption(f"Basis: ${daily_usd:,.0f}/day observed over 30d at ${rate_now}/credit"
                   + (f" + ${float(extra_credits) * rate_now:,.0f}/day hypothetical load"
                      if extra_credits else "") + ". "
                   "Exhaustion applies to the current contract's remaining "
                   f"{contract_credits - consumed:,.0f} credits.")


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


@st.fragment
def _whatif_panel(sized, days: int, rate: float) -> None:
    """Fragment: slider moves rerun this panel only, not the whole page."""
    with st.expander("Interactive what-if: size step + auto-suspend together"):
        st.caption(
            "Replays this window's observed credits under a size step and a new "
            "auto-suspend, as a bounded range — not a promise. Busy credits land "
            "between rate-scaled (queries keep their wall time) and cost-neutral "
            "(perfect runtime scaling); idle scales with the suspend window."
        )
        wi_names = sized["WAREHOUSE_NAME"].astype(str).tolist()
        wi_pick = st.selectbox("Warehouse", wi_names, key="whatif_wh")
        wrow_wi = sized[sized["WAREHOUSE_NAME"].astype(str) == wi_pick].iloc[0]
        live_size, live_suspend = "", 600
        whs_wi = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                     tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        if whs_wi.ok and not whs_wi.empty:
            wdf_wi = whs_wi.df.copy()
            wdf_wi.columns = [str(c).lower() for c in wdf_wi.columns]
            match = wdf_wi[wdf_wi.get("name", "").astype(str) == wi_pick] if "name" in wdf_wi.columns else wdf_wi.iloc[0:0]
            if not match.empty:
                live_size = str(match.iloc[0].get("size", "") or "")
                live_suspend = int(safe_float(match.iloc[0].get("auto_suspend"), 600) or 600)
        c_sz, c_sus = st.columns(2)
        with c_sz:
            delta_wi = st.select_slider("Size step", options=[-2, -1, 0, 1, 2], value=0,
                                        key="whatif_delta")
        with c_sus:
            sus_wi = st.select_slider("New auto-suspend (s)",
                                      options=[30, 60, 120, 300, 600, 900],
                                      value=60, key="whatif_suspend")
        idle_wi = safe_float(wrow_wi.get("CREDITS_TOTAL")) * safe_float(wrow_wi.get("IDLE_PCT")) / 100.0
        sim = simulate_scenario(
            size=live_size or "MEDIUM",
            credits_window=safe_float(wrow_wi.get("CREDITS_TOTAL")),
            idle_credits_window=idle_wi,
            window_days=days, rate_usd=rate, size_delta=int(delta_wi),
            autosuspend_now_s=live_suspend, autosuspend_new_s=int(sus_wi),
        )
        if not sim.get("ok"):
            st.info(str(sim.get("reason", "Cannot simulate this warehouse."))
                    + (" (SHOW WAREHOUSES did not return its size.)" if not live_size else ""))
        else:
            kpi_row([
                {"label": f"Now ({sim['size_now']}, {live_suspend}s suspend)",
                 "value": format_usd(sim["monthly_now_usd"]),
                 "help": "Observed window scaled to 30 days at the configured rate."},
                {"label": f"Scenario ({sim['size_new']}, {int(sus_wi)}s)",
                 "value": f"{format_usd(sim['monthly_low_usd'])} – {format_usd(sim['monthly_high_usd'])}",
                 "severity": ("ok" if sim["monthly_high_usd"] <= sim["monthly_now_usd"] else
                              "warn" if sim["monthly_low_usd"] <= sim["monthly_now_usd"] else "bad"),
                 "help": "Bounded range — both ends of the stated assumptions."},
            ])
            for a_line in sim["assumptions"]:
                st.caption(f"· {a_line}")


def _optimization_tab(company: str, days: int, rate: float, settings: dict, is_operator: bool) -> None:
    """Ported optimization insights: idle warehouses, repeat queries, storage movers."""
    # ---- 1. Idle warehouse advisor -------------------------------------------
    st.markdown("**Idle warehouse advisor**")
    st.caption("Credits billed in warehouse-hours with zero queries — the auto-suspend opportunity.")
    idle_res = run(insights_sql.idle_warehouse_analysis(days, company), page=_PAGE,
                   key=f"idle_{company}_{days}", tier="historical",
                   source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (hourly join)")
    if guard(idle_res, "No warehouse metering in this window."):
        advisor = idle_advisor(idle_res.df, rate, days)
        flagged = advisor[advisor["FLAGGED"]]
        total_idle = float(advisor["IDLE_USD"].sum())
        kpi_row([
            {"label": f"Idle spend ({days}d)", "value": format_usd(total_idle),
             "help": "Credits billed while no query ran on the warehouse."},
            {"label": "Projected monthly idle", "value": format_usd(float(advisor["PROJECTED_MONTHLY_IDLE_USD"].sum()))},
            {"label": "Warehouses flagged", "value": f"{len(flagged)}",
             "help": ">=20% idle share and >=1 idle credit."},
        ])
        styled_table(
            advisor[["WAREHOUSE_NAME", "COMPANY", "TOTAL_CREDITS", "IDLE_CREDITS",
                      "IDLE_PCT", "IDLE_USD", "PROJECTED_MONTHLY_IDLE_USD", "FLAGGED", "RECOMMENDATION"]],
            column_config={
                "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.1f%%"),
                "IDLE_USD": st.column_config.NumberColumn("Idle $", format="$%.0f"),
                "PROJECTED_MONTHLY_IDLE_USD": st.column_config.NumberColumn("Proj. monthly $", format="$%.0f"),
            },
        )
        result_caption(idle_res, note="Hour-slice granularity; short auto-suspends already reduce this.")
        if not flagged.empty:
            with st.expander("Generated remediation + savings-ledger entries"):
                statements = []
                for _, r in flagged.head(10).iterrows():
                    statements.append(idle_suspend_sql(r["WAREHOUSE_NAME"]))
                    statements.append(
                        f"INSERT INTO {core_object('SAVINGS_LEDGER')} (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL)\n"
                        f"VALUES ({sql_literal('Auto-suspend tune: ' + str(r['WAREHOUSE_NAME']))}, 'ESTIMATED', "
                        f"{sql_number(r['PROJECTED_MONTHLY_IDLE_USD'])}, "
                        f"{sql_literal('Re-run idle analysis for ' + str(r['WAREHOUSE_NAME']) + ' after the change; verify idle $ drop.')});"
                    )
                st.code("\n".join(statements), language="sql")
                st.caption("Review and run as OVERWATCH_OPERATOR. Warehouse changes are never executed from the app.")
        ai_evaluation_panel(
            key=f"idle_{company}_{days}",
            prompt=idle_warehouse_prompt(advisor, company, days),
            settings=settings,
            page=_PAGE,
            subject="evaluate idle warehouse spend",
        )

    st.divider()
    # ---- Right-sizing simulator ------------------------------------------------
    st.markdown("**Warehouse right-sizing simulator**")
    st.caption(
        "Mechanical scenario model: one Snowflake size step halves or doubles the credit rate. "
        "Runtime effects depend on the workload - the rationale says why; you decide."
    )
    prof_res = run(insights_sql.warehouse_sizing_profile(days, company), page=_PAGE,
                   key=f"sizing_{company}_{days}", tier="historical",
                   source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (profile)")
    if guard(prof_res, "No warehouse activity to profile in this window."):
        sized = size_recommendations(prof_res.df, rate, days)
        summary = sizing_summary(sized)
        kpi_row([
            {"label": "Size up / add cluster", "value": f"{summary['up']}",
             "delta_color": "inverse" if summary["up"] else "off",
             "help": "Sustained queueing or remote spill in the window."},
            {"label": "Size-down candidates", "value": f"{summary['down']}"},
            {"label": "Tune auto-suspend first", "value": f"{summary['suspend']}"},
            {"label": "Potential saving (down)", "value": format_usd(summary["potential_saving_usd"]),
             "help": "Half-rate scenario on down candidates only. Model, not a promise."},
        ])
        sel_sz = selectable_table(
            sized[["WAREHOUSE_NAME", "COMPANY", "RECOMMENDATION", "RATIONALE",
                    "MONTHLY_USD_NOW", "SCENARIO_DOWN_USD", "SCENARIO_UP_USD",
                    "QUEUED_MIN_PER_DAY", "SPILL_REMOTE_GB", "P95_ELAPSED_SEC", "IDLE_PCT"]],
            key="sizing_sel",
            column_config={
                "MONTHLY_USD_NOW": st.column_config.NumberColumn("Now $/mo", format="$%.0f"),
                "SCENARIO_DOWN_USD": st.column_config.NumberColumn("x0.5 $/mo", format="$%.0f"),
                "SCENARIO_UP_USD": st.column_config.NumberColumn("x2 $/mo", format="$%.0f"),
                "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.0f%%"),
            },
        )
        if sel_sz is not None and is_operator:
            srow = sized.iloc[int(sel_sz)]
            target_size = st.selectbox("Resize to", ["XSMALL", "SMALL", "MEDIUM", "LARGE"],
                                       key="sizing_to")
            stmt_sz = remediation.resize_fix(str(srow["WAREHOUSE_NAME"]), target_size)
            st.code(stmt_sz, language="sql")
            est_sz = 0.0
            if str(srow.get("RECOMMENDATION", "")).upper().startswith("DOWN"):
                est_sz = round(max(0.0, safe_float(srow.get("MONTHLY_USD_NOW"))
                                    - safe_float(srow.get("SCENARIO_DOWN_USD"))), 2)
                st.caption(f"Half-rate scenario saving ~${est_sz:,.0f}/mo (ESTIMATED until the verifier proves it).")
            blast_radius(str(srow["WAREHOUSE_NAME"]), _PAGE)
            confirm_sz = st.text_input("Type the warehouse name to confirm resize", key="sizing_confirm")
            if st.button("Execute resize + log", key="sizing_exec",
                         disabled=(confirm_sz != str(srow["WAREHOUSE_NAME"]))):
                ok, msg = execute_statement(stmt_sz, page=_PAGE)
                execute_statement(
                    f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                    "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE) "
                    f"SELECT 'RESIZE', {sql_literal(str(srow['WAREHOUSE_NAME']))}, {sql_literal(stmt_sz)}, "
                    f"{sql_number(est_sz)}, {sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}",
                    page=_PAGE)
                if ok and est_sz > 0:
                    execute_statement(
                        f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                        "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES) "
                        f"SELECT {sql_literal('Resize ' + str(srow['WAREHOUSE_NAME']) + ' to ' + target_size)}, "
                        f"'ESTIMATED', {sql_number(est_sz)}, {sql_literal(stmt_sz)}, "
                        "'Booked from sizing simulator; verifier tests actuals.'", page=_PAGE)
                notify(ok, msg)
        _whatif_panel(sized, days, rate)
        result_caption(prof_res)

    st.divider()
    # ---- Most expensive queries (allocated $) --------------------------------
    st.markdown("**Most expensive queries (allocated $)**")
    if st.toggle("Run expensive-query scan", key="cost_expq_toggle",
                 help="Splits each warehouse-hour's credits across that hour's queries "
                      "by execution-time share. The heaviest scan after repeat-queries."):
        with st.spinner("Allocating warehouse-hour credits across queries…"):
            expq = run(insights_sql.expensive_queries_usd(days, company, 50), page=_PAGE,
                       key=f"expq_{company}_{days}", tier="historical",
                       source="QUERY_HISTORY x WAREHOUSE_METERING_HISTORY (hour-share allocation)")
        if guard(expq, "No warehouse queries in this window."):
            edf_q = expq.df.copy()
            edf_q["ALLOCATED_USD"] = edf_q["ALLOCATED_CREDITS"].map(
                lambda c: round(safe_float(c) * rate, 2))
            top_total = float(edf_q["ALLOCATED_USD"].sum())
            kpi_row([
                {"label": "Top-50 allocated spend", "value": format_usd(top_total),
                 "help": "Allocated, not billed: Snowflake bills the warehouse, not the query. "
                         "Hour-of-start bucketing; idle-only hours excluded (idle advisor owns those)."},
                {"label": "Costliest single query",
                 "value": format_usd(float(edf_q["ALLOCATED_USD"].max())) if len(edf_q) else "$0"},
            ])
            styled_table(
                edf_q[["ALLOCATED_USD", "USER_NAME", "WAREHOUSE_NAME", "QUERY_TYPE",
                       "EXECUTION_STATUS", "ELAPSED_SEC", "START_TIME", "QUERY_SNIPPET", "QUERY_ID"]],
                column_config={"ALLOCATED_USD": st.column_config.NumberColumn("Allocated $", format="$%.2f")},
            )
            result_caption(expq, note="allocated by execution-second share within each warehouse-hour")
            st.caption("Chase the top rows in Operations → Queries (query drill-through) by QUERY_ID.")

        st.markdown("**Recurring cost patterns (same query, run all day)**")
        st.caption("Grouped by parameterized fingerprint: a $9 query run 400x outranks one $300 "
                   "outlier — this is where caching/materialization actually pays.")
        pats = run(insights_sql.expensive_patterns_usd(days, company, 30), page=_PAGE,
                   key=f"exppat_{company}_{days}", tier="historical",
                   source="QUERY_HISTORY x METERING (hour-share, by QUERY_PARAMETERIZED_HASH)")
        if guard(pats, "No fingerprint with 5+ runs carrying allocated credits in this window."):
            pdf_c = pats.df.copy()
            pdf_c["USD_TOTAL"] = pdf_c["ALLOCATED_CREDITS"].map(lambda c: round(safe_float(c) * rate, 2))
            pdf_c["USD_PER_DAY"] = pdf_c["CREDITS_PER_DAY"].map(lambda c: round(safe_float(c) * rate, 2))
            styled_table(
                pdf_c[["USD_PER_DAY", "USD_TOTAL", "RUNS", "USERS", "WAREHOUSES",
                       "QUERY_SNIPPET", "PATTERN_HASH"]],
                column_config={
                    "USD_PER_DAY": st.column_config.NumberColumn("$/day", format="$%.2f"),
                    "USD_TOTAL": st.column_config.NumberColumn(f"$ ({days}d)", format="$%.2f"),
                },
            )
            result_caption(pats, note="candidates for result-cache reuse, materialization, or a schedule")
            st.markdown("**Price a pattern (estimate before prod)**")
            pat_opts = pdf_c["PATTERN_HASH"].astype(str).tolist()
            pat_pick = st.selectbox("Pattern", pat_opts, key="price_pat",
                                    format_func=lambda h: h[:16] + "…")
            prow = pdf_c[pdf_c["PATTERN_HASH"].astype(str) == pat_pick].iloc[0]
            delta_pr = st.select_slider("At size step", options=[-2, -1, 0, 1, 2], value=0,
                                        key="price_delta")
            bounds = price_per_run_bounds(safe_float(prow["ALLOCATED_CREDITS"]),
                                          int(prow["RUNS"]), rate, int(delta_pr))
            kpi_row([
                {"label": "Observed $/run", "value": f"${bounds['per_run_now_usd']:.4f}",
                 "help": f"{int(prow['RUNS'])} runs in {days}d, hour-share allocated."},
                {"label": f"At {'+' if delta_pr > 0 else ''}{delta_pr} size step",
                 "value": f"${bounds['per_run_low_usd']:.4f} – ${bounds['per_run_high_usd']:.4f}",
                 "help": "Bounds: rate-scaled (same wall time) vs cost-neutral "
                         "(perfect runtime scaling) — same assumptions as the what-if."},
            ])
            st.caption(f"Sample: {str(prow['QUERY_SNIPPET'])[:120]}")

    st.divider()
    # ---- 2. Repeat-query candidates -------------------------------------------
    st.markdown("**Repeat-query candidates (cache / materialization)**")
    _rq_on = st.toggle("Run repeat-query scan (fingerprints the window's QUERY_HISTORY)",
                       key="cost_repeatq_toggle",
                       help="The heaviest scan on this page — runs only when you ask.")
    if _rq_on:
        with st.spinner("Fingerprinting the window's QUERY_HISTORY…"):
            rq_res = run(insights_sql.repeat_query_fingerprints(
            days, company,
            database=st.session_state.get("flt_database", ""),
            schema_contains=st.session_state.get("flt_schema_contains", "")), page=_PAGE,
                     key=f"repeatq_{company}_{days}", tier="historical",
                     source="QUERY_HISTORY (QUERY_PARAMETERIZED_HASH)")
        if guard(rq_res, "No query fingerprints with 10+ successful runs in this window.",
                 setup_hint="Needs QUERY_PARAMETERIZED_HASH (standard in current Snowflake accounts)."):
            candidates = flag_repeat_candidates(rq_res.df)
            hot = candidates[candidates["CANDIDATE"]]
            kpi_row([
                {"label": "Repeated fingerprints", "value": f"{len(candidates)}"},
                {"label": "Materialization candidates", "value": f"{len(hot)}",
                 "help": ">=0.5h total compute and <=25% cache hit."},
                {"label": "Compute in repeats", "value": f"{float(candidates['TOTAL_ELAPSED_HOURS'].sum()):,.1f} h"},
            ])
            styled_table(
                candidates[["RUNS", "USERS", "TOTAL_ELAPSED_HOURS", "AVG_ELAPSED_SEC",
                             "TOTAL_TB_SCANNED", "AVG_CACHE_PCT", "CANDIDATE", "QUERY_PREVIEW"]],
                column_config={
                    "TOTAL_ELAPSED_HOURS": st.column_config.NumberColumn("Total hours", format="%.2f"),
                    "AVG_CACHE_PCT": st.column_config.NumberColumn("Cache %", format="%.0f%%"),
                    "TOTAL_TB_SCANNED": st.column_config.NumberColumn("TB scanned", format="%.3f"),
                },
            )
            result_caption(rq_res, note="Same parameterized query shape grouped across users/warehouses.")

        st.divider()
    # ---- 3. Storage growth movers ------------------------------------------------
    st.markdown("**Storage growth movers**")
    days_storage = max(days, 30)
    sg_res = run(insights_sql.storage_growth_by_database(days_storage, company), page=_PAGE,
                 key=f"storgrow_{company}_{days_storage}", tier="historical",
                 source="DATABASE_STORAGE_USAGE_HISTORY")
    if guard(sg_res, "No storage history for this scope."):
        movers = storage_movers(sg_res.df, safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0))
        growing = movers[movers["GROWTH_TB"] > 0]
        kpi_row([
            {"label": "Current storage", "value": f"{float(movers['CURRENT_TB'].sum()):,.2f} TB"},
            {"label": f"Growth ({days_storage}d)", "value": f"{float(movers['GROWTH_TB'].sum()):,.2f} TB"},
            {"label": "Projected growth $/mo", "value": format_usd(float(growing['GROWTH_USD_30D'].sum())),
             "help": "Growth rate extended 30 days x storage rate. Display estimate."},
        ])
        charts.bar_usd(growing.head(10), "DATABASE_NAME", "GROWTH_USD_30D", title="Projected growth $/mo")
        st.dataframe(
            movers[["DATABASE_NAME", "COMPANY", "CURRENT_TB", "GROWTH_TB", "GROWTH_TB_30D",
                     "GROWTH_USD_30D", "FAILSAFE_SHARE_PCT"]],
            hide_index=True, use_container_width=True,
            column_config={
                "GROWTH_USD_30D": st.column_config.NumberColumn("Growth $/mo", format="$%.0f"),
                "FAILSAFE_SHARE_PCT": st.column_config.NumberColumn("Failsafe %", format="%.1f%%"),
            },
        )
        result_caption(sg_res, note=f"Window widened to {days_storage}d for a stable growth slope.")

    st.divider()
    st.markdown("**Query efficiency (pruning + result cache)**")
    if st.toggle("Run query-efficiency scan", key="cost_eff_toggle",
                 help="Scans the window's QUERY_HISTORY for full-table-scan families and the zero-scan share."):
        prune = run(ops_sql.poor_pruning_queries(
            days, company,
            database=st.session_state.get("flt_database", ""),
            schema_contains=st.session_state.get("flt_schema_contains", "")), page=_PAGE,
                    key=f"prune_{company}_{days}", tier="historical",
                    source="ACCOUNT_USAGE.QUERY_HISTORY (PARTITIONS_SCANNED)")
        if prune.ok and prune.empty:
            st.success("No query family scans >80% of a 100+-partition table in this window.")
        elif guard(prune, ""):
            st.caption("These families read almost every micro-partition — clustering keys or "
                       "better predicates would cut both runtime and credits.")
            st.dataframe(prune.df, hide_index=True, use_container_width=True)
            result_caption(prune)
        cache = run(ops_sql.result_cache_daily(days, company), page=_PAGE,
                    key=f"cachehit_{company}_{days}", tier="historical",
                    source="ACCOUNT_USAGE.QUERY_HISTORY (BYTES_SCANNED = 0)")
        if guard(cache, "No queries in the window."):
            charts.daily_metric_line(cache.df, "DAY", "HIT_PCT", "zero-scan answers %")
            st.caption("Share of successful queries answered without scanning (result cache + "
                       "metadata). A falling line means redundant recomputation.")

    st.markdown("**Storage waste (Time Travel / failsafe / stale tables)**")
    if st.toggle("Run storage-waste scan", key="cost_waste_toggle",
                 help="Top tables by retention bytes, flagged STALE when no DML touched them in 90 days."):
        with st.spinner("Scanning table storage + 90 days of read/DML history…"):
            waste = run(insights_sql.storage_reclaim(company), page=_PAGE,
                        key=f"reclaim_{company}", tier="historical",
                        source="TABLE_STORAGE_METRICS + DML + ACCESS_HISTORY (reads, 90d)")
            reads_available = waste.ok
            if not waste.ok:
                # ACCESS_HISTORY needs Enterprise edition — degrade to the DML-only view.
                waste = run(insights_sql.storage_waste(company), page=_PAGE,
                            key=f"waste_{company}", tier="historical",
                            source="TABLE_STORAGE_METRICS + TABLE_DML_HISTORY")
        if waste.ok and waste.empty:
            st.success("No table above 1 GB of combined active + retention bytes in this scope.")
        elif guard(waste, ""):
            sdf = waste.df.copy()
            if "DML_STATUS" in sdf.columns:
                sdf = sdf.rename(columns={"DML_STATUS": "STATUS"})
            stale = sdf[sdf["STATUS"].astype(str) == "STALE"]
            kpis_w = [
                {"label": "Tables shown", "value": f"{len(sdf)}"},
                {"label": "Stale (no DML 90d)", "value": f"{len(stale)}",
                 "delta_color": "inverse" if len(stale) else "off"},
                {"label": "Stale retention GB",
                 "value": f"{float(stale['TIME_TRAVEL_GB'].sum() + stale['FAILSAFE_GB'].sum()):,.0f}"
                          if not stale.empty else "0"},
            ]
            if reads_available and "NEVER_READ" in sdf.columns:
                never = sdf[sdf["NEVER_READ"].astype(bool) & (sdf["STATUS"].astype(str) == "STALE")]
                kpis_w.append({
                    "label": "Stale AND never read (90d)", "value": f"{len(never)}",
                    "severity": "warn" if len(never) else "ok",
                    "help": "No DML and no reads in ACCESS_HISTORY for 90 days — the "
                            "safe-to-archive shortlist. Verify with owners before dropping.",
                })
            kpi_row(kpis_w)
            sel_w = selectable_table(sdf, key="waste_sel", height=300)
            st.caption("Stale + heavy retention = candidates for DATA_RETENTION_TIME_IN_DAYS "
                       "reduction, transient conversion, or dropping."
                       + ("" if reads_available else
                          " Read evidence unavailable (ACCESS_HISTORY needs Enterprise edition) — "
                          "showing DML-only staleness."))
            result_caption(waste)
            if sel_w is not None:
                _trow = sdf.iloc[int(sel_w)]
                st.markdown(f"**Object TCO — `{_trow['DATABASE_NAME']}.{_trow['SCHEMA_NAME']}.{_trow['TABLE_NAME']}`**")
                _st_gb = (safe_float(_trow.get("ACTIVE_GB")) + safe_float(_trow.get("TIME_TRAVEL_GB"))
                          + safe_float(_trow.get("FAILSAFE_GB")) + safe_float(_trow.get("CLONE_RETAINED_GB")))
                _st_usd = round(_st_gb / 1024 * safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0), 2)
                try:
                    tco = run(insights_sql.table_tco(str(_trow["DATABASE_NAME"]), str(_trow["SCHEMA_NAME"]),
                                                     str(_trow["TABLE_NAME"]), 30),
                              page=_PAGE, key=f"tco_{sel_w}", tier="historical",
                              source="ACCESS_HISTORY (reads + writes, 30d)")
                except ValueError:
                    tco = None  # exotic identifier: storage economics still shown
                _reads = _writes = 0
                _last_read = None
                if tco is not None and tco.usable():
                    for _, krow in tco.df.iterrows():
                        if str(krow["KIND"]) == "READ":
                            _reads = int(safe_float(krow["TOUCHES"]))
                            _last_read = krow.get("LAST_TOUCH")
                        else:
                            _writes = int(safe_float(krow["TOUCHES"]))
                kpi_row([
                    {"label": "Storage $/mo", "value": f"${_st_usd:,.2f}",
                     "help": f"{_st_gb:,.1f} GB total incl. retention + clone-retained."},
                    {"label": "Reads (30d)", "value": f"{_reads:,}",
                     "severity": "warn" if _reads == 0 else "ok"},
                    {"label": "Writes (30d)", "value": f"{_writes:,}",
                     "help": "Writes with zero reads = paying to refresh an unread table."},
                ])
                if tco is not None and not tco.ok:
                    st.caption("Read/write evidence needs ACCESS_HISTORY (Enterprise) — "
                               "storage economics shown from TABLE_STORAGE_METRICS alone.")
                elif _writes > 0 and _reads == 0:
                    st.warning("Being refreshed but never read in 30d — retire-candidate: "
                               "pause the writer AND reduce retention below.")
                elif _reads == 0:
                    st.info("No reads in 30d" + (" (last read unknown)" if _last_read is None else "")
                            + " — confirm with the owner, then reclaim below.")
            if sel_w is not None and is_operator:
                wrow = sdf.iloc[int(sel_w)]
                keep_days = st.number_input("Set retention days", 0, 90, 1, key="waste_days")
                stmt_w = remediation.retention_fix(str(wrow["DATABASE_NAME"]),
                                                   str(wrow["SCHEMA_NAME"]),
                                                   str(wrow["TABLE_NAME"]), int(keep_days))
                st.code(stmt_w, language="sql")
                est_w = round((safe_float(wrow.get("TIME_TRAVEL_GB")) + safe_float(wrow.get("FAILSAFE_GB")))
                              / 1024 * safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0), 2)
                st.caption(f"Frees ~{safe_float(wrow.get('TIME_TRAVEL_GB')) + safe_float(wrow.get('FAILSAFE_GB')):,.0f} GB "
                           f"of retention (~${est_w:,.2f}/mo, ESTIMATED). Failsafe drains over 7 days.")
                confirm_w = st.text_input("Type the table name to confirm", key="waste_confirm")
                if st.button("Execute retention change + log", key="waste_exec",
                             disabled=(confirm_w != str(wrow["TABLE_NAME"]))):
                    ok, msg = execute_statement(stmt_w, page=_PAGE)
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE) "
                        f"SELECT 'RETENTION', {sql_literal('.'.join([str(wrow['DATABASE_NAME']), str(wrow['SCHEMA_NAME']), str(wrow['TABLE_NAME'])]))}, "
                        f"{sql_literal(stmt_w)}, {sql_number(est_w)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}", page=_PAGE)
                    if ok and est_w > 0:
                        execute_statement(
                            f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                            "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES) "
                            f"SELECT {sql_literal('Retention ' + str(wrow['TABLE_NAME']) + ' -> ' + str(int(keep_days)) + 'd')}, "
                            f"'ESTIMATED', {sql_number(est_w)}, {sql_literal(stmt_w)}, "
                            "'Booked from storage-waste scan.'", page=_PAGE)
                    notify(ok, msg)

    st.divider()
    st.markdown("**Guarded remediation (generate → review → execute)**")
    panel_help(
        "Turns findings into exact `ALTER` statements. Execution needs the "
        "OVERWATCH_OPERATOR role, writes a REMEDIATION_LOG audit row, and books an "
        "ESTIMATED savings-ledger item that the monthly verifier later proves or rejects. "
        "Anyone can copy the SQL for review."
    )
    idle_res = run(insights_sql.idle_warehouse_analysis(days, company), page=_PAGE,
                   key=f"remed_idle_{company}_{days}", tier="recent",
                   source="WAREHOUSE_METERING_HISTORY + FACT_QUERY_HOURLY")
    if guard(idle_res, "No warehouse activity in the window to remediate."):
        idf = idle_res.df.copy()
        wh_pick = st.selectbox("Warehouse", sorted(idf["WAREHOUSE_NAME"].astype(str)),
                               key="remed_wh")
        fix_kind = st.radio("Fix", ["Tighten auto-suspend to 60s", "Off-hours suspend/resume schedule"],
                            horizontal=True, key="remed_kind")
        row = idf[idf["WAREHOUSE_NAME"].astype(str) == wh_pick]
        idle_credits = float(pd.to_numeric(row["IDLE_CREDITS"], errors="coerce").fillna(0).iloc[0]) if not row.empty else 0.0
        est_monthly = remediation.monthly_savings_estimate(idle_credits, days, rate)

        stmt = ""
        if fix_kind.startswith("Tighten"):
            stmt = remediation.auto_suspend_fix(wh_pick, 60)
            st.caption(f"Idle credits in window: {idle_credits:,.1f} → estimated ${est_monthly:,.0f}/mo if suspend catches them (ESTIMATED until verified).")
        else:
            prof = run(insights_sql.warehouse_hourly_activity(14, company), page=_PAGE,
                       key=f"remed_prof_{company}", tier="recent",
                       source="hour-of-day activity profile")
            if guard(prof, "No hourly profile available yet."):
                charts.hour_heatmap(prof.df, "WAREHOUSE_NAME", "HOUR_OF_DAY", "AVG_CREDITS",
                                    title="avg credits/hour")
                st.caption("Dark cells with no matching query activity are the schedule opportunity.")
                mine = prof.df[prof.df["WAREHOUSE_NAME"].astype(str) == wh_pick]
                proposal = remediation.propose_quiet_window(mine.to_dict("records"))
                if proposal is None:
                    st.info("No contiguous 4h+ window where this warehouse burns credits with ~no queries — "
                            "a schedule would not pay for its risk. Auto-suspend is the better fix here.")
                else:
                    st.caption(
                        f"Quiet window {proposal['start']:02d}:00–{proposal['end']:02d}:00 "
                        f"({proposal['hours']}h, ~{proposal['avg_credits_per_day']} credits/day burned idle). "
                        f"Weekday schedule below; review before executing — resume-on-demand still works "
                        f"if a job fires early."
                    )
                    stmt = remediation.suspend_schedule(wh_pick, proposal["start"], proposal["end"])
                    est_monthly = round(proposal["avg_credits_per_day"] * 30 * rate * 5 / 7, 2)

        if stmt:
            st.code(stmt, language="sql")
            if is_operator:
                confirm = st.text_input("Type the warehouse name to confirm execution", key="remed_confirm")
                if st.button("Execute + log + book estimated savings", key="remed_exec",
                             disabled=(confirm != wh_pick)):
                    ok, msg = execute_statement(stmt, page=_PAGE)
                    log_sql = (
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE) "
                        f"SELECT {sql_literal('AUTO_SUSPEND' if fix_kind.startswith('Tighten') else 'SCHEDULE')}, "
                        f"{sql_literal(wh_pick)}, {sql_literal(stmt[:4000])}, {sql_number(est_monthly)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}"
                    )
                    execute_statement(log_sql, page=_PAGE)
                    if ok and est_monthly > 0:
                        ledger_sql = (
                            f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                            "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES) "
                            f"SELECT {sql_literal(f'{fix_kind} on {wh_pick}')}, 'ESTIMATED', "
                            f"{sql_number(est_monthly)}, {sql_literal(stmt[:4000])}, "
                            f"{sql_literal('Booked by guarded remediation; verifier will test actuals.')}"
                        )
                        execute_statement(ledger_sql, page=_PAGE)
                    notify(ok, msg)
            else:
                st.caption("Copy the SQL freely; executing from the app requires OVERWATCH_OPERATOR.")

    remlog = run(mart_sql.remediation_log(50), page=_PAGE, key="remed_log", tier="live",
                 source="REMEDIATION_LOG")
    if remlog.ok and not remlog.empty:
        st.markdown("**Remediation history**")
        styled_table(remlog.df, height=200)


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


@st.fragment
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


def _savings_tab() -> None:
    res = run(mart_sql.savings_ledger(), page=_PAGE, key="savings_ledger",
              tier="live", source="SAVINGS_LEDGER")
    if not res.ok:
        st.info("Savings ledger is not installed yet — an admin can verify on Admin → Migrations & freshness.")
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
        styled_table(res.df[["CREATED_AT", "DESCRIPTION", "STATE", "ESTIMATED_USD", "VERIFIED_USD", "VERIFIED_BY"]])

    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES

    runs = run(mart_sql.savings_verification_runs(), page=_PAGE, key="savings_runs",
               tier="recent", source="SAVINGS_VERIFICATION_RUNS")
    if runs.usable():
        st.markdown("**Auto-verification (monthly re-measurement)**")
        st.caption(
            "TASK_VERIFY_SAVINGS re-measures each ESTIMATED auto-suspend item's idle spend and "
            "proposes a verified amount. Apply it below with the standard proof-gated verify flow."
        )
        styled_table(runs.df, height=260,
                     column_config={
                         "BASELINE_EST_USD": st.column_config.NumberColumn("Baseline est. $", format="$%.0f"),
                         "MEASURED_IDLE_USD_30D": st.column_config.NumberColumn("Idle now (30d) $", format="$%.0f"),
                         "PROPOSED_VERIFIED_USD": st.column_config.NumberColumn("Proposed verified $", format="$%.0f"),
                     })

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
            notify(ok, msg)
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
                    notify(ok, msg)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    page_header("Cost & Contract",
                "Where the money goes, whether the contract holds, and what savings are proven.",
                scope_note=f"{f['company']} · last {f['days']} days", icon_name="cost")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    # Four grouped sections instead of eight pills (CoCo density fix): each
    # group renders its related sub-panels under labeled section headers.
    section = lazy_sections(
        ["Spend & Attribution", "Contract & Forecast", "Chargeback & AI",
         "Optimization & Savings"], key="cost_section")
    if section == "Spend & Attribution":
        section_header("Spend", "info", "spend")
        _spend_tab(f["company"], f["days"], rate, ai_rate)
        st.divider()
        section_header("Attribution", "info", "chargeback")
        _attribution_tab(f["company"], f["days"], rate, f["database"], f["schema_contains"])
        st.divider()
        section_header("Query-tag governance", "info", "chargeback")
        st.caption("Chargeback precision is capped by tag coverage — untagged execution "
                   "time can only be allocated, never attributed.")
        tags_res = run(cost_sql.tag_coverage(f["days"], f["company"]), page=_PAGE,
                       key=f"tagcov_{f['company']}_{f['days']}", tier="historical",
                       source="QUERY_HISTORY (exec-time-weighted tag coverage)")
        if guard(tags_res, "No workloads above the 60s floor in this window."):
            tdf_g = tags_res.df.copy()
            total_exec = float(tdf_g["EXEC_SEC"].sum())
            untagged = float(tdf_g["UNTAGGED_EXEC_SEC"].sum())
            kpi_row([
                {"label": "Tagged share (exec-time)",
                 "value": f"{(1 - untagged / total_exec) * 100 if total_exec else 100:,.1f}%",
                 "severity": "ok" if total_exec and untagged / total_exec < 0.3 else "warn"},
                {"label": "Top untagged user",
                 "value": str(tdf_g.iloc[0]["USER_NAME"]) if len(tdf_g) else "n/a",
                 "delta": f"{float(tdf_g.iloc[0]['UNTAGGED_EXEC_SEC']) / 3600:,.1f}h untagged" if len(tdf_g) else None,
                 "delta_color": "off"},
            ])
            styled_table(tdf_g, height=260, column_config={
                "TAGGED_PCT": st.column_config.NumberColumn("Tagged %", format="%.1f%%")})
            st.caption("Fix at the source: set QUERY_TAG in the tool/session that runs the "
                       "workload; the scoreboard moves within a day.")
    elif section == "Contract & Forecast":
        section_header("Contract pacing & renewal planner", "info", "contract")
        _contract_tab(settings)
    elif section == "Chargeback & AI":
        section_header("Department chargeback", "info", "chargeback")
        _chargeback_tab(f["company"], f["days"], rate, is_operator)
        st.divider()
        section_header("Cortex & storage", "info", "cost")
        _cortex_storage_tab(f["company"], f["days"], ai_rate, settings)
        st.divider()
        section_header("AI users", "info", "operations")
        _ai_users_tab(f["company"], f["days"], ai_rate, settings, is_operator)
    else:
        section_header("Optimization", "info", "optimize")
        _optimization_tab(f["company"], f["days"], rate, settings, is_operator)
        st.divider()
        section_header("Savings ledger", "ok", "cost")
        _savings_tab()
