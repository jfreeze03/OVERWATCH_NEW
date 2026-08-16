"""Decision Studio: prioritization, objectives, economics and experiment follow-through."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.identity import viewer_name
from app.core.query import execute_statement, run
from app.core.session import is_operator
from app.core.state import request_navigation
from app.data import mart_sql, workbench_sql
from app.logic.decision import prioritize_workloads, scenario_projection, slo_summary
from app.logic.formulas import blended_billed_usd, credits_to_usd, format_usd, safe_float
from app.logic.workbench import (
    EXPERIMENT_STATUSES,
    SLO_METRIC_KEYS,
    create_slo_objective_sql,
    mark_watched,
    mark_watched_pairs,
    update_experiment_sql,
)
from app.ui import charts
from app.ui.components import (
    decision_rows,
    empty_state,
    exception_summary,
    guard,
    kpi_row,
    load_settings,
    notify,
    read_model_caption,
    result_caption,
    selectable_nav_table,
    selectable_table,
    styled_table,
)

_PAGE = "Decision Studio"


def _open_entity(kind: str, key: str) -> None:
    request_navigation(
        "Control Room", "Entity 360",
        context={"entity_type": kind, "entity_key": key},
    )


def _portfolio(company: str, days: int, rate: float) -> None:
    result = run(
        workbench_sql.workload_portfolio(days, company, 200), page=_PAGE,
        key=f"decision_portfolio_{company}_{days}", tier="historical",
        source="MART_PATTERN_COST_DAILY + MART_QUERY_FAMILY_DAILY",
    )
    if not guard(result, "No measured recurring-query cost exists in this scope."):
        return
    portfolio = prioritize_workloads(result.df, rate, days)
    # DS #1: make Watch more than a bookmark — a watched query family gets a WATCHED flag
    # here and is pinned to the top WITHIN its lane (so an ACT NOW item is never buried
    # under a watched PLAN item). Degrades to no-pin if the watchlist read is unavailable.
    _viewer = viewer_name()
    _wl_res = run(workbench_sql.watchlist(_viewer), page=_PAGE, key="decision_watchlist",
                  tier="live", source="USER_WATCHLIST") if _viewer else None
    _wl = _wl_res.df if (_wl_res is not None and _wl_res.usable()) else None
    portfolio["WATCHED"] = mark_watched(portfolio, _wl, "QUERY_FINGERPRINT", "FINGERPRINT")
    if bool(portfolio["WATCHED"].any()):
        _lane_rank = portfolio["LANE"].map({"ACT NOW": 0, "PLAN": 1, "VALIDATE": 2}).fillna(3)
        portfolio = (portfolio.assign(_LR=_lane_rank)
                     .sort_values(["_LR", "WATCHED", "PRIORITY_SCORE"],
                                  ascending=[True, False, False])
                     .drop(columns="_LR").reset_index(drop=True))
    read_model_caption("workload_portfolio")
    act_now = portfolio[portfolio["LANE"].eq("ACT NOW")]
    failure_risk = portfolio[portfolio["FAIL_PCT"].ge(2)]
    validate = portfolio[portfolio["CONFIDENCE"].lt(0.5)]
    exceptions = []
    if not act_now.empty:
        exceptions.append({
            "label": "Act now",
            "value": f"{len(act_now):,}",
            "detail": f"{format_usd(act_now['IMPACT_USD_30D'].sum())} measured 30-day impact.",
            "severity": "warn",
        })
    if not failure_risk.empty:
        exceptions.append({
            "label": "Failure risk",
            "value": f"{len(failure_risk):,}",
            "detail": "Families at or above a 2% observed failure rate.",
            "severity": "bad",
        })
    if not validate.empty:
        exceptions.append({
            "label": "Needs validation",
            "value": f"{len(validate):,}",
            "detail": "Evidence confidence is below the action threshold.",
            "severity": "warn",
        })
    exception_summary(
        exceptions,
        "No immediate-action, elevated-failure, or low-confidence workload families.",
    )
    kpi_row([
        {"label": "Measured families", "value": f"{len(portfolio):,}"},
        {"label": "Act now", "value": f"{portfolio['LANE'].eq('ACT NOW').sum():,}",
         "severity": "warn" if portfolio["LANE"].eq("ACT NOW").any() else "ok"},
        {"label": "30d normalized impact",
         "value": format_usd(portfolio["IMPACT_USD_30D"].sum()),
         "help": "Measured pattern credits normalized to 30 days; observed cost, not promised savings."},
        {"label": "High-confidence", "value": f"{portfolio['CONFIDENCE'].ge(0.8).sum():,}"},
        {"label": "Watching", "value": f"{int(portfolio['WATCHED'].sum()):,}",
         "help": "Query families on your personal watchlist — pinned to the top of their lane. "
                 "Watch or unwatch a family from its Entity 360 (open one below)."},
    ])
    charts.workload_portfolio(portfolio)
    def open_profile(index: int) -> None:
        _open_entity("QUERY_FINGERPRINT", str(portfolio.iloc[int(index)]["FINGERPRINT"]))

    decision_rows(
        portfolio,
        key="decision_portfolio_table",
        decision_col="NEXT_MOVE",
        why_col="QUERY_PREVIEW",
        impact_col="IMPACT_USD_30D",
        confidence_col="CONFIDENCE",
        status_col="LANE",
        context_cols=("WATCHED", "EFFORT_PROXY", "RUNS", "FAIL_PCT", "AVG_CACHE_PCT", "P95_SEC",
                      "EVIDENCE_COVERAGE"),
        on_select=open_profile,
        height=370,
        sort_label="decision lane, then evidence-weighted priority",
        impact_help="Measured pattern credits x the compute rate, normalized to 30 days — "
                    "observed cost, not promised savings.",
        confidence_label="Confidence (evidence)",
        confidence_help="Evidence heuristic (0-1): a blend of run recency, active-day coverage, "
                        "and whether the family has measured cost. NOT statistical confidence.",
    )
    # Trust: name which numbers are measured and which are heuristics, and give the
    # exact lane rule so a reader never mistakes an ordering heuristic for a verdict.
    st.caption(
        "Impact $, runs, fail % and cache are **measured**; confidence, priority and lane are "
        "**evidence-weighted heuristics** for ordering, not guarantees. Lane rule: ACT NOW = "
        "top-20% priority AND confidence ≥ 0.65; VALIDATE = confidence < 0.5; otherwise PLAN. "
        "A family with no measured cache/latency/failure evidence (coverage 0) is held at "
        "VALIDATE and never told to cache — a blank cell is missing data, not a measured zero. "
        "WATCHED families (starred from an Entity 360) are pinned to the top of their lane."
    )
    result_caption(result, note="credits are measured; effort is a users + databases proxy")


def _slo_editor() -> None:
    if not is_operator():
        return
    with st.expander("Create objective"):
        metric = st.selectbox("Metric", SLO_METRIC_KEYS, key="slo_new_metric")
        entity_type = (
            "WAREHOUSE" if metric.startswith("WAREHOUSE_")
            else "TASK" if metric.startswith("TASK_")
            else "QUERY_FINGERPRINT"
        )
        name = st.text_input("Objective", key="slo_new_name", max_chars=300)
        entity_key = st.text_input(
            f"{entity_type.replace('_', ' ').title()} key", key="slo_new_entity",
            max_chars=500,
        )
        success_metric = metric.endswith("SUCCESS_PCT")
        comparator = ">=" if success_metric else "<="
        target = st.number_input(
            "Target", min_value=0.0, value=99.0 if success_metric else 60.0,
            step=0.1, key="slo_new_target",
        )
        error_budget = st.number_input(
            "Error budget %", min_value=0.01, max_value=100.0, value=1.0,
            step=0.1, key="slo_new_budget",
        )
        window = st.select_slider(
            "Window", options=[7, 14, 30, 60, 90], value=30,
            format_func=lambda value: f"{value} days", key="slo_new_window",
        )
        owner = st.text_input("Owner", value="DBA", key="slo_new_owner", max_chars=200)
        notes = st.text_area("Notes", key="slo_new_notes", max_chars=4000)
        if name and entity_key:
            statement = create_slo_objective_sql(
                name=name, entity_type=entity_type, entity_key=entity_key,
                metric_key=metric, comparator=comparator, target_value=target,
                error_budget_pct=error_budget, window_days=window, owner=owner,
                notes=notes, actor=viewer_name(),
            )
            st.code(statement, language="sql")
            if st.button("Create objective", key="slo_new_execute", type="primary"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)
                if ok:
                    st.rerun()


def _slos() -> None:
    result = run(
        workbench_sql.slo_cockpit(), page=_PAGE, key="decision_slos",
        tier="recent", source="SLO_OBJECTIVES + existing metric marts",
    )
    if not result.ok:
        empty_state("needs_setup", "Apply V074 to configure objectives and error budgets.")
        return
    summary = slo_summary(result.df)
    measured_objectives = int(summary["met"] + summary["breach"])
    read_model_caption("slo_cockpit")
    st.caption(
        "P95 objectives evaluate the **worst daily P95** over the window — a day-granular "
        "check (the objective holds only if *every* day stayed under target), not a single "
        "window percentile. STALE = the newest mart day is >2 days old, so the verdict is "
        "withheld rather than read off stale evidence. Error-budget burn applies to "
        "success-rate objectives only; latency/P95 objectives show n/a."
    )
    if not result.empty:
        exceptions = []
        if summary["breach"]:
            exceptions.append({
                "label": "Breached objectives",
                "value": f"{summary['breach']:,.0f}",
                "detail": "Measured value is outside the configured objective.",
                "severity": "bad",
            })
        if summary["no_data"]:
            exceptions.append({
                "label": "Missing evidence",
                "value": f"{summary['no_data']:,.0f}",
                "detail": "The objective cannot be evaluated from its current mart window.",
                "severity": "warn",
            })
        if summary["stale"]:
            exceptions.append({
                "label": "Stale evidence",
                "value": f"{summary['stale']:,.0f}",
                "detail": "Newest mart day is >2 days old — the loader may have stalled; verdict withheld.",
                "severity": "warn",
            })
        if summary["worst_burn"] > 1:
            exceptions.append({
                "label": "Worst error-budget burn",
                "value": f"{summary['worst_burn']:,.2f}x",
                "detail": "Reliability consumption exceeds the configured budget.",
                "severity": "bad",
            })
        exception_summary(exceptions, "Every measured objective is within its configured target.")
    kpi_row([
        {"label": "Objectives", "value": f"{summary['total']:,.0f}"},
        {"label": "Meeting target", "value": f"{summary['met']:,.0f}",
         "severity": "ok" if measured_objectives else ""},
        {"label": "Breached", "value": f"{summary['breach']:,.0f}",
         "severity": "bad" if summary["breach"] else ("ok" if measured_objectives else "")},
        {"label": "No evidence", "value": f"{summary['no_data']:,.0f}",
         "severity": "warn" if summary["no_data"] else ("ok" if summary["total"] else "")},
        {"label": "Stale", "value": f"{summary['stale']:,.0f}",
         "severity": "warn" if summary["stale"] else ("ok" if summary["total"] else ""),
         "help": "Objectives whose newest mart day is >2 days old; the verdict is withheld."},
        {"label": "Worst burn",
         "value": (f"{summary['worst_burn']:,.2f}x" if summary["has_burn"] else "n/a"),
         "severity": ("bad" if summary["worst_burn"] > 1 else "ok") if summary["has_burn"] else "",
         "help": "Error-budget consumption; success-rate objectives only (latency/P95 show n/a)."},
    ])
    if result.empty:
        empty_state("no_data_yet", "No active objectives are configured.")
    else:
        frame = result.df.reset_index(drop=True)

        def open_slo_entity(index: int) -> None:
            row = frame.iloc[int(index)]
            _open_entity(str(row["ENTITY_TYPE"]), str(row["ENTITY_KEY"]))

        selectable_nav_table(
            frame, key="decision_slo_table", on_select=open_slo_entity, height=370,
            sort_label="breach, missing evidence, then error-budget burn",
        )
    _slo_editor()


def _products(company: str, days: int, rate: float) -> None:
    result = run(
        workbench_sql.data_product_economics(days, company), page=_PAGE,
        key=f"decision_products_{company}_{days}", tier="historical",
        source="ENTITY_CATALOG + object, warehouse and task marts",
    )
    if not result.ok:
        empty_state("needs_setup", "Apply V074 and map catalog entities to data products.")
        return
    if result.empty:
        empty_state("no_data_yet", "No catalog entities are mapped to a data product yet.")
        return
    frame = result.df.copy()
    frame["MEASURED_OBJECT_USD"] = frame["MEASURED_OBJECT_CREDITS"].map(
        lambda value: credits_to_usd(value, rate)
    )
    frame["METERED_WAREHOUSE_USD"] = frame["METERED_WAREHOUSE_CREDITS"].map(
        lambda value: credits_to_usd(value, rate)
    )
    kpi_row([
        {"label": "Data products", "value": f"{len(frame):,}"},
        {"label": "Catalog entities", "value": f"{safe_float(frame['CATALOG_ENTITIES'].sum()):,.0f}"},
        {"label": "Object-attributed cost", "value": format_usd(frame["MEASURED_OBJECT_USD"].sum()),
         "help": "Measured query and maintenance cost at object grain."},
        {"label": "Warehouse cost", "value": format_usd(frame["METERED_WAREHOUSE_USD"].sum()),
         "help": "Metered warehouse cost; separate because it can overlap object-attributed compute."},
    ])

    def open_product(index: int) -> None:
        _open_entity("DATA_PRODUCT", str(frame.iloc[int(index)]["DATA_PRODUCT"]))

    selectable_nav_table(
        frame[[
            "DATA_PRODUCT", "TEAM", "OWNER_NAME", "CRITICALITY", "CATALOG_ENTITIES",
            "MEASURED_OBJECT_USD", "METERED_WAREHOUSE_USD", "COSTED_OBJECTS",
            "WAREHOUSES", "TASK_RUNS", "TASK_FAILURES", "TASK_FAIL_PCT",
        ]],
        key="decision_product_table", on_select=open_product, height=390,
        column_config={
            # rec32: the two dollar columns are SEPARATE lenses that overlap and are
            # NOT additive — state that in each header's help, where the summing impulse
            # happens (totals are deliberately absent for the same reason).
            "MEASURED_OBJECT_USD": st.column_config.NumberColumn(
                "Object-attributed $", format="$%.2f",
                help="Cost attributed to this product's objects from measured query cost. "
                     "A SEPARATE lens from Warehouse $ — the two overlap and are NOT "
                     "additive; never sum them."),
            "METERED_WAREHOUSE_USD": st.column_config.NumberColumn(
                "Warehouse $", format="$%.2f",
                help="Cost metered on the warehouses this product runs on, including idle. "
                     "A SEPARATE lens from Object-attributed $ — the two overlap and are "
                     "NOT additive; never sum them."),
            "TASK_FAIL_PCT": st.column_config.NumberColumn("Task fail %", format="%.2f%%"),
        },
        sort_label="measured object cost, then metered warehouse cost",
    )


def _cost_truth(company: str, days: int) -> None:
    result = run(
        workbench_sql.cost_truth(days, company), page=_PAGE,
        key=f"decision_cost_truth_{company}_{days}", tier="historical",
        source="existing billed, metered, measured and allocated facts",
    )
    if not guard(result, "No cost facts exist in this window."):
        return
    frame = result.df.copy()
    # DS #4: cost_truth ALWAYS returns four rows (un-grouped scalar aggregates), so an
    # empty basis arrives as NULL CREDITS, not a missing row — and safe_float would turn
    # that into a measured-looking $0.00. Track presence and render "No evidence" per basis
    # instead, most importantly on per-company views where the three company-scoped bases
    # can be legitimately empty while account-wide BILLED shows real dollars.
    present = {
        str(row.get("BASIS")): bool(pd.notna(row.get("CREDITS")))
        for _, row in frame.iterrows()
    }
    values = {
        str(row.get("BASIS")): safe_float(row.get("CREDITS"))
        for _, row in frame.iterrows()
    }
    metered = values.get("METERED", 0.0)
    allocated = values.get("ALLOCATED", 0.0)
    measured = values.get("MEASURED", 0.0)
    billed = values.get("BILLED", 0.0)
    # rec29: dollars primary (execs read dollars, not credits), credits secondary.
    # The three compute-clean bases convert at the compute rate; BILLED blends
    # services, so its AI/Cortex share must price at the AI rate (house rule d) via
    # the billed AI/OTHER split — a flat rate would overprice AI credits.
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    split = run(mart_sql.billed_split(days), page=_PAGE,
                key=f"decision_billed_split_{days}", tier="historical",
                source="FACT_METERING_DAILY (billed AI/OTHER split)")
    if split.usable():
        _s = split.df.iloc[0]
        billed_usd = blended_billed_usd(safe_float(_s.get("CREDITS_BILLED_OTHER")),
                                        safe_float(_s.get("CREDITS_BILLED_AI")), rate, ai_rate)
        _billed_help = ("Account-wide billing basis; Company does not apply. AI/Cortex "
                        "credits priced at the AI rate, compute at the compute rate.")
    else:
        # Degrade only when the AI/OTHER split read fails: a flat compute rate on the
        # whole billed total slightly overstates AI — say so rather than claiming AI-aware.
        billed_usd = credits_to_usd(billed, rate)
        _billed_help = ("Account-wide billing basis; Company does not apply. AI/OTHER split "
                        "unavailable — priced at the flat compute rate (AI slightly overstated).")
    # DS #4: billed is account-wide (the cost_truth row OR the account-wide split); the
    # other three are company-scoped and can be legitimately absent. "No evidence" != $0.
    _billed_present = present.get("BILLED", False) or split.usable()
    _no_ev = "No evidence"
    kpi_row([
        {"label": "Billed credits (modeled $)",
         "value": format_usd(billed_usd) if _billed_present else _no_ev,
         "delta": f"{billed:,.0f} cr" if _billed_present else "—",
         "delta_color": "off", "help": _billed_help},
        {"label": "Metered warehouse",
         "value": format_usd(credits_to_usd(metered, rate)) if present.get("METERED") else _no_ev,
         "delta": f"{metered:,.0f} cr" if present.get("METERED") else "—", "delta_color": "off"},
        {"label": "Measured object-query",
         "value": format_usd(credits_to_usd(measured, rate)) if present.get("MEASURED") else _no_ev,
         "delta": f"{measured:,.0f} cr" if present.get("MEASURED") else "—", "delta_color": "off"},
        {"label": "Allocated to users",
         "value": format_usd(credits_to_usd(allocated, rate)) if present.get("ALLOCATED") else _no_ev,
         "delta": f"{allocated:,.0f} cr" if present.get("ALLOCATED") else "—", "delta_color": "off"},
    ])
    styled_table(frame, height=300, sort_label="semantic basis order")
    st.caption(
        "Dollars primary, credits secondary. Rows are lenses over cost, not addends: "
        "billed credits include the cloud-services adjustment but modeled $ uses configured "
        "rates; organization currency is billing truth. Metered includes warehouse idle; "
        "measured excludes idle; "
        "allocated redistributes warehouse usage."
        + (f" Measured is {measured / metered * 100:,.0f}% and allocated "
           f"{allocated / metered * 100:,.0f}% of metered."
           if (present.get("METERED") and metered and present.get("MEASURED")
               and present.get("ALLOCATED")) else "")
    )


def _scenarios(company: str) -> None:
    actions = run(
        workbench_sql.action_center(company, False, 500), page=_PAGE,
        key=f"decision_scenario_actions_{company}", tier="live",
        source="ACTION_QUEUE with confidence and entity keys",
    )
    if not actions.ok:
        empty_state("needs_setup", "Apply V074 to model confidence-aware action scenarios.")
        legacy = run(
            mart_sql.action_queue(500), page=_PAGE, key="decision_scenario_legacy",
            tier="live", source="ACTION_QUEUE (legacy read-only shape)",
        )
        if legacy.ok and not legacy.empty:
            styled_table(legacy.df, height=260)
        return
    # rec18: with an empty action queue the sliders would model nothing and the KPIs
    # would silently project $0 — say so and skip the controls until there is a queue.
    if actions.empty:
        empty_state("no_data_yet",
                    "No open actions to model yet — scenarios size a plan from the open "
                    "action queue. Create actions on Action Center to project savings.")
        return
    c1, c2, c3 = st.columns(3)
    with c1:
        adoption = st.slider("Adoption %", 0, 100, 60, 5, key="scenario_adoption")
    with c2:
        realization = st.slider("Realization %", 0, 100, 70, 5, key="scenario_realization")
    with c3:
        confidence = st.slider("Confidence floor", 0.0, 1.0, 0.6, 0.05,
                               key="scenario_confidence")
    projection = scenario_projection(
        actions.df, adoption_pct=adoption, realization_pct=realization,
        confidence_floor=confidence,
    )
    has_candidates = projection["candidates"] > 0
    ledger = run(
        mart_sql.savings_ledger(), page=_PAGE, key="decision_scenario_ledger",
        tier="recent", source="SAVINGS_LEDGER",
    )
    verified = (safe_float(ledger.df.get("VERIFIED_USD", pd.Series(dtype=float)).sum())
                if ledger.ok and not ledger.empty else 0.0)
    verified_value = format_usd(verified) if ledger.ok else "Unavailable"
    kpi_row([
        {"label": "Eligible entities", "value": f"{projection['candidates']:,.0f}"},
        {"label": "Gross authored estimate",
         "value": format_usd(projection["gross_estimate"]) if has_candidates else "No evidence"},
        {"label": "Expected capture",
         "value": format_usd(projection["expected_capture"]) if has_candidates else "No evidence",
         "delta": (f"{format_usd(projection['low_capture'])} to "
                   f"{format_usd(projection['high_capture'])}") if has_candidates else None,
         "delta_color": "off"},
        {"label": "Verified separately", "value": verified_value,
         "severity": "ok" if ledger.ok else "warn",
         "help": ("Verified savings ledger total." if ledger.ok
                  else f"Savings ledger read failed: {ledger.error}")},
    ])
    st.caption(
        "Open action estimates are de-duplicated by entity, then adoption and realization "
        "haircuts are applied. Verified savings never enter the projection."
    )
    if not actions.empty:
        # DS #1: pin actions on watched entities to the top WITHIN their severity band, so a
        # watched entity's action surfaces first without burying a CRITICAL under a watched LOW.
        _viewer = viewer_name()
        _wl_res = run(workbench_sql.watchlist(_viewer), page=_PAGE,
                      key="decision_scenario_watchlist", tier="live", source="USER_WATCHLIST"
                      ) if _viewer else None
        _wl = _wl_res.df if (_wl_res is not None and _wl_res.usable()) else None
        adf = actions.df.copy()
        adf["WATCHED"] = mark_watched_pairs(adf, _wl, "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY")
        if bool(adf["WATCHED"].any()):
            _sev_rank = adf["SEVERITY"].astype(str).str.upper().map(
                {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}).fillna(4)
            adf = (adf.assign(_SR=_sev_rank)
                   .sort_values(["_SR", "WATCHED"], ascending=[True, False], kind="stable")
                   .drop(columns="_SR").reset_index(drop=True))
            st.caption(f"★ {int(adf['WATCHED'].sum())} action(s) on your watched entities, "
                       "pinned to the top of their severity band.")
        styled_table(
            adf[[column for column in (
                "WATCHED", "SEVERITY", "TITLE", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY",
                "CONFIDENCE", "ESTIMATED_USD", "OWNER", "DUE_DATE",
            ) if column in adf.columns]],
            height=320, sort_label="action priority order",
        )


def _experiments() -> None:
    result = run(
        workbench_sql.experiments(limit=300), page=_PAGE, key="decision_experiments",
        tier="recent", source="OPTIMIZATION_EXPERIMENTS",
    )
    if not result.ok:
        empty_state("needs_setup", "Apply V074 to track optimization experiments.")
        return
    if result.empty:
        empty_state("no_data_yet", "No optimization experiments have been started.")
        return
    frame = result.df.reset_index(drop=True)
    status = frame["STATUS"].astype(str).str.upper()
    kpi_row([
        {"label": "Experiments", "value": f"{len(frame):,}"},
        {"label": "Running / observing", "value": f"{status.isin(('RUNNING', 'OBSERVING')).sum():,}"},
        {"label": "Verified", "value": f"{status.eq('VERIFIED').sum():,}", "severity": "ok"},
        {"label": "Verified value", "value": format_usd(frame["VERIFIED_USD"].map(safe_float).sum())},
    ])
    selected = selectable_table(frame, key="decision_experiment_table", height=340,
                                sort_label="active status, observation end, then update")
    # rec17: no silent row-0 — an unselected table must not render (and let an operator
    # Save) the first experiment as if it were the chosen one. Require a selection.
    if selected is None:
        st.caption("Select an experiment above to view its hypothesis, open its entity, "
                   "or record results.")
        return
    row = frame.iloc[int(selected)]
    experiment_id = str(row["EXPERIMENT_ID"])
    if st.button("Open experiment entity", key=f"experiment_entity_{experiment_id}",
                 type="tertiary"):
        _open_entity(str(row["ENTITY_TYPE"]), str(row["ENTITY_KEY"]))
    st.markdown(f"**{row['TITLE']}**")
    st.write(str(row.get("HYPOTHESIS") or ""))
    if is_operator():
        current = str(row.get("STATUS") or "PLANNED").upper()
        update_status = st.selectbox(
            "Status", EXPERIMENT_STATUSES,
            index=EXPERIMENT_STATUSES.index(current) if current in EXPERIMENT_STATUSES else 0,
            key=f"experiment_status_{experiment_id}",
        )
        result_note = st.text_area(
            "Result evidence", value=str(row.get("RESULT_NOTE") or ""),
            key=f"experiment_result_{experiment_id}", max_chars=4000,
        )
        verified_value = st.number_input(
            "Verified USD", min_value=0.0, value=safe_float(row.get("VERIFIED_USD")),
            step=25.0, key=f"experiment_value_{experiment_id}",
        )
        statement = update_experiment_sql(
            experiment_id, status=update_status, result=result_note,
            verified_usd=verified_value if update_status == "VERIFIED" else None,
            actor=viewer_name(),
        )
        with st.expander("SQL preview"):
            st.code(statement, language="sql")
        if st.button("Save experiment", key=f"experiment_save_{experiment_id}", type="primary"):
            ok, message = execute_statement(statement, page=_PAGE)
            notify(ok, message)
            if ok:
                st.rerun()


# rec8: the page shell (header + primary section bar + dispatch) now lives in
# app/ui/pages/decision_studio.py; this module keeps only the section bodies.
