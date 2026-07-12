"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal, sql_number
from app.data import insights_sql, mart27_sql, mart_sql, ops_sql, security_sql
from app.logic import remediation
from app.logic.actions import LEDGER_ESTIMATED, can_verify, ledger_totals
from app.logic.ai_prompts import idle_warehouse_prompt
from app.logic.formulas import format_usd, safe_float
from app.logic.insights import flag_repeat_candidates, idle_advisor, idle_suspend_sql, storage_movers
from app.logic.sizing import price_per_run_bounds, simulate_scenario, size_recommendations, sizing_summary
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    blast_radius,
    guard,
    kpi_row,
    notify,
    panel_help,
    result_caption,
    run_mart_first,
    selectable_table,
    styled_table,
    toggle_cost_hint,
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

@st.fragment
def _whatif_panel(sized, days: int, rate: float) -> None:
    """Fragment: slider moves rerun this panel only, not the whole page.

    r21 #4: the docstring claimed fragment for months while the decorator
    was missing — every slider move re-rendered the whole grouped Cost page.
    The AST lock in test_codex_r21 makes 'Fragment:' docstrings binding."""
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
    idle_res = run_mart_first(
        mart27_sql.eff_idle_analysis(days, company),
        insights_sql.idle_warehouse_analysis(days, company),
        page=_PAGE, key=f"idle_{company}_{days}",
        mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart, loaded hourly)",
        live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")
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
    # On-demand (Codex r3 #5): the profile joins metering x QUERY_HISTORY over
    # the whole window (~90s cold at 60d in production telemetry). The idle
    # advisor above answers the everyday question; load this when sizing.
    st.caption(toggle_cost_hint("sizing"))
    if not st.toggle("Load right-sizing profile (heavy scan)", key="sizing_load",
                     help="Profiles every warehouse over the window: queueing, spill, "
                          "p95, idle share, and x0.5/x2 cost scenarios."):
        st.caption("Toggle to run the per-warehouse sizing profile on demand.")
    elif guard((prof_res := run_mart_first(
                    mart27_sql.eff_sizing_profile(days, company),
                    insights_sql.warehouse_sizing_profile(days, company),
                    page=_PAGE, key=f"sizing_{company}_{days}",
                    mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart — p95 is peak daily)",
                    live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")),
               "No warehouse activity to profile in this window."):
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
            from app.logic import remediation as _remediation
            st.caption(_remediation.reverse_hint("RESIZE", str(srow["WAREHOUSE_NAME"])))
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
                if ok:
                    from app.ui.components import log_ui_event
                    log_ui_event("remediation_exec", page=_PAGE)
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
            expq = run(insights_sql.expensive_queries_usd(
                           days, company, 50,
                           database=st.session_state.get("flt_database", ""),
                           schema_contains=st.session_state.get("flt_schema_contains", "")),
                       page=_PAGE, key=f"expq_{company}_{days}", tier="historical",
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
    st.caption(toggle_cost_hint("repeatq"))
    _rq_on = st.toggle("Run repeat-query scan (fingerprints the window's QUERY_HISTORY)",
                       key="cost_repeatq_toggle",
                       help="The heaviest scan on this page — runs only when you ask.")
    if _rq_on:
        with st.spinner("Fingerprinting the window's QUERY_HISTORY…"):
            rq_res = run_mart_first(
                mart27_sql.family_repeat_fingerprints(
                    days, company,
                    database=st.session_state.get("flt_database", ""),
                    schema_contains=st.session_state.get("flt_schema_contains", "")),
                insights_sql.repeat_query_fingerprints(
                    days, company,
                    database=st.session_state.get("flt_database", ""),
                    schema_contains=st.session_state.get("flt_schema_contains", "")),
                page=_PAGE, key=f"repeatq_{company}_{days}",
                mart_source="MART_QUERY_FAMILY_DAILY (mart — exec-time grain, day-level LAST_RUN)",
                live_source="QUERY_HISTORY (QUERY_PARAMETERIZED_HASH, live fallback)")
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
    toggle_cost_hint("prune_")   # v4.26.1: the pain table counted these
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
            styled_table(prune.df)
            result_caption(prune)
        cache = run(ops_sql.result_cache_daily(days, company), page=_PAGE,
                    key=f"cachehit_{company}_{days}", tier="historical",
                    source="ACCOUNT_USAGE.QUERY_HISTORY (BYTES_SCANNED = 0)")
        if guard(cache, "No queries in the window."):
            charts.daily_metric_line(cache.df, "DAY", "HIT_PCT", "zero-scan answers %")
            st.caption("Share of successful queries answered without scanning (result cache + "
                       "metadata). A falling line means redundant recomputation.")

    st.markdown("**Storage waste (Time Travel / failsafe / stale tables)**")
    toggle_cost_hint("reclaim_")
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

    st.markdown("**Automatic clustering spend (per table)**")
    st.caption(toggle_cost_hint("clustering_"))
    if st.toggle("Run clustering-spend scan", key="cost_clustering_toggle",
                 help="Serverless reclustering credits per table over the window — "
                      "a table rewriting itself daily is a silent burner."):
        clu = run(insights_sql.clustering_by_table(max(days, 30), company), page=_PAGE,
                  key=f"clustering_{company}_{days}", tier="historical",
                  source="ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY")
        if clu.ok and clu.empty:
            st.success("No automatic-clustering credits in this window.")
        elif guard(clu, ""):
            styled_table(clu.df, height=240)
            st.caption("High credits with high TB reclustered = revisit the clustering "
                       "key or the load pattern; dollars = credits x your compute rate.")
            result_caption(clu)

    st.divider()
    st.markdown("**Guarded remediation (generate → review → execute)**")
    panel_help(
        "Turns findings into exact `ALTER` statements. Execution needs the "
        "OVERWATCH_OPERATOR role, writes a REMEDIATION_LOG audit row, and books an "
        "ESTIMATED savings-ledger item that the monthly verifier later proves or rejects. "
        "Anyone can copy the SQL for review."
    )
    # Same builder PAIR as the advisor above (r20 #1): identical SQL identity
    # means this is served from the advisor's cache — the remediation block
    # no longer pays its own live metering x history join when the mart is up.
    idle_res = run_mart_first(
        mart27_sql.eff_idle_analysis(days, company),
        insights_sql.idle_warehouse_analysis(days, company),
        page=_PAGE, key=f"remed_idle_{company}_{days}",
        mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart, loaded hourly)",
        live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")
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

def _savings_tab() -> None:
    res = run(mart_sql.savings_ledger(), page=_PAGE, key="savings_ledger",
              tier="live", source="SAVINGS_LEDGER")
    if not res.ok:
        st.info("Savings ledger is not installed yet — an admin can verify on Admin → Migrations & freshness.")
        return
    totals = ledger_totals(res.df)
    st.caption("Books itself since V038: the daily scan detects cost-lever changes "
               "(auto-suspend, size, clusters, scaling policy) wherever they were "
               "made — Snowsight included — and settles each against 14 days of "
               "measured actuals. Manual items remain for one-offs.")
    kpi_row([
        {"label": "Verified savings", "value": format_usd(totals["verified_usd"]),
         "delta": f"{totals['verified_count']} items",
         "help": "Measured post-period proof. This is the number to quote."},
        {"label": "Estimated (unverified)", "value": format_usd(totals["estimated_usd"]),
         "delta": f"{totals['estimated_count']} items", "delta_color": "off",
         "help": "Never added to verified. Auto-booked items settle themselves "
                 "when the 14-day verdict lands; manual items use the verify flow."},
    ])
    if res.empty:
        st.info("Nothing booked yet — the autobook task fills this as warehouse "
                "cost-lever changes are detected (needs migration V038).")
    else:
        styled_table(res.df[[c for c in ("CREATED_AT", "SOURCE", "DESCRIPTION", "STATE",
                                          "ESTIMATED_USD", "VERIFIED_USD", "VERIFIED_BY")
                             if c in res.df.columns]])

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
