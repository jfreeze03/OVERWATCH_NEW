"""Cost & Contract — the Optimization & Savings section bodies (advisors,
scans, guarded remediation, savings ledger).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.

C1 (audit 2026-07-31) — the divisor rule for this whole file: the live SQL
builders clamp to MAX_LIVE_WINDOW_DAYS (90) while the window picker goes to
365. Any per-day rate or x30 projection must divide by the
window that was actually SERVED — components.served_days(result, days) for a
run_mart_first read, bounded_days(days) for a plain live read — never by the
requested `days`. Dividing 90 days of credits by 365 read ~4x low on every
projection on this page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import core_object
from app.core.identity import identity_sql
from app.core.query import execute_statement, run
from app.core.session import is_operator as _is_operator
from app.core.sqlsafe import sql_literal, sql_number
from app.core.state import request_navigation
from app.data import cost_sql, insights_sql, mart27_sql, mart_sql, ops_sql, security_sql
from app.data.common import bounded_days
from app.logic import remediation
from app.logic.actions import LEDGER_ESTIMATED, can_verify, ledger_totals
from app.logic.ai_prompts import idle_warehouse_prompt
from app.logic.capacity import capacity_forecasts
from app.logic.consolidation import WarehouseProfile, consolidation_candidates
from app.logic.formulas import format_usd, humanize_duration, md_dollars, safe_float
from app.logic.insights import (
    IDLE_TARGET_SUSPEND_SEC,
    flag_repeat_candidates,
    idle_advisor,
    idle_suspend_sql,
    repeat_min_runs,
    storage_movers,
    with_auto_suspend_settings,
)
from app.logic.savings_rollup import (
    SavingsOpportunity,
    confidence_weight,
    rollup_savings,
)
from app.logic.serverless_roi import classify_qas_roi
from app.logic.sizing import price_per_run_bounds, simulate_scenario, size_recommendations, sizing_summary
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    blast_radius,
    confirm_gate,
    empty_state,
    guard,
    kpi_row,
    lazy_sections,
    notify,
    panel_help,
    result_caption,
    run_mart_first,
    selectable_nav_table,
    selectable_table,
    served_days,
    snowsight_profile_column,
    styled_table,
    toggle_cost_hint,
    with_user_names,
)

_PAGE = "Cost & Contract"


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
        load_settings = st.toggle(
            "Load current warehouse settings", key="whatif_live_settings",
            help="Runs SHOW WAREHOUSES only when this scenario needs live size and auto-suspend.",
        )
        whs_wi = run(
            security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
            tier="metadata", source="SHOW WAREHOUSES", max_rows=0,
        ) if load_settings else None
        if whs_wi is not None and whs_wi.ok and not whs_wi.empty:
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
        if not live_size:
            # D2: the sim silently fell back to MEDIUM, so a size step off an
            # X-Small warehouse quoted 4x the real dollars — and the only hint
            # ("SHOW WAREHOUSES did not return its size") lived on the failure
            # branch that the MEDIUM default made unreachable.
            if load_settings:
                st.warning(
                    "SHOW WAREHOUSES did not return this warehouse's size — the scenario below "
                    "assumes MEDIUM. Treat the size-step dollars as illustrative only; the "
                    "auto-suspend half of the answer is unaffected."
                )
            else:
                st.caption(
                    "Current settings are not loaded. The preview uses MEDIUM and 600s until "
                    "you turn on the live-settings control above."
                )
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

def _capacity_forecast_panel(company: str) -> None:
    """On-demand, mart-only forecast of recurring warehouse pressure."""
    st.markdown("**Capacity pressure forecast**")
    st.caption(
        "Complete days only, fixed 90-day evidence window. A forecast appears only when "
        "pressure recurs, workload growth agrees, recent settings are stable, and a 7-day "
        "holdout is accurate enough."
    )
    if not st.toggle(
        "Load capacity forecast",
        key="capacity_forecast_load",
        help="Reads warehouse daily facts and change history; no live system-view fallback.",
    ):
        st.caption("Load on demand after the current sizing evidence, not on first paint.")
        return
    result = run(
        mart_sql.warehouse_capacity_daily(90, company),
        page=_PAGE,
        key=f"capacity_forecast_{company}_90",
        tier="hourly",
        source="FACT_QUERY_HOURLY + FACT_WAREHOUSE_DAILY + WAREHOUSE_CHANGE_REGISTRY",
    )
    if not guard(result, "No complete-day warehouse facts are available for forecasting."):
        return
    forecast = capacity_forecasts(result.df)
    if forecast.empty:
        empty_state("no_data_yet", "No warehouse has enough complete-day evidence yet.")
        return
    statuses = forecast["STATUS"].value_counts().to_dict()
    kpi_row([
        {"label": "Pressure now", "value": str(statuses.get("PRESSURE NOW", 0)),
         "severity": "bad" if statuses.get("PRESSURE NOW", 0) else ""},
        {"label": "Forecasted <=180d", "value": str(statuses.get("FORECAST", 0)),
         "severity": "warn" if statuses.get("FORECAST", 0) else ""},
        {"label": "Watch / unstable / stale",
         "value": str(statuses.get("WATCH", 0) + statuses.get("UNSTABLE", 0)
                      + statuses.get("STALE", 0)),
         "help": "Includes stale telemetry, which is never forecast."},
        {"label": "Insufficient / inactive",
         "value": str(statuses.get("INSUFFICIENT", 0) + statuses.get("INACTIVE", 0))},
    ])
    styled_table(
        forecast,
        height=360,
        sort_label="risk, then ETA",
        column_config={
            "CURRENT_PRESSURE_INDEX": st.column_config.NumberColumn(
                "Current pressure", format="%.2f",
                help="max(7d median queue-min / 30, remote-spill-GB / 1).",
            ),
            "SLOPE_PER_DAY": st.column_config.NumberColumn("Trend / day", format="%.4f"),
            "R2": st.column_config.NumberColumn("Fit R2", format="%.2f"),
            "BACKTEST_MAE": st.column_config.NumberColumn("Holdout MAE", format="%.2f"),
            "WORKLOAD_GROWTH_PCT": st.column_config.NumberColumn(
                "Demand growth %", format="%.1f%%"),
            "COVERAGE_PCT": st.column_config.NumberColumn("Coverage %", format="%.1f%%"),
        },
    )
    st.caption(
        "No ETA is intentional for STABLE, WATCH, UNSTABLE, STALE, INACTIVE, or INSUFFICIENT rows. "
        "The interval is model uncertainty, not a capacity promise; validate against workload plans."
    )
    result_caption(result)


def _optimization_tab(company: str, days: int, rate: float, settings: dict, is_operator: bool) -> None:
    """Optimization insights: idle/right-sizing advisors, expensive queries and
    patterns, the object-cost ledger, efficiency/storage/clustering scans, and
    guarded remediation."""
    opt_section = lazy_sections(["Idle & sizing", "Queries & patterns", "Storage & waste", "Remediation & ledger"], key="opt_section", deep_link=False)

    if opt_section == "Idle & sizing":
        # rec#16: collect each advisor's OPEN recoverable-$ estimate here, then roll
        # them into one de-duplicated headline at the end of the section.
        _savings_opps: list[SavingsOpportunity] = []
        # rec#20: idle-tail $ and size class per warehouse, for the consolidation scan.
        _idle_by_wh: dict[str, float] = {}
        _size_by_wh: dict[str, str] = {}
        # ---- Idle warehouse advisor ----------------------------------------------
        st.markdown("**Idle warehouse advisor**")
        st.caption("Credits billed in warehouse-hours with zero queries — the auto-suspend opportunity.")
        idle_res = run_mart_first(
            mart27_sql.eff_idle_analysis(days, company),
            insights_sql.idle_warehouse_analysis(days, company),
            page=_PAGE, key=f"idle_{company}_{days}", days=days,
            mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart, loaded hourly)",
            live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")
        if guard(idle_res, "No warehouse metering in this window."):
            # Current settings are part of recommendation eligibility, not decoration:
            # measured idle alone cannot prove that a timer change is still available.
            _whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                       tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
            _idle_df = with_auto_suspend_settings(
                idle_res.df, _whs.df if _whs.ok and not _whs.empty else pd.DataFrame()
            )
            # C1: the mart serves up to 365d but the live fallback clamps to 90d —
            # divide by what was served, or the x30 projection reads ~4x low.
            idle_days = served_days(idle_res, days)
            advisor = idle_advisor(_idle_df, rate, idle_days)
            flagged = advisor[advisor["FLAGGED"]]
            actionable = advisor[advisor["ACTIONABLE"]]
            total_idle = float(advisor["IDLE_USD"].sum())
            _savings_opps.extend(     # rec#16: idle-timer opportunities (net actionable)
                SavingsOpportunity("IDLE", str(r["WAREHOUSE_NAME"]),
                                   safe_float(r["ACTIONABLE_MONTHLY_USD"]),
                                   confidence_weight(r.get("SAVINGS_CONFIDENCE")))
                for _, r in advisor.iterrows()
                if safe_float(r["ACTIONABLE_MONTHLY_USD"]) > 0)
            # rec#20: idle-tail $ per warehouse (for the consolidation saving estimate)
            _idle_by_wh = {str(r["WAREHOUSE_NAME"]).strip().upper():
                           safe_float(r["PROJECTED_MONTHLY_IDLE_USD"])
                           for _, r in advisor.iterrows()}
            if _whs.ok and not _whs.empty:
                _sw = _whs.df.copy()
                _sw.columns = [str(c).lower() for c in _sw.columns]
                if {"name", "size"}.issubset(_sw.columns):
                    _size_by_wh = {str(n).strip().upper(): str(s).upper()
                                   for n, s in zip(_sw["name"], _sw["size"], strict=False)}
            kpi_row([
                {"label": f"Idle spend ({idle_days}d)", "value": format_usd(total_idle),
                 "help": "Credits billed while no query ran on the warehouse."},
                {"label": "Projected monthly idle",
                 "value": format_usd(float(advisor["PROJECTED_MONTHLY_IDLE_USD"].sum())),
                 "help": "All idle, monthly-ized — the gross number, not the recoverable one."},
                {"label": "Actionable via timer",
                 "value": format_usd(float(advisor["ACTIONABLE_MONTHLY_USD"].sum())),
                 "help": f"Settings-verified targets only, net of the unavoidable "
                         f"~{IDLE_TARGET_SUSPEND_SEC}s resume/suspend tail per active metered hour."},
                {"label": "Actionable warehouses", "value": f"{len(actionable)}",
                 "help": f"{len(flagged)} met the idle gate; already-tuned and unknown-setting rows "
                         "remain visible but are not counted as timer opportunities."},
            ])
            idle_sel = selectable_table(
                advisor[["WAREHOUSE_NAME", "RECOMMENDATION", "ACTIONABLE_MONTHLY_USD",
                          "SAVINGS_CONFIDENCE", "ACTION_STATUS"]],
                key="idle_advisor_sel",
                column_config={
                    "ACTIONABLE_MONTHLY_USD": st.column_config.NumberColumn("Actionable $/mo", format="$%.0f"),
                },
                sort_label="actionable monthly dollars, then confidence",
            )
            if idle_sel is None:
                st.caption("Select a warehouse to inspect measured idle, timer state, and gross projections.")
            else:
                st.markdown("**Selected warehouse evidence**")
                styled_table(
                    advisor.iloc[[int(idle_sel)]][[
                        "WAREHOUSE_NAME", "COMPANY", "TOTAL_CREDITS", "IDLE_CREDITS",
                        "IDLE_PCT", "IDLE_USD", "PROJECTED_MONTHLY_IDLE_USD",
                        "AUTO_SUSPEND", "ACTION_STATUS", "ACTIONABLE_MONTHLY_USD",
                        "SAVINGS_CONFIDENCE", "RECOMMENDATION",
                    ]],
                    size_note=False,
                    column_config={
                        "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.1f%%"),
                        "IDLE_USD": st.column_config.NumberColumn("Idle $", format="$%.0f"),
                        "PROJECTED_MONTHLY_IDLE_USD": st.column_config.NumberColumn(
                            "Proj. monthly $", format="$%.0f"),
                        "AUTO_SUSPEND": st.column_config.NumberColumn(
                            "Auto-suspend (s)", format="%.0f"),
                        "ACTIONABLE_MONTHLY_USD": st.column_config.NumberColumn(
                            "Actionable $/mo", format="$%.0f"),
                    },
                )
            result_caption(idle_res, note="Hour-slice granularity; short auto-suspends already reduce this.")
            st.caption(
                f"Actionable $ = idle minus one ~{IDLE_TARGET_SUSPEND_SEC}s suspend/resume tail per active "
                "metered hour, and only where SHOW WAREHOUSES confirms the current timer is above the "
                "target or disabled. Unknown and already-tuned settings are deliberately excluded."
            )
            if not actionable.empty:
                with st.expander("Generated remediation + savings-ledger entries"):
                    statements = []
                    for _, r in actionable.head(10).iterrows():
                        # A3: never generate an ALTER that RAISES a tuned warehouse's
                        # suspend — clamp the target to min(current, 60s) and skip
                        # warehouses already at/below target (their idle is resume overhead).
                        _cur = safe_float(r.get("AUTO_SUSPEND"), 0.0)
                        if 0 < _cur <= IDLE_TARGET_SUSPEND_SEC:
                            continue
                        _target = int(min(_cur, IDLE_TARGET_SUSPEND_SEC)) if _cur > 0 else IDLE_TARGET_SUSPEND_SEC
                        statements.append(idle_suspend_sql(r["WAREHOUSE_NAME"], _target))
                        # B3: book the RECOVERABLE figure, and say in the DESCRIPTION
                        # what was deducted — the ledger is the number people quote.
                        statements.append(
                            f"INSERT INTO {core_object('SAVINGS_LEDGER')} (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL)\n"
                            f"VALUES ({sql_literal('Auto-suspend tune: ' + str(r['WAREHOUSE_NAME']) + ' (idle net of ~' + str(IDLE_TARGET_SUSPEND_SEC) + 's resume tail per active hour)')}, 'ESTIMATED', "
                            f"{sql_number(r['ACTIONABLE_MONTHLY_USD'])}, "
                            f"{sql_literal('Re-run idle analysis for ' + str(r['WAREHOUSE_NAME']) + ' after the change; verify idle $ drop.')});"
                        )
                    st.code("\n".join(statements), language="sql")
                    st.caption("Review and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS. Warehouse changes are "
                               "never executed from the app. Booked amounts are the recoverable idle "
                               "(resume tail deducted), not the gross idle.")
            ai_evaluation_panel(
                key=f"idle_{company}_{days}",
                prompt=idle_warehouse_prompt(advisor, company, idle_days),
                settings=settings,
                page=_PAGE,
                subject="evaluate idle warehouse spend",
            )

        st.divider()
        # ---- Right-sizing simulator ------------------------------------------------
        st.markdown("**Warehouse right-sizing simulator**")
        st.caption(
            "Mechanical scenario model: one Snowflake size step halves or doubles the credit rate. "
            "Runtime effects depend on the workload — the rationale says why; you decide."
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
                        page=_PAGE, key=f"sizing_{company}_{days}", days=days,
                        mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart — p95 is peak daily)",
                        live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")),
                   "No warehouse activity to profile in this window."):
            # C1: same divisor rule — every per-day rate and the x30 monthly figure
            # inside size_recommendations divides by the window actually served.
            sizing_days = served_days(prof_res, days)
            _sizing_whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                              tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
            _sizing_df = with_auto_suspend_settings(
                prof_res.df,
                _sizing_whs.df if _sizing_whs.ok and not _sizing_whs.empty else pd.DataFrame(),
            )
            sized = size_recommendations(_sizing_df, rate, sizing_days)
            _savings_opps.extend(     # rec#16: right-sizing opportunities (overlaps idle per warehouse)
                SavingsOpportunity("RESIZE", str(r["WAREHOUSE_NAME"]),
                                   safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")),
                                   confidence_weight(r.get("CONFIDENCE")))
                for _, r in sized.iterrows()
                if safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")) > 0)
            summary = sizing_summary(sized)
            _sz_cols = ["WAREHOUSE_NAME", "COMPANY", "RECOMMENDATION", "RATIONALE",
                        "CONFIDENCE", "AUTO_SUSPEND", "ACTIVE_QUERY_DAYS", "ACTIVE_DAYS_PER_30D",
                        "MONTHLY_USD_NOW", "IDLE_MONTHLY_USD", "SCENARIO_DOWN_USD", "SCENARIO_UP_USD",
                        "QUEUED_MIN_PER_DAY", "SPILL_GB_PER_DAY", "P95_ELAPSED_SEC", "IDLE_PCT"]
            if "PROVISION_MIN_PER_DAY" in sized.columns:
                _sz_cols.insert(_sz_cols.index("SPILL_GB_PER_DAY"), "PROVISION_MIN_PER_DAY")
            kpi_row([
                {"label": "Size up / add cluster", "value": f"{summary['up']}",
                 "delta_color": "inverse" if summary["up"] else "off",
                 "help": "Sustained overload queueing or remote spill, both PER DAY. Provisioning "
                         "(resume) time is excluded — that is a suspend-timer signal, not concurrency."},
                {"label": "Tune auto-suspend first", "value": f"{summary['suspend']}"},
                {"label": "Idle spend on those", "value": format_usd(summary["idle_saving_usd"]),
                 "help": "Measured idle share of today's bill on the auto-suspend rows — reversible, "
                         "unlike the size-down bet."},
                {"label": "Size-down candidates", "value": f"{summary['down']}"},
                {"label": "Potential saving (down)",
                 "value": f"{format_usd(summary['potential_saving_usd'])}–{format_usd(summary['potential_saving_high_usd'])}",
                 "help": "rec #13: a RANGE, not a point. Low = half the measured IDLE (only idle "
                         "reliably shrinks when the per-hour rate halves); high = half the whole "
                         "bill (if busy credits halve too — optimistic, since a compute-bound query "
                         "on a smaller warehouse runs ~2x longer). Model, not a promise; the idle "
                         "number to its left is measured."},
                {"label": "Evidence / cadence review",
                 "value": f"{summary['observe'] + summary['review']}",
                 "help": "Resize advice is withheld for episodic evidence, unknown timers, and "
                         "high idle that remains after an already-short timer."},
            ])
            _sz_primary = [
                "WAREHOUSE_NAME", "RECOMMENDATION", "RATIONALE", "CONFIDENCE",
                "MONTHLY_USD_NOW", "IDLE_MONTHLY_USD",
            ]
            sel_sz = selectable_table(
                sized[[c for c in _sz_primary if c in sized.columns]],
                key="sizing_sel",
                column_config={
                    "MONTHLY_USD_NOW": st.column_config.NumberColumn("Now $/mo", format="$%.0f"),
                    "IDLE_MONTHLY_USD": st.column_config.NumberColumn("of which idle $/mo", format="$%.0f"),
                },
                sort_label="recommendation priority, then monthly cost",
            )
            if sel_sz is None:
                st.caption("Select a recommendation to load queue, spill, P95, timer, and scenario evidence.")
            else:
                srow = sized.iloc[int(sel_sz)]
                st.markdown("**Selected recommendation evidence**")
                styled_table(
                    sized.iloc[[int(sel_sz)]][[c for c in _sz_cols if c in sized.columns]],
                    size_note=False,
                    column_config={
                        "MONTHLY_USD_NOW": st.column_config.NumberColumn("Now $/mo", format="$%.0f"),
                        "IDLE_MONTHLY_USD": st.column_config.NumberColumn(
                            "of which idle $/mo", format="$%.0f"),
                        "SCENARIO_DOWN_USD": st.column_config.NumberColumn(
                            "x0.5 $/mo", format="$%.0f"),
                        "SCENARIO_UP_USD": st.column_config.NumberColumn(
                            "x2 $/mo", format="$%.0f"),
                        "AUTO_SUSPEND": st.column_config.NumberColumn(
                            "Auto-suspend (s)", format="%.0f"),
                        "SPILL_GB_PER_DAY": st.column_config.NumberColumn(
                            "Spill GB/day", format="%.2f"),
                        "PROVISION_MIN_PER_DAY": st.column_config.Column("Provision per day"),
                        "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.0f%%"),
                    },
                )
            if sel_sz is not None and is_operator:
                srow = sized.iloc[int(sel_sz)]
                if not bool(srow.get("ACTIONABLE", False)):
                    st.warning(
                        "This row is not an evidence-backed resize recommendation. The SQL remains "
                        "available for an intentional operator override, but no saving is booked."
                    )
                target_size = st.selectbox("Resize to", ["XSMALL", "SMALL", "MEDIUM", "LARGE"],
                                           key="sizing_to")
                stmt_sz = remediation.resize_fix(str(srow["WAREHOUSE_NAME"]), target_size)
                st.code(stmt_sz, language="sql")
                est_sz = 0.0
                # sizing.RECOMMEND_DOWN == "Size down candidate" -> "SIZE DOWN ...";
                # the old startswith("DOWN") never matched, so the resize saving was
                # dead code and every downsize booked $0.
                if str(srow.get("RECOMMENDATION", "")).upper().startswith("SIZE DOWN"):
                    est_sz = round(max(0.0, safe_float(srow.get("MONTHLY_USD_NOW"))
                                        - safe_float(srow.get("SCENARIO_DOWN_USD"))), 2)
                    st.caption(f"Half-rate scenario saving ~${est_sz:,.0f}/mo (ESTIMATED until you verify it "
                               "on the Savings ledger below).")
                blast_radius(str(srow["WAREHOUSE_NAME"]), _PAGE)
                from app.logic import remediation as _remediation
                st.caption(_remediation.reverse_hint("RESIZE", str(srow["WAREHOUSE_NAME"])))
                if confirm_gate(str(srow["WAREHOUSE_NAME"]), "Execute resize + log", key="sizing",
                                prompt="Type the warehouse name to confirm resize", object_name=True):
                    ok, msg = execute_statement(stmt_sz, page=_PAGE)
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE, EXECUTED_BY) "
                        f"SELECT 'RESIZE', {sql_literal(str(srow['WAREHOUSE_NAME']))}, {sql_literal(stmt_sz)}, "
                        f"{sql_number(est_sz)}, {sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}",
                        page=_PAGE)
                    if ok:
                        from app.ui.components import log_ui_event
                        log_ui_event("remediation_exec", page=_PAGE)
                    if ok and est_sz > 0:
                        execute_statement(
                            f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                            "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, FINDING_TYPE, TARGET_OBJECT) "
                            f"SELECT {sql_literal('Resize ' + str(srow['WAREHOUSE_NAME']) + ' to ' + target_size)}, "
                            f"'ESTIMATED', {sql_number(est_sz)}, {sql_literal(stmt_sz)}, "
                            "'Booked from sizing simulator; verify with a proof run on the Savings ledger.', "
                            f"'RESIZE', {sql_literal(str(srow['WAREHOUSE_NAME']))}", page=_PAGE)
                    notify(ok, msg)
            _whatif_panel(sized, sizing_days, rate)
            result_caption(prof_res)

        st.divider()
        _capacity_forecast_panel(company)

        st.divider()
        # rec#6: serverless ROI — Query Acceleration. The app tracked QAS SPEND but
        # never the benefit side; QUERY_ACCELERATION_ELIGIBLE names the eligible
        # workload, so a warehouse paying for QAS that rarely helps (or missing an
        # acceleration opportunity) finally surfaces.
        st.markdown("**Serverless ROI — Query Acceleration (QAS)**")
        st.caption(
            "QAS credits spent vs the queries ELIGIBLE for acceleration (Snowflake's own "
            "eligibility signal). Eligibility is utilization, not a dollarized saving. "
            "Search-opt / MV-refresh / auto-clustering credits are tracked under Storage & waste "
            "and the object ledger; their true benefit needs query-level correlation (queued)."
        )
        if st.toggle("Run QAS ROI scan", key="opt_qas_roi_toggle",
                     help="Reads QUERY_ACCELERATION_ELIGIBLE + QUERY_ACCELERATION_HISTORY."):
            qas = run(cost_sql.qas_roi(days, company), page=_PAGE,
                      key=f"qas_roi_{company}_{days}", tier="historical",
                      source="QUERY_ACCELERATION_ELIGIBLE x QUERY_ACCELERATION_HISTORY (QAS ROI)")
            if qas.ok and qas.empty:
                empty_state("clean", "No Query Acceleration spend or eligible workload in this window.")
            elif guard(qas, ""):
                qdf = qas.df.copy()
                qdf["QAS_USD"] = qdf["QAS_CREDITS"].map(lambda c: round(safe_float(c) * rate, 2))
                verdicts = [classify_qas_roi(u, e)
                            for u, e in zip(qdf["QAS_USD"], qdf["ELIGIBLE_QUERIES"], strict=False)]
                qdf["VERDICT"] = [v.verdict for v in verdicts]
                _drop = sum(1 for v in verdicts if v.action == "drop")
                _enable = sum(1 for v in verdicts if v.action == "enable")
                kpi_row([
                    {"label": f"QAS spend ({days}d)", "value": format_usd(float(qdf["QAS_USD"].sum()))},
                    {"label": "Drop candidates", "value": str(_drop),
                     "severity": "warn" if _drop else "",
                     "help": "Paying for QAS with little eligible workload — acceleration rarely helps."},
                    {"label": "Enable candidates", "value": str(_enable),
                     "help": "Eligible acceleration workload with QAS off — a possible speedup."},
                ])
                _qcols = [c for c in ["WAREHOUSE_NAME", "QAS_USD", "ELIGIBLE_QUERIES", "ELIGIBLE_SEC",
                                      "MAX_SCALE_FACTOR", "VERDICT"] if c in qdf.columns]
                styled_table(
                    qdf[_qcols], height=320, sort_label="QAS $ desc",
                    column_config={"QAS_USD": st.column_config.NumberColumn("QAS $", format="$%.2f")})
                result_caption(qas, note="eligibility is utilization, not a dollarized compute saving")

        st.divider()
        # rec#16: one de-duplicated headline across the advisors. Idle-tune and
        # size-down on the SAME warehouse recover the same idle credits, so summing
        # them naively double-counts; rollup_savings keeps the larger of the pair.
        st.markdown("**Total addressable savings (de-duplicated)**")
        _roll = rollup_savings(_savings_opps)
        if _roll.items:
            kpi_row([
                {"label": "Addressable $/mo (net)", "value": format_usd(_roll.total_monthly_usd),
                 "help": "Idle-timer + right-sizing opportunities, de-duplicated so a warehouse "
                         "counted for BOTH idle and resize is not double-counted (the larger wins)."},
                {"label": "Opportunities", "value": str(len(_roll.items))},
                {"label": "Overlaps removed", "value": str(len(_roll.dropped)),
                 "help": "Idle/resize double-counts on the same warehouse dropped from the total."},
            ])
            _rdf = pd.DataFrame([
                {"Source": o.source, "Warehouse / target": o.target,
                 "$/mo": round(o.monthly_usd, 2), "Confidence": round(o.confidence, 2)}
                for o in _roll.items])
            styled_table(_rdf, height=280, sort_label="confidence x dollars",
                         column_config={"$/mo": st.column_config.NumberColumn(format="$%.2f")})
            st.caption(
                "Ranked by confidence x dollars. The failed-query **Wasted spend** board "
                "(Operations) and the **Serverless ROI** panel above are additional levers not yet "
                "folded into this total; storage / clustering plug into the same rollup next."
            )
        else:
            st.caption("No open idle or right-sizing opportunities to roll up in this window.")

        st.divider()
        # rec#20: fleet consolidation — same-size warehouses in this scope whose active
        # hours barely overlap can plausibly share one warehouse, retiring the mostly-
        # idle one. Review-only: it names the pair and a conservative saving, proposes
        # nothing. (Multi-cluster scale-in — lowering MAX_CLUSTER_COUNT on rarely-
        # saturated warehouses — is the other half of this rec and stays queued.)
        st.markdown("**Fleet consolidation candidates (review-only)**")
        st.caption(
            "Same size class, current company scope, active hours that barely overlap → the two "
            "workloads plausibly fit on one warehouse. Estimated saving is the retired warehouse's "
            "monthly idle tail (conservative). Verify concurrency and ownership before merging."
        )
        if st.toggle("Run consolidation scan (hour-of-day activity)", key="opt_consolidation_toggle",
                     help="Reads WAREHOUSE_METERING_HISTORY x FACT_QUERY_HOURLY hour-of-day activity."):
            act = run(insights_sql.warehouse_hourly_activity(days, company), page=_PAGE,
                      key=f"wh_hourly_consol_{company}_{days}", tier="historical",
                      source="WAREHOUSE_METERING_HISTORY x FACT_QUERY_HOURLY (hour-of-day activity)")
            if guard(act, "No hour-of-day warehouse activity in this window."):
                adf = act.df.copy()
                profiles = []
                for wh, group in adf.groupby("WAREHOUSE_NAME"):
                    key = str(wh).strip().upper()
                    hours = frozenset(
                        int(h) for h, qc in zip(group["HOUR_OF_DAY"], group["AVG_QUERIES"], strict=False)
                        if safe_float(qc) >= 1.0)
                    if hours and _size_by_wh.get(key):   # need a known size class to merge safely
                        profiles.append(WarehouseProfile(
                            name=str(wh), size_class=_size_by_wh[key], owner=str(company),
                            active_hours=hours, monthly_idle_usd=_idle_by_wh.get(key, 0.0)))
                cands = consolidation_candidates(profiles)
                if cands:
                    kpi_row([
                        {"label": "Consolidation candidates", "value": str(len(cands))},
                        {"label": "Est. addressable $/mo",
                         "value": format_usd(sum(c.est_monthly_saving_usd for c in cands)),
                         "help": "Sum of the retired warehouses' idle tails (conservative)."},
                    ])
                    cdf = pd.DataFrame([
                        {"Keep": c.keep, "Retire": c.retire, "Size": c.size_class,
                         "Shared active hours": c.shared_hours, "Est. $/mo": c.est_monthly_saving_usd}
                        for c in cands])
                    styled_table(cdf, height=280, sort_label="estimated saving",
                                 column_config={"Est. $/mo": st.column_config.NumberColumn(format="$%.2f")})
                else:
                    st.caption("No same-size warehouses with non-overlapping activity in this scope.")

    elif opt_section == "Queries & patterns":
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
                _eq, _eq_cfg = snowsight_profile_column(edf_q, _PAGE)
                _eq = with_user_names(_eq, _PAGE)
                _eq_cols = ["ALLOCATED_USD", "USER", "USER_NAME", "WAREHOUSE_NAME", "QUERY_TYPE",
                            "EXECUTION_STATUS", "ELAPSED_SEC", "START_TIME", "QUERY_SNIPPET", "QUERY_ID"]
                if "PROFILE" in _eq.columns:
                    _eq_cols.append("PROFILE")
                styled_table(
                    _eq[_eq_cols],
                    column_config={"ALLOCATED_USD": st.column_config.NumberColumn("Allocated $", format="$%.2f"),
                                   **_eq_cfg},
                )
                result_caption(expq, note="allocated by execution-second share within each warehouse-hour")
                st.caption("Chase the top rows in Operations → Queries (query drill-through) by QUERY_ID.")

        # rec#41: the exact complement of the allocated estimate above, offered right
        # here instead of siloed on the Unit-costs tab — QUERY_ATTRIBUTION_HISTORY bills
        # each query its OWN attributed credits (idle warehouse time excluded), so on an
        # idle-heavy warehouse the allocated panel above distorts and this one does not.
        st.markdown("**Most expensive queries (measured $, exact attribution)**")
        st.caption(
            "Measured, not allocated: exact QUERY_ATTRIBUTION_HISTORY credits for running "
            "each query, with warehouse idle time excluded (the allocated panel spreads the "
            "whole warehouse-hour bill, idle included). ~8h attribution lag; queries with no "
            "attributed credits are omitted, so the measured total reads below the allocated one."
        )
        if st.toggle("Run measured-cost scan (exact QAH attribution)", key="cost_measq_toggle",
                     help="Bills each query its own attributed compute + QAS credits — the "
                          "measured truth behind the hour-share estimate above."):
            with st.spinner("Reading measured query attribution…"):
                measq = run(insights_sql.measured_query_costs(
                                days, company,
                                database=st.session_state.get("flt_database", ""),
                                schema_contains=st.session_state.get("flt_schema_contains", ""),
                                warehouse_contains=st.session_state.get("flt_warehouse_contains", ""),
                                limit=50),
                            page=_PAGE, key=f"measq_{company}_{days}", tier="historical",
                            source="QUERY_ATTRIBUTION_HISTORY x QUERY_HISTORY (measured, idle excluded)")
            if guard(measq, "No measured query attribution in this window (~8h view lag)."):
                mdf_q = measq.df.copy()
                mdf_q["MEASURED_USD"] = mdf_q["CREDITS"].map(lambda c: round(safe_float(c) * rate, 2))
                kpi_row([
                    {"label": "Top-50 measured spend", "value": format_usd(float(mdf_q["MEASURED_USD"].sum())),
                     "help": "Exact per-query attributed credits (idle excluded), so this reads "
                             "below the allocated Top-50 — the allocated lens carries idle time."},
                    {"label": "Costliest single query",
                     "value": format_usd(float(mdf_q["MEASURED_USD"].max())) if len(mdf_q) else "$0"},
                ])
                _mq, _mq_cfg = snowsight_profile_column(mdf_q, _PAGE)
                _mq = with_user_names(_mq, _PAGE)
                _mq_cols = [c for c in ["MEASURED_USD", "USER", "USER_NAME", "WAREHOUSE_NAME",
                                        "DATABASE_NAME", "QUERY_TYPE", "EXECUTION_STATUS",
                                        "ELAPSED_SEC", "START_TIME", "QUERY_SNIPPET", "QUERY_ID"]
                            if c in _mq.columns]
                if "PROFILE" in _mq.columns and "PROFILE" not in _mq_cols:
                    _mq_cols.append("PROFILE")
                styled_table(
                    _mq[_mq_cols],
                    column_config={"MEASURED_USD": st.column_config.NumberColumn("Measured $", format="$%.2f"),
                                   **_mq_cfg},
                )
                result_caption(measq, note="exact QUERY_ATTRIBUTION_HISTORY credits, warehouse idle excluded")

        st.markdown("**Recurring cost patterns (same query, run all day)**")
        # $-escape: the $9/$300 pair would render " query run 400x outranks one " as math
        st.caption(md_dollars("Grouped by parameterized fingerprint: a $9 query run 400x outranks "
                              "one $300 outlier — this is where caching/materialization actually pays."))
        st.caption(toggle_cost_hint("exppat_"))
        # #33: this panel groups by parameterized fingerprint across warehouses/users
        # and carries no database/schema grain, so it cannot narrow to the global
        # Database/Schema filter — labelled company-wide when a scope is active so it
        # never silently reads as db-scoped like the expensive-queries panel above.
        if str(st.session_state.get("flt_database", "")).strip() or \
           str(st.session_state.get("flt_schema_contains", "")).strip():
            st.caption("Company-wide: this fingerprint rollup is not narrowed by the active "
                       "Database/Schema filter (it has no object grain).")
        if st.toggle("Run recurring-pattern scan (hour-share allocation by fingerprint)",
                     key="cost_exppat_toggle",
                     help="Its own scan, independent of the expensive-query one above — "
                          "it was hidden inside that toggle before v4.49."):
            pats = run(insights_sql.expensive_patterns_usd(days, company, 30), page=_PAGE,
                       key=f"exppat_{company}_{days}", tier="historical",
                       source="QUERY_HISTORY x METERING (hour-share, by QUERY_PARAMETERIZED_HASH)")
            if guard(pats, "No fingerprint with 5+ runs carrying allocated credits in this window."):
                # E2: expensive_patterns_usd is a plain live builder, so the window it
                # scanned is the CLAMPED one — the labels must say what was measured.
                pat_days = bounded_days(days)
                pdf_c = pats.df.copy()
                pdf_c["USD_TOTAL"] = pdf_c["ALLOCATED_CREDITS"].map(lambda c: round(safe_float(c) * rate, 2))
                pdf_c["USD_PER_DAY"] = pdf_c["CREDITS_PER_DAY"].map(lambda c: round(safe_float(c) * rate, 2))
                styled_table(
                    pdf_c[["USD_PER_DAY", "USD_TOTAL", "RUNS", "USERS", "WAREHOUSES",
                           "QUERY_SNIPPET", "PATTERN_HASH"]],
                    column_config={
                        "USD_PER_DAY": st.column_config.NumberColumn("$/day", format="$%.2f"),
                        "USD_TOTAL": st.column_config.NumberColumn(f"$ ({pat_days}d)", format="$%.2f"),
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
                     "help": f"{int(prow['RUNS'])} runs in {pat_days}d, hour-share allocated."},
                    {"label": f"At {'+' if delta_pr > 0 else ''}{delta_pr} size step",
                     "value": f"${bounds['per_run_low_usd']:.4f} – ${bounds['per_run_high_usd']:.4f}",
                     "help": "Bounds: rate-scaled (same wall time) vs cost-neutral "
                             "(perfect runtime scaling) — same assumptions as the what-if."},
                ])
                st.caption(f"Sample: {str(prow['QUERY_SNIPPET'])[:120]}")

        st.divider()
        # ---- Repeat-query candidates ----------------------------------------------
        st.markdown("**Repeat-query candidates (cache / materialization)**")
        st.caption(toggle_cost_hint("repeatq"))
        _rq_on = st.toggle("Run repeat-query scan (fingerprints the window's QUERY_HISTORY)",
                           key="cost_repeatq_toggle",
                           help="The heaviest scan on this page — runs only when you ask.")
        if _rq_on:
            with st.spinner("Fingerprinting the window's QUERY_HISTORY…"):
                _rq_mart_min = repeat_min_runs(bounded_days(days, 400))
                _rq_live_min = repeat_min_runs(bounded_days(days))
                rq_res = run_mart_first(
                    mart27_sql.family_repeat_fingerprints(
                        days, company, _rq_mart_min,
                        database=st.session_state.get("flt_database", ""),
                        schema_contains=st.session_state.get("flt_schema_contains", "")),
                    insights_sql.repeat_query_fingerprints(
                        days, company, _rq_live_min,
                        database=st.session_state.get("flt_database", ""),
                        schema_contains=st.session_state.get("flt_schema_contains", "")),
                    page=_PAGE, key=f"repeatq_{company}_{days}", days=days,
                    mart_source="MART_QUERY_FAMILY_DAILY (mart — wall-clock elapsed, day-level LAST_RUN)",
                    live_source="QUERY_HISTORY (QUERY_PARAMETERIZED_HASH, live fallback)")
            if guard(rq_res, "No query fingerprints meet the normalized recurrence gate.",
                     setup_hint="Needs QUERY_PARAMETERIZED_HASH (standard in current Snowflake accounts)."):
                # C1/D3: window-relative gate — the same fingerprint must not become
                # a "candidate" merely because the picker moved from 7d to 365d.
                rq_days = served_days(rq_res, days)
                candidates = flag_repeat_candidates(rq_res.df, rq_days)
                hot = candidates[candidates["CANDIDATE"]]
                kpi_row([
                    {"label": "Repeated fingerprints", "value": f"{len(candidates)}"},
                    {"label": "Materialization candidates", "value": f"{len(hot)}",
                     "help": ">=10 runs and >=0.5h of compute per normalized 30 days, with <=25% cache hit."},
                    {"label": "Compute in repeats",
                     "value": humanize_duration(candidates["TOTAL_ELAPSED_HOURS"].sum(), "h")},
                ])
                # C3: WHY and LAST_RUN were computed on every path and rendered on
                # none — the engine's own recommendation, and the "is this pattern
                # still running?" column, were dead code. Both belong in the table.
                _rq_df = candidates.copy()
                _rq_cols = ["RUNS", "RUNS_PER_30D", "USERS", "TOTAL_ELAPSED_HOURS", "AVG_ELAPSED_SEC",
                            "TOTAL_TB_SCANNED", "AVG_CACHE_PCT"]
                _rq_cfg = {
                    "TOTAL_ELAPSED_HOURS": st.column_config.Column("Total elapsed"),
                    "AVG_CACHE_PCT": st.column_config.NumberColumn("Cache %", format="%.0f%%"),
                    "TOTAL_TB_SCANNED": st.column_config.NumberColumn("TB scanned", format="%.3f"),
                }
                _rq_priced = "EST_CREDITS" in _rq_df.columns
                if _rq_priced:
                    # D3: rank and label by money. Size-weighted execution time is an
                    # ESTIMATE; the family mart has no size grain and shows no $.
                    _rq_df["AVOIDABLE_USD_PER_30D"] = _rq_df["AVOIDABLE_CREDITS_PER_30D"].map(
                        lambda credits: round(safe_float(credits) * rate, 2)
                    )
                    _rq_cols.insert(0, "AVOIDABLE_USD_PER_30D")
                    _rq_cfg["AVOIDABLE_USD_PER_30D"] = st.column_config.NumberColumn(
                        "Avoidable $/30d", format="$%.2f"
                    )
                if "LAST_RUN" in _rq_df.columns:
                    _rq_cols.append("LAST_RUN")
                _rq_cols += ["CANDIDATE", "WHY", "QUERY_PREVIEW"]
                from app.config import PAGES_BY_PROFILE, resolve_role_profile
                from app.core.session import current_role

                _can_entity = "Control Room" in PAGES_BY_PROFILE.get(
                    resolve_role_profile(current_role()), ()
                )
                if _can_entity:
                    def _open_fingerprint(index: int) -> None:
                        fingerprint = str(_rq_df.iloc[int(index)]["FINGERPRINT"])
                        request_navigation(
                            "Control Room", "Entity 360",
                            context={
                                "entity_type": "QUERY_FINGERPRINT",
                                "entity_key": fingerprint,
                            },
                        )

                    selectable_nav_table(
                        _rq_df[_rq_cols], key="repeat_query_profiles",
                        on_select=_open_fingerprint, column_config=_rq_cfg,
                        sort_label="normalized avoidable cost, then recurrence",
                    )
                else:
                    styled_table(_rq_df[_rq_cols], column_config=_rq_cfg)
                result_caption(rq_res, note="Same parameterized query shape grouped across users/warehouses.")
                st.caption(
                    "Ranked by normalized 30-day estimated credits x cache-miss share, not wall-clock hours: an X-Small "
                    "hour and a 4X-Large hour differ 128x in money, and an already-cached family has "
                    "nothing left to reclaim. Avoidable $/30d is execution time x size rate x cache-miss share — an "
                    "estimate, not the hour-share allocation the panels above use."
                    if _rq_priced else
                    "Served from the query-family mart, which carries no warehouse-size grain — the "
                    "ordering here is by compute hours; the live scan adds the credit-weighted ranking."
                )

    elif opt_section == "Storage & waste":
        # ---- Object cost ledger ----------------------------------------------------
        st.markdown("**Object cost ledger (measured + maintenance)**")
        st.caption(
            "Additive per-object credits (V048–V050): measured query compute+QAS split "
            "equally across touched objects, labeled by role — QUERY_COMPUTE_WRITE is "
            "the cost of building the object (production), QUERY_COMPUTE_READ the cost "
            "of reading it (consumption) — plus direct clustering / MV refresh / "
            "serverless task / Snowpipe / search-opt. QUERY_COMPUTE_RESIDUAL = credits "
            "for queries that neither read nor wrote a base object."
        )
        # #19/#33: thread the global Database filter (was silently dropped — the
        # panel header promises db-scoped attribution). The object's db is the first
        # label of OBJECT_FQN; cost_sql scopes on it. Included in the cache key so a
        # db switch re-reads. Schema is not applied here (object-cost is scoped by
        # database, not the free-text schema filter) — the caption says so.
        _oc_db = st.session_state.get("flt_database", "")
        _oc = run(cost_sql.object_cost_by_arm(days, company, database=_oc_db), page=_PAGE,
                  key=f"objcost_arm_{company}_{days}_{_oc_db}", tier="recent",
                  source="FACT_OBJECT_COST_DAILY (object-cost ledger)", probe=True)
        if _oc.ok and not _oc.empty:
            _adf = _oc.df.copy()
            _adf["USD"] = _adf["CREDITS"].map(safe_float) * rate
            kpi_row([{"label": "Object-attributed spend", "value": format_usd(float(_adf["USD"].sum())),
                      "help": "Sum across arms x the configured rate. Additive by construction."}])
            charts.bar_usd(_adf.sort_values("USD", ascending=False), "COST_ARM", "USD",
                           title="$ by cost arm", takeaway=True)
            _top = run(cost_sql.object_cost_top(days, company, 25, database=_oc_db), page=_PAGE,
                       key=f"objcost_top_{company}_{days}_{_oc_db}", tier="recent",
                       source="FACT_OBJECT_COST_DAILY (top objects)", probe=True)
            if _top.ok and not _top.empty:
                _tdf = _top.df.copy()
                _tdf["USD"] = _tdf["CREDITS"].map(safe_float) * rate
                _tdf["QUERY_USD"] = _tdf["QUERY_CREDITS"].map(safe_float) * rate
                _tdf["MAINT_USD"] = _tdf["MAINTENANCE_CREDITS"].map(safe_float) * rate
                styled_table(_tdf[["OBJECT_FQN", "OBJECT_DOMAIN", "COMPANY", "QUERY_USD", "MAINT_USD", "USD"]],
                             height=300)
            st.caption(
                ("Scoped to the selected database. " if str(_oc_db).strip() else "")
                + "Measured query compute is split equally across a query's base objects "
                "(additive); full-query 'influenced cost' is a separate non-additive lens.")
        elif _oc.ok:
            st.success("No object-cost rows yet (loads after V048 + the first daily run).")
        else:
            st.info("Object cost arrives with migration V048 (FACT_OBJECT_COST_DAILY) — an admin "
                    "can apply it on Admin → Migrations & freshness.")
        st.divider()
        st.markdown("**Storage growth movers**")
        days_storage = max(days, 30)
        sg_res = run(insights_sql.storage_growth_by_database(days_storage, company), page=_PAGE,
                     key=f"storgrow_{company}_{days_storage}", tier="historical",
                     source="DATABASE_STORAGE_USAGE_HISTORY")
        if guard(sg_res, "No storage history for this scope."):
            movers = storage_movers(sg_res.df, safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0))
            # #33: honor the global Database filter. storage_growth_by_database
            # (outside this cluster) returns every database in the company scope, so
            # narrow to the selected database on movers' own DATABASE_NAME grain —
            # filtering rows keeps every column, so the KPIs/chart stay well-typed.
            _sg_db = str(st.session_state.get("flt_database", "") or "").strip()
            if _sg_db and not movers.empty and "DATABASE_NAME" in movers.columns:
                movers = movers[movers["DATABASE_NAME"].astype(str).str.upper() == _sg_db.upper()]
            if _sg_db and movers.empty:
                st.info("No storage history for the selected database in this window.")
            else:
                # D4: the chart's top-10 cut is by projected DOLLARS — the database adding
                # the most TB is not necessarily the one adding the most money, and this
                # panel's own axis is $/mo.
                growing = movers[
                    (movers["GROWTH_USD_30D"] > 0) & movers["PROJECTABLE"]
                ].sort_values("GROWTH_USD_30D", ascending=False)
                _provisional = int(
                    ((movers["GROWTH_USD_30D"] > 0) & ~movers["PROJECTABLE"]).sum()
                )
                _shrinking = int((movers["GROWTH_USD_30D"] < 0).sum())
                kpi_row([
                    {"label": "Current storage", "value": f"{float(movers['CURRENT_TB'].sum()):,.2f} TB"},
                    {"label": f"Growth ({days_storage}d)", "value": f"{float(movers['GROWTH_TB'].sum()):,.2f} TB"},
                    # E6: gainers-only, and it always was — the label now says so instead
                    # of reading like the account's net storage trend.
                    {"label": "Projected growth $/mo (confident gainers)",
                     "value": format_usd(float(growing["GROWTH_USD_30D"].sum())),
                     "help": f"Growing databases with >=7 observed days only; {_provisional} provisional "
                              f"gainer(s) and {_shrinking} shrinking one(s) are excluded. Estimate."},
                ])
                charts.bar_usd(growing.head(10), "DATABASE_NAME", "GROWTH_USD_30D", title="Projected growth $/mo")
                _mv_cols = ["DATABASE_NAME", "COMPANY", "CURRENT_TB", "GROWTH_TB", "GROWTH_TB_30D",
                            "GROWTH_USD_30D", "FAILSAFE_SHARE_PCT"]
                if "LOW_CONFIDENCE" in movers.columns:
                    _mv_cols.append("LOW_CONFIDENCE")
                styled_table(  # rec21: status tint, prettified headers, CSV
                    movers[_mv_cols],
                    column_config={
                        "GROWTH_USD_30D": st.column_config.NumberColumn("Growth $/mo", format="$%.0f"),
                        "FAILSAFE_SHARE_PCT": st.column_config.NumberColumn("Failsafe %", format="%.1f%%"),
                        "LOW_CONFIDENCE": st.column_config.CheckboxColumn("Low confidence"),
                    },
                )
                result_caption(sg_res, note=(f"Window widened to {days_storage}d for a stable growth slope."
                                             if days < days_storage else f"{days_storage}d window."))
                _low = int(movers["LOW_CONFIDENCE"].sum()) if "LOW_CONFIDENCE" in movers.columns else 0
                st.caption(
                    "The projection is a least-squares slope over every observed day, not first-vs-last "
                    "bytes — one reload on an endpoint day used to become the whole trend."
                    + (f" {_low} database(s) have fewer than 7 observed days and are flagged low "
                       "confidence: treat their $/mo as a placeholder, not a forecast." if _low else "")
                    + " Failsafe % is the failsafe share of TOTAL billed bytes (database + failsafe)."
                )

        st.divider()
        st.markdown("**Query efficiency (pruning + result cache)**")
        st.caption(toggle_cost_hint("prune_"))
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
                # #33: result_cache_daily (outside this cluster) aggregates to day grain
                # with no database/schema column, so unlike the pruning panel above it
                # cannot honor the global filter — labelled company-wide so a scoped
                # screen does not read this line as db-specific.
                st.caption("Share of successful queries answered without scanning (result cache + "
                           "metadata). A falling line means redundant recomputation. "
                           "Company-wide: not narrowed by the active Database/Schema filter.")

        st.markdown("**Storage waste (Time Travel / failsafe / stale tables)**")
        st.caption(toggle_cost_hint("reclaim_"))
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
                # D4: "or dropping" is only defensible when read evidence exists. On
                # the degraded (DML-only) path a table can be read constantly and
                # still look stale — recommending a DROP there is dangerous advice.
                st.caption("Stale + heavy retention = candidates for DATA_RETENTION_TIME_IN_DAYS "
                           + ("reduction, transient conversion, or dropping."
                              if reads_available else
                              "reduction or transient conversion. Read evidence is unavailable "
                              "(ACCESS_HISTORY needs Enterprise edition), so STALE here means no DML "
                              "only — a table nobody writes may still be read every day. These are "
                              "not drop candidates."))
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
                    _tt_gb = safe_float(wrow.get("TIME_TRAVEL_GB"))
                    _fs_gb = safe_float(wrow.get("FAILSAFE_GB"))
                    _ret_known = bool(wrow.get("RETENTION_KNOWN", False))
                    _cur_ret = safe_float(wrow.get("RETENTION_DAYS"), 0.0)
                    _default_ret = int(max(0, min(_cur_ret, 90))) if _ret_known else 0
                    keep_days = st.number_input(
                        "Set retention days", min_value=0, max_value=90,
                        value=_default_ret, step=1, key="waste_days",
                    )
                    _rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
                    _can_reduce = _ret_known and _cur_ret > 0 and int(keep_days) < _cur_ret
                    if not _ret_known:
                        st.warning(
                            "Current DATA_RETENTION_TIME_IN_DAYS is unavailable. No ALTER or savings "
                            "entry is generated until the setting can be verified."
                        )
                    elif not _can_reduce:
                        st.info(
                            f"Choose a value below the verified current {_cur_ret:.0f}d retention "
                            "to generate a reduction. The control defaults to no change."
                        )
                    else:
                        stmt_w = remediation.retention_fix(str(wrow["DATABASE_NAME"]),
                                                           str(wrow["SCHEMA_NAME"]),
                                                           str(wrow["TABLE_NAME"]), int(keep_days))
                        st.code(stmt_w, language="sql")
                        # Only Time Travel bytes released by the shorter window are
                        # recoverable. Failsafe remains a fixed seven-day tail.
                        _freed_gb = _tt_gb * max(0.0, _cur_ret - int(keep_days)) / _cur_ret
                        _basis = (f"{_cur_ret:,.0f}d -> {int(keep_days)}d retention releases about "
                                  f"{_freed_gb:,.0f} of {_tt_gb:,.0f} GB of Time Travel")
                        est_w = round(_freed_gb / 1024 * _rate_tb, 2)
                        st.caption(
                            f"{_basis} (~${est_w:,.2f}/mo, ESTIMATED). Time-Travel bytes also age out on "
                            f"their own as the existing window rolls forward. The {_fs_gb:,.0f} GB of "
                            "failsafe is NOT included: it drains on a fixed 7-day schedule regardless of "
                            "this setting."
                        )
                        if confirm_gate(str(wrow["TABLE_NAME"]), "Execute retention change + log", key="waste",
                                        prompt="Type the table name to confirm", object_name=True):
                            ok, msg = execute_statement(stmt_w, page=_PAGE)
                            execute_statement(
                                f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                                "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE, EXECUTED_BY) "
                                f"SELECT 'RETENTION', {sql_literal('.'.join([str(wrow['DATABASE_NAME']), str(wrow['SCHEMA_NAME']), str(wrow['TABLE_NAME'])]))}, "
                                f"{sql_literal(stmt_w)}, {sql_number(est_w)}, "
                                f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}", page=_PAGE)
                            if ok and est_w > 0:
                                execute_statement(
                                    f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                                    "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, FINDING_TYPE, TARGET_OBJECT) "
                                    f"SELECT {sql_literal('Retention ' + str(wrow['TABLE_NAME']) + ' -> ' + str(int(keep_days)) + 'd')}, "
                                    f"'ESTIMATED', {sql_number(est_w)}, {sql_literal(stmt_w)}, "
                                    "'Booked from storage-waste scan.', "
                                    f"'RETENTION', {sql_literal('.'.join([str(wrow['DATABASE_NAME']), str(wrow['SCHEMA_NAME']), str(wrow['TABLE_NAME'])]))}", page=_PAGE)
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

    elif opt_section == "Remediation & ledger":
        st.markdown("**Guarded remediation (generate → review → execute)**")
        panel_help(
            "Turns findings into exact `ALTER` statements. Execution needs the "
            "admin profile, writes a REMEDIATION_LOG audit row, and books an "
            "ESTIMATED savings-ledger item — verify it on the Savings ledger below. "
            "Warehouse-setting changes are also caught by the change scan, which "
            "books and settles its own measured row (V038). "
            "Anyone can copy the SQL for review."
        )
        # Same builder PAIR as the advisor above (r20 #1): identical SQL identity
        # means this is served from the advisor's cache — the remediation block
        # no longer pays its own live metering x history join when the mart is up.
        idle_res = run_mart_first(
            mart27_sql.eff_idle_analysis(days, company),
            insights_sql.idle_warehouse_analysis(days, company),
            page=_PAGE, key=f"remed_idle_{company}_{days}", days=days,
            mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart, loaded hourly)",
            live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")
        if guard(idle_res, "No warehouse activity in the window to remediate."):
            _remed_whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                             tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
            idf = with_auto_suspend_settings(
                idle_res.df,
                _remed_whs.df if _remed_whs.ok and not _remed_whs.empty else pd.DataFrame(),
            )
            wh_pick = st.selectbox("Warehouse", sorted(idf["WAREHOUSE_NAME"].astype(str)),
                                   key="remed_wh")
            fix_kind = st.radio("Fix", ["Tighten auto-suspend to 60s", "Off-hours suspend/resume schedule"],
                                horizontal=True, key="remed_kind")
            row = idf[idf["WAREHOUSE_NAME"].astype(str) == wh_pick]
            idle_credits = float(pd.to_numeric(row["IDLE_CREDITS"], errors="coerce").fillna(0).iloc[0]) if not row.empty else 0.0
            # C1: divide by the window actually served, not the requested one.
            remed_days = served_days(idle_res, days)
            # Book the advisor's settings-verified action value, never gross idle.
            # A missing SHOW WAREHOUSES row intentionally produces no executable fix.
            _rec_all = idle_advisor(idf, rate, remed_days)
            _rec_row = _rec_all[_rec_all["WAREHOUSE_NAME"].astype(str) == wh_pick]
            est_monthly = (round(float(_rec_row["ACTIONABLE_MONTHLY_USD"].iloc[0]), 2)
                           if not _rec_row.empty else 0.0)

            stmt = ""
            if fix_kind.startswith("Tighten"):
                _known = bool(_rec_row["AUTO_SUSPEND_KNOWN"].iloc[0]) if not _rec_row.empty else False
                _current = safe_float(_rec_row["AUTO_SUSPEND"].iloc[0]) if _known else 0.0
                if not _known:
                    st.warning(
                        "Current AUTO_SUSPEND could not be verified. No executable ALTER or savings "
                        "entry is generated until SHOW WAREHOUSES returns this setting."
                    )
                elif 0 < _current <= IDLE_TARGET_SUSPEND_SEC:
                    st.info(
                        f"{wh_pick} is already at AUTO_SUSPEND={_current:.0f}s. A 60s change would "
                        "raise or preserve the timer, so this engine will not generate it."
                    )
                else:
                    _target = int(min(_current, IDLE_TARGET_SUSPEND_SEC)) if _current > 0 else IDLE_TARGET_SUSPEND_SEC
                    stmt = remediation.auto_suspend_fix(wh_pick, _target)
                    st.caption(f"Idle credits in window ({remed_days}d): {idle_credits:,.1f} → actionable "
                               f"${est_monthly:,.0f}/mo once the unavoidable ~{IDLE_TARGET_SUSPEND_SEC}s resume tail per "
                               "active hour is deducted (ESTIMATED until verified).")
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
                        st.info("No defensible recurring 4h+ window where this warehouse burns credits with "
                                "~no queries. Sparse or all-day idle profiles are routed to auto-suspend, "
                                "consolidation, or retirement instead of an invalid schedule pair.")
                    else:
                        st.caption(
                            f"Highest-value quiet window {proposal['start']:02d}:00–{proposal['end']:02d}:00 "
                            f"({proposal['hours']}h, ~{proposal['avg_credits_per_day']} credits/day burned idle). "
                            f"Weekday schedule below; review before executing — resume-on-demand still works "
                            f"if a job fires early."
                        )
                        stmt = remediation.suspend_schedule(wh_pick, proposal["start"], proposal["end"])
                        # B1: avg_credits_per_day is now per CALENDAR day of the profile
                        # window (insights_sql.warehouse_hourly_activity divides by the
                        # window, not by the days that particular hour happened to
                        # meter). It used to be a per-METERED-day figure monetized x30
                        # calendar days — a ~3.5x overstatement on any warehouse that
                        # burns in that hour only some nights. x5/7 because the CRON
                        # suspends on weekdays only.
                        est_monthly = round(proposal["avg_credits_per_day"] * 30 * rate * 5 / 7, 2)

            if stmt:
                st.code(stmt, language="sql")
                if is_operator:
                    if confirm_gate(wh_pick, "Execute + log + book estimated savings", key="remed",
                                    prompt="Type the warehouse name to confirm execution", object_name=True):
                        ok, msg = execute_statement(stmt, page=_PAGE)
                        log_sql = (
                            f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                            "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD, STATUS, RESULT_NOTE, EXECUTED_BY) "
                            f"SELECT {sql_literal('AUTO_SUSPEND' if fix_kind.startswith('Tighten') else 'SCHEDULE')}, "
                            f"{sql_literal(wh_pick)}, {sql_literal(stmt[:4000])}, {sql_number(est_monthly)}, "
                            f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}"
                        )
                        execute_statement(log_sql, page=_PAGE)
                        if ok and est_monthly > 0:
                            ledger_sql = (
                                f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                                "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, FINDING_TYPE, TARGET_OBJECT) "
                                f"SELECT {sql_literal(f'{fix_kind} on {wh_pick}')}, 'ESTIMATED', "
                                f"{sql_number(est_monthly)}, {sql_literal(stmt[:4000])}, "
                                f"{sql_literal('Booked by guarded remediation; verify with a proof run on the Savings ledger.')}, "
                                # V053: AUTO_SUSPEND rows are what the monthly verifier re-measures (P1-A).
                                f"{sql_literal('AUTO_SUSPEND' if fix_kind.startswith('Tighten') else 'SCHEDULE')}, {sql_literal(wh_pick)}"
                            )
                            execute_statement(ledger_sql, page=_PAGE)
                        notify(ok, msg)
                else:
                    st.caption("Copy the SQL freely; executing from the app requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")

        remlog = run(mart_sql.remediation_log(50), page=_PAGE, key="remed_log", tier="live",
                     source="REMEDIATION_LOG")
        if remlog.ok and not remlog.empty:
            st.markdown("**Remediation history**")
            styled_table(remlog.df, height=200)

def _savings_tab() -> None:
    res = run(mart_sql.savings_ledger(), page=_PAGE, key="savings_ledger",
              tier="live", source="SAVINGS_LEDGER")
    if not res.ok:
        st.info("Savings ledger is not installed yet — an admin can apply the pending schema update on Admin → Migrations & freshness.")
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

    # #3: operator gating from the VIEWER identity + allowlist, not CURRENT_ROLE().
    is_operator = _is_operator()

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
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS — in-app execution needs an admin profile.")

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
                    f"VERIFIED_AT = CURRENT_TIMESTAMP(), VERIFIED_BY = {identity_sql()}\n"
                    # codex#26: guard on STATE so a stale/concurrent page (the row was already
                    # VERIFIED or REJECTED after this page rendered) no-ops instead of
                    # overwriting a settled amount. Verification is now conditional/idempotent.
                    f"WHERE ITEM_ID = {sql_literal(row['ITEM_ID'])} AND STATE = 'ESTIMATED';"
                )
                st.code(update_sql, language="sql")
                if not allowed:
                    st.warning(why)
                elif is_operator and st.button("Execute verification", key="ledger_verify_exec"):
                    ok, msg = execute_statement(update_sql, page=_PAGE)
                    notify(ok, msg)
