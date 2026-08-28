"""Decision Studio: prioritization, objectives, economics and experiment follow-through."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.identity import viewer_name
from app.core.query import execute_statement, run
from app.core.session import is_operator
from app.core.state import request_navigation
from app.data import mart_sql, workbench_sql
from app.logic import insights
from app.logic.actions import ledger_totals, savings_by_lever, savings_by_month
from app.logic.decision import prioritize_workloads, scenario_projection, slo_summary
from app.logic.formulas import (
    account_now,
    blended_billed_usd,
    credits_to_usd,
    format_usd,
    md_dollars,
    safe_float,
)
from app.logic.proof import (
    acceptance_summary,
    account_precision,
    proof_verdict,
    roi_multiple,
)
from app.logic.workbench import (
    EXPERIMENT_STATUSES,
    SLO_METRIC_KEYS,
    create_slo_objective_sql,
    experiment_age_days,
    mark_watched,
    mark_watched_pairs,
    stale_planning,
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
    section_header,
    selectable_nav_table,
    selectable_table,
    stamp_write,
    stash_section_count,
    styled_table,
    write_gate_open,
)

_PAGE = "Decision Studio"


def _open_entity(kind: str, key: str) -> None:
    request_navigation(
        "Control Room", "Entity 360",
        context={"entity_type": kind, "entity_key": key},
    )


def _open_action_center() -> None:
    """F56: experiments are created from a work item's 'Start optimization
    experiment' expander on Action Center — the jump target for the
    Experiments empty state (same request_navigation idiom the scorecard uses)."""
    request_navigation("Control Room", "Action Center")


_PORTFOLIO_CAP = 200


def _portfolio(company: str, days: int, rate: float) -> None:
    result = run(
        workbench_sql.workload_portfolio(days, company, _PORTFOLIO_CAP), page=_PAGE,
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
    # #15: the board caps at the top _PORTFOLIO_CAP families by measured credits; the
    # app's row-truncation banner only fires at the 5000 fetch cap, so this smaller cap
    # would truncate silently. Disclose when the cap is hit so "N families" isn't misread
    # as the whole population.
    if len(portfolio) >= _PORTFOLIO_CAP:
        st.caption(
            f"Showing the top {_PORTFOLIO_CAP} query families by measured credits — more "
            "exist in this scope; narrow the Window or Company to surface the rest."
        )
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
        {"label": "30d normalized impact",
         "value": format_usd(portfolio["IMPACT_USD_30D"].sum()),
         "help": "Measured pattern credits normalized to 30 days; observed cost, not promised savings."},
        {"label": "High-confidence", "value": f"{portfolio['CONFIDENCE'].ge(0.8).sum():,}"},
        # DS #2: how much of the board's recommendations rest on COMPLETE evidence. Low
        # coverage = more calls made on partial signals; per-row EVIDENCE_COVERAGE shows which.
        {"label": "Evidence coverage",
         "value": (f"{portfolio['EVIDENCE_COVERAGE'].mean() * 100:,.0f}%" if len(portfolio) else "—"),
         "severity": (("ok" if portfolio["EVIDENCE_COVERAGE"].mean() >= 0.8 else "warn")
                      if len(portfolio) else ""),
         "help": "Average share of the three evidence signals (cache, latency, fail-rate) "
                 "present per family. Low coverage means more of the board's recommendations "
                 "rest on partial evidence — the per-row EVIDENCE_COVERAGE column shows which."},
        {"label": "Watching", "value": f"{int(portfolio['WATCHED'].sum()):,}",
         "help": "Query families on your personal watchlist — pinned to the top of their lane. "
                 "Watch or unwatch a family from its Entity 360 (open one below)."},
    ])
    charts.workload_portfolio(portfolio)
    # DS #1: where does the measured 30-day cost concentrate? A per-lane subtotal so the
    # ACT NOW / PLAN / VALIDATE cost split reads at a glance (measured COST, not savings).
    _lane_cost = portfolio.groupby("LANE")["IMPACT_USD_30D"].sum()
    _lane_bits = " · ".join(
        f"{_lane}: {format_usd(float(_lane_cost.get(_lane, 0.0)))}"
        for _lane in ("ACT NOW", "PLAN", "VALIDATE") if _lane in _lane_cost.index
    )
    if _lane_bits:
        st.caption(md_dollars(f"30-day measured cost by lane — {_lane_bits} "
                   "(observed cost concentration, not promised savings)."))

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
            if (st.button("Create objective", key="slo_new_execute", type="primary")
                    and write_gate_open("slo_new_execute")):
                ok, message = execute_statement(statement, page=_PAGE)
                stamp_write("slo_new_execute", ok)  # C48
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
        {"label": "Owner conflicts",
         "value": f"{int(frame['OWNER_CONFLICT'].astype(bool).sum()):,}",
         "severity": "warn" if bool(frame["OWNER_CONFLICT"].astype(bool).any()) else "",
         "help": "Data products whose catalog entities carry different owners — ambiguous "
                 "ownership to resolve; the OWNER_NAME shown is one of several."},
    ])

    # Codex #20: coverage is the trust anchor for the whole board — mapped vs total
    # account object cost and mapped vs total catalog entities, with the unmapped residual.
    _cov = run(workbench_sql.product_mapping_totals(days, company), page=_PAGE,
               key=f"product_coverage_{company}_{days}", tier="recent",
               source="FACT_OBJECT_COST_DAILY + ENTITY_CATALOG", probe=True)
    if _cov.usable():
        _cr = _cov.df.iloc[0]
        _total_obj = credits_to_usd(safe_float(_cr.get("TOTAL_OBJECT_CREDITS")), rate)
        _mapped_obj = float(frame["MEASURED_OBJECT_USD"].sum())
        _unmapped = max(0.0, _total_obj - _mapped_obj)
        _cov_pct = (_mapped_obj / _total_obj * 100.0) if _total_obj > 0 else 0.0
        _tot_e = int(safe_float(_cr.get("TOTAL_ENTITIES")))
        _map_e = int(safe_float(_cr.get("MAPPED_ENTITIES")))
        kpi_row([
            {"label": "Product-mapped object cost", "value": format_usd(_mapped_obj),
             "delta": f"{_cov_pct:.0f}% of account object cost",
             "delta_color": "normal" if _cov_pct >= 80 else "inverse" if _cov_pct < 50 else "off",
             "help": "Object cost attributable to a mapped data product, as a share of the whole "
                     "account's object cost. Low coverage = the board below sees only a slice."},
            {"label": "Unmapped object cost", "value": format_usd(_unmapped),
             "delta_color": "inverse" if _unmapped > 0 else "off",
             "help": "Object cost on entities with NO data-product mapping — invisible to the "
                     "per-product board. Map them in the catalog to close the gap."},
            {"label": "Entity coverage", "value": f"{_map_e:,}/{_tot_e:,}",
             "delta": (f"{_map_e / _tot_e * 100:.0f}% mapped" if _tot_e else "no catalog"),
             "delta_color": "off",
             "help": "Catalog entities mapped to a data product vs all catalog entities."},
        ])
        st.caption("Coverage first: the per-product economics below covers only the mapped share "
                   "above, so read its totals as a floor — not the whole account — until coverage "
                   "is high.")

    # #28: cost-per-consumer + retirement candidates. Which products cost real money but
    # have lost their readers? Consumer reach + reads trend from ACCESS_HISTORY
    # (Enterprise-only, degrade-safe) joined to the object cost above.
    reads = run(workbench_sql.product_consumer_reads(days, company), page=_PAGE,
                key=f"decision_product_consumers_{company}_{days}", tier="recent",
                source="ENTITY_CATALOG + ACCESS_HISTORY reads", probe=True)
    verdicts = insights.product_retirement(
        result.df, reads.df if reads.usable() else pd.DataFrame(), rate)
    if not verdicts.empty:
        st.markdown("**Cost per consumer & retirement candidates**")
        # Three degrade states, distinguished honestly: ACCESS_HISTORY absent (edition/
        # permission), present-but-empty (queried, no reads for mapped products), or
        # measured. `_measured` gates the consumer surfaces so "couldn't measure" never
        # renders as a measured 0 (unlike a genuine measured-zero product).
        _measured = reads.usable()
        if not reads.ok:
            st.caption("Consumer reach needs Enterprise ACCESS_HISTORY, which isn't available here "
                       "— every verdict shows INSUFFICIENT_DATA (usage can't be measured, not zero).")
        elif reads.empty:
            st.caption("ACCESS_HISTORY returned no reads for mapped products in this window "
                       "(recent-read ingestion lag, or none were read) — verdicts show "
                       "INSUFFICIENT_DATA because usage couldn't be measured, not because it's zero.")
        _retire = int((verdicts["RETIREMENT_VERDICT"] == "RETIRE_CANDIDATE").sum())
        _served = int(safe_float(verdicts.get("DISTINCT_CONSUMERS", pd.Series(dtype=float)).sum()))
        _cpc = verdicts["COST_PER_CONSUMER_USD"].dropna()
        kpi_row([
            {"label": "Consumers served", "value": f"{_served:,}" if _measured else "—",
             "help": "Distinct accounts that read these products' objects in the cost window "
                     "(any read, incl. service/pipeline; write-only ETL excluded). Blank when "
                     "consumer reach can't be measured (no Enterprise ACCESS_HISTORY)."},
            {"label": "Median $/consumer",
             "value": format_usd(float(_cpc.median())) if not _cpc.empty else "—",
             "help": "Object-attributed cost per distinct consumer. Blank when no product has consumers."},
            {"label": "Retire candidates", "value": f"{_retire:,}",
             "severity": "warn" if _retire else "",
             "help": "Costly products with no or collapsing reads — a candidate to review, not an order."},
        ])

        def open_retire(index: int) -> None:
            _open_entity("DATA_PRODUCT", str(verdicts.iloc[int(index)]["DATA_PRODUCT"]))

        # Drop the consumer column when unmeasured — every value is a meaningless 0 that
        # would contradict the INSUFFICIENT_DATA verdict on the same row.
        _rcols = [c for c in ("DATA_PRODUCT", "COST_USD", "DISTINCT_CONSUMERS",
                              "COST_PER_CONSUMER_USD", "READ_TREND_PCT", "RETIREMENT_VERDICT")
                  if c in verdicts.columns and (c != "DISTINCT_CONSUMERS" or _measured)]
        selectable_nav_table(
            verdicts[_rcols], key="decision_retire_table", on_select=open_retire, height=320,
            column_config={
                "COST_USD": st.column_config.NumberColumn(
                    "Object cost $", format="$%.2f",
                    help="Measured object-attributed cost over the window (credit rate applied)."),
                "DISTINCT_CONSUMERS": st.column_config.NumberColumn(
                    "Consumers", help="Distinct accounts that read the product in the cost window."),
                "COST_PER_CONSUMER_USD": st.column_config.NumberColumn(
                    "$ / consumer", format="$%.2f",
                    help="Object cost divided by distinct consumers; blank when a product has 0 consumers."),
                "READ_TREND_PCT": st.column_config.NumberColumn(
                    "Reads trend", format="%.0f%%",
                    help="Recent-window reads vs the equal window before; blank for a brand-new product."),
            },
            sort_label="object cost",
        )
        st.caption("Advisory: RETIRE_CANDIDATE = costly with usage gone or collapsing; "
                   "INSUFFICIENT_DATA = usage couldn't be measured (never a retire call). Consumers "
                   "count read accesses from ACCESS_HISTORY (Enterprise) — write-only ETL targets are "
                   "excluded, but service/pipeline reads still count. A candidate to review with the "
                   "owner, not an order.")

    def open_product(index: int) -> None:
        _open_entity("DATA_PRODUCT", str(frame.iloc[int(index)]["DATA_PRODUCT"]))

    selectable_nav_table(
        frame[[
            "DATA_PRODUCT", "TEAM", "OWNER_NAME", "OWNER_CONFLICT", "CRITICALITY",
            "CATALOG_ENTITIES", "MEASURED_OBJECT_USD", "METERED_WAREHOUSE_USD",
            "COSTED_OBJECTS", "WAREHOUSES", "TASK_RUNS", "TASK_FAILURES", "TASK_FAIL_PCT",
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
            "OWNER_CONFLICT": st.column_config.CheckboxColumn(
                "Owner conflict?",
                help="This product's catalog entities carry more than one owner — the "
                     "OWNER_NAME shown is one of several. Resolve ownership in the catalog."),
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
    # F26/C34 review: NO sort_label here — the four rows are LENSES over cost
    # ("do not add", DS #4's No-evidence discipline), so a rank ordinal and a
    # credits race bar would invite exactly the cross-basis comparison this
    # table forbids. The label was invisible on a 4-row table anyway.
    styled_table(frame, height=300)
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


def _scorecard(company: str, rate: float) -> None:
    """Prove-it flagship: does OVERWATCH work, and does it pay for itself? Composes the
    five trust/value signals — ROI multiple, realization, acceptance, alert precision,
    evidence coverage — into one director-facing scorecard + a one-line verdict. The gate
    the owner set before going autonomous. Account-wide; reuses the ledger/queue/alert
    marts and adds no new scan (only the small account roll-ups in app/logic/proof.py)."""
    section_header("Does OVERWATCH earn its keep?", "info", "target")
    st.caption("Account-wide proof: what the tool recommended, how much the team acted on, what "
               "verified out in dollars vs OVERWATCH's own run cost, and how much of the advice "
               "rests on labeled evidence. Resolve alerts with a kind and verify savings to grow it.")

    ledger = run(mart_sql.savings_ledger(), page=_PAGE, key="decision_roi_ledger",
                 tier="recent", source="SAVINGS_LEDGER")
    if not ledger.ok:
        empty_state("needs_setup", "Apply the action + savings layer (V051+) to start the proof record.")
        return
    totals = ledger_totals(ledger.df)
    realization = totals["realization_pct"]

    _q = run(mart_sql.savings_summary_quarter(), page=_PAGE, key="sc_quarter",
             tier="recent", source="SAVINGS_LEDGER (QTD)")
    _ac = run(mart_sql.app_cost_quarter(), page=_PAGE, key="sc_appcost",
              tier="recent", source="FACT_WAREHOUSE_DAILY (app warehouse, QTD)")
    verified_qtd = safe_float(_q.df.iloc[0].get("VERIFIED_QTD_USD")) if _q.usable() else 0.0
    run_cost = safe_float(_ac.df.iloc[0].get("APP_CREDITS_QTD")) * rate if _ac.usable() else 0.0
    roi = roi_multiple(verified_qtd, run_cost)

    _acc = run(mart_sql.action_acceptance(90), page=_PAGE, key="sc_accept",
               tier="recent", source="ACTION_QUEUE (decided in 90d)")
    acc = acceptance_summary(_acc.df if _acc.usable() else None)

    _prec = run(mart_sql.rule_precision(90), page=_PAGE, key="sc_precision",
                tier="recent", source="ALERT_EVENTS resolution kinds", probe=True)
    prec = account_precision(_prec.df if _prec.usable() else None)

    evcov = None
    _port = run(workbench_sql.workload_portfolio(30, "ALL", 200), page=_PAGE, key="sc_portfolio",
                tier="historical", source="MART_PATTERN_COST_DAILY (evidence coverage)", probe=True)
    if _port.usable():
        _pf = prioritize_workloads(_port.df, rate, 30)
        if not _pf.empty and "EVIDENCE_COVERAGE" in _pf.columns:
            evcov = round(float(_pf["EVIDENCE_COVERAGE"].mean()) * 100, 0)

    verdict = proof_verdict(roi, realization, acc["ACCEPTANCE_PCT"], prec)
    _banner = {"good": st.success, "watch": st.warning, "unproven": st.info}[verdict["level"]]
    _banner(md_dollars(verdict["headline"]))

    kpi_row([
        {"label": "Pays for itself",
         "value": (f"{roi['RATIO']:.1f}×" if roi["RATIO"] is not None else "—"),
         "severity": ("ok" if roi["PAYS"] else ("warn" if roi["RATIO"] is not None else "")),
         "delta": (f"{format_usd(roi['VERIFIED_USD'])} verified vs {format_usd(roi['RUN_COST_USD'])} run cost (QTD)"
                   if roi["RATIO"] is not None else "run cost or verified $ not measured yet"),
         "delta_color": "off",
         "help": "Verified savings this quarter as a multiple of OVERWATCH's own warehouse run cost "
                 "(APP_WAREHOUSE credits × rate). ≥1× means it pays for itself."},
        {"label": "Realization",
         "value": (f"{realization:,.0f}%" if realization is not None else "—"),
         "help": "Of what verified items were estimated to save, how much actually measured out. "
                 "Near 100% means the estimates held up (verified items that carried an estimate)."},
        {"label": "Acted on",
         "value": (f"{acc['ACCEPTANCE_PCT']:,.0f}%" if acc["ACCEPTANCE_PCT"] is not None else "—"),
         "delta": f"{acc['DONE_N']} done · {acc['DROPPED_N']} dismissed · {acc['OPEN_N']} open",
         "delta_color": "off",
         "help": "Of the recommendations the team DECIDED on (last 90d), the share acted on (DONE) vs "
                 "dismissed (DROPPED). Open items are still undecided, not counted for or against."},
        {"label": "Alert precision",
         "value": (f"{prec['PRECISION_PCT']:,.0f}%" if prec["PRECISION_PCT"] is not None else "—"),
         "severity": ("ok" if (prec["PRECISION_PCT"] or 0) >= 70 else
                      ("warn" if prec["PRECISION_PCT"] is not None else "")),
         "delta": (f"{prec['ACTIONED']} actioned · {prec['NOISE']} noise"
                   + (f" · {prec['UNTAGGED_SHARE_PCT']:.0f}% unlabeled" if prec["UNTAGGED_SHARE_PCT"] else "")),
         "delta_color": "off",
         "help": "When a rule fires and is resolved, how often it was real (ACTIONED) vs noise "
                 "(expected/maintenance closes excluded). A high unlabeled share means the number "
                 "isn't trustworthy yet — resolve alerts with a kind on Alerts ▸ Open events."},
        {"label": "On solid evidence",
         "value": (f"{evcov:,.0f}%" if evcov is not None else "—"),
         "help": "Share of the recommendation board's three signals (cache, latency, fail-rate) "
                 "actually present per family — how much of the advice rests on complete evidence."},
    ])

    _fn = run(mart_sql.acceptance_funnel(90), page=_PAGE, key="sc_funnel",
              tier="recent", source="REMEDIATION_LOG + SAVINGS_LEDGER", probe=True)
    if _fn.usable():
        fr = _fn.df.iloc[0]
        st.caption(md_dollars(
            "Last 90 days — "
            f"**{int(safe_float(fr.get('SAVINGS_ESTIMATED')))}** savings items estimated → "
            f"**{int(safe_float(fr.get('FIXES_EXECUTED')))}** fixes executed → "
            f"**{int(safe_float(fr.get('SAVINGS_VERIFIED')))}** verified "
            f"(**{format_usd(safe_float(fr.get('VERIFIED_USD')))}**)"
            + (f" · {int(safe_float(fr.get('SAVINGS_REJECTED')))} rejected"
               if safe_float(fr.get("SAVINGS_REJECTED")) else "") + "."))

    _c1, _c2 = st.columns(2)
    with _c1:
        if st.button("Savings track record → ROI", key="sc_link_roi", type="secondary"):
            request_navigation("Decision Studio", "ROI")
    with _c2:
        if st.button("Per-rule alert precision → Alerts ▸ Rules", key="sc_link_alerts", type="secondary"):
            request_navigation("Alerts", "Rules")
    result_caption(ledger)


def _roi(company: str) -> None:
    """DS flagship (#40/#31/#19): the ROI / realization story, front and center — what
    OVERWATCH has verifiably saved, how well the estimates held up, the monthly run-rate,
    and which levers produced it. The director-facing proof that the loop closes. All from
    the SAVINGS_LEDGER the app already books (no new source)."""
    section_header("Return on OVERWATCH — verified savings", "info", "target")
    st.caption("Account-wide — SAVINGS_LEDGER has no company grain, so this track record does "
               "not narrow to the Company filter.")
    ledger = run(mart_sql.savings_ledger(), page=_PAGE, key="decision_roi_ledger",
                 tier="recent", source="SAVINGS_LEDGER")
    if not ledger.ok:
        empty_state("needs_setup", "Apply the action layer (V051+) to book and verify savings.")
        return
    totals = ledger_totals(ledger.df)
    _real = totals["realization_pct"]
    _avgd = totals["avg_days_to_verify"]
    kpi_row([
        {"label": "Verified savings (all time)", "value": format_usd(totals["verified_usd"]),
         "severity": "ok" if totals["verified_usd"] else "",
         "help": "Measured, proof-backed savings booked to the ledger — never mixes in estimates."},
        {"label": "Verified this quarter", "value": format_usd(totals["verified_qtd_usd"])},
        {"label": "Realization rate",
         "value": (f"{_real:,.0f}%" if _real is not None else "—"),
         "delta": (f"{format_usd(totals['verified_usd'])} of "
                   f"{format_usd(totals['verified_estimated_usd'])} estimated"
                   if _real is not None else "nothing verified yet"),
         "delta_color": "off",
         "help": "Verified $ as a share of what those items were estimated to save — the honest "
                 "estimate-vs-actual (verified items that carried an estimate). Near 100% means the "
                 "estimates held up; above 100% means realized savings beat the estimate."},
        {"label": "Open pipeline", "value": format_usd(totals["estimated_usd"]),
         "delta": f"{totals['estimated_count']:,} item(s) awaiting proof", "delta_color": "off",
         "help": "Estimated savings still unverified — the opportunity ahead. Verify them on "
                 "Experiments (below) or Cost ▸ Optimize."},
    ])
    if totals["verified_usd"] > 0:
        st.markdown(md_dollars(
            f"OVERWATCH has verified **{format_usd(totals['verified_usd'])}** in savings across "
            f"**{totals['verified_count']:,}** item(s)"
            + (f", realizing **{_real:,.0f}%** of what they were estimated to save"
               if _real is not None else "")
            + (f", closing the loop in **{_avgd:g} days** on average." if _avgd is not None else ".")
            + f" **{format_usd(totals['estimated_usd'])}** more is estimated, awaiting proof."))
    else:
        empty_state("no_data_yet",
                    f"No savings verified yet — {format_usd(totals['estimated_usd'])} is estimated across "
                    f"{totals['estimated_count']:,} item(s). Verify optimizations on Experiments (below) "
                    "or Cost ▸ Optimize to start the track record.")

    month_df = savings_by_month(ledger.df, 12)
    lever_df = savings_by_lever(ledger.df)
    if not month_df.empty:
        st.markdown("**Verified-savings run-rate — by month**")
        charts.daily_metric_line(month_df, "MONTH", "VERIFIED_USD", "verified $ / month")
    if not lever_df.empty:
        st.markdown("**Where the realized savings come from — by lever**")
        charts.bar_usd(lever_df, "LEVER", "VERIFIED_USD", "verified $ by lever", top_n=10)
        styled_table(lever_df, height=220, column_config={
            "VERIFIED_USD": st.column_config.NumberColumn("Verified $", format="$%.2f"),
            "REALIZATION_PCT": st.column_config.NumberColumn("Realization %", format="%.0f%%"),
            "ITEMS": st.column_config.NumberColumn("Items", format="%d"),
        })
    result_caption(ledger)


def _scenarios(company: str) -> None:
    actions = run(
        workbench_sql.action_center(company, False, 500), page=_PAGE,
        key=f"decision_scenario_actions_{company}", tier="live",
        source="ACTION_QUEUE with confidence and entity keys",
    )
    if not actions.ok:
        empty_state("needs_setup", "Apply V074 to model confidence-aware action scenarios.")
        legacy = run(
            mart_sql.action_queue(500, company), page=_PAGE,
            key=f"decision_scenario_legacy_{company}",
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
    kpi_row([
        {"label": "Eligible entities", "value": f"{projection['candidates']:,.0f}"},
        {"label": "Gross authored estimate",
         "value": format_usd(projection["gross_estimate"]) if has_candidates else "No evidence"},
        {"label": "Expected capture",
         "value": format_usd(projection["expected_capture"]) if has_candidates else "No evidence",
         "delta": (f"{format_usd(projection['low_capture'])} to "
                   f"{format_usd(projection['high_capture'])}") if has_candidates else None,
         "delta_color": "off"},
    ])
    st.caption(
        "Open action estimates are de-duplicated by entity, then adoption and realization "
        "haircuts are applied. Estimates are summed at face value across time bases "
        "(monthly, one-time, annual, or unlabeled — see each action's PERIOD), so read the "
        "projection as an order-of-magnitude opportunity, not a strict run-rate. "
        "Verified savings never enter the projection."
    )
    # The realized track record (verified $, realization rate, run-rate, levers) is now
    # its own first-class **ROI** section — Scenarios stays focused on the forward projection.
    st.caption("→ Realized savings and the estimate-vs-actual track record are in the **ROI** "
               "section (top of Decision Studio).")
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
        # DS #34: flag open actions untouched for 30+ days — the plan was made and forgotten,
        # so its estimate is decaying. A prompt to re-estimate, act, or close.
        adf["STALE"] = stale_planning(adf, account_now())
        _n_stale = int(adf["STALE"].sum())
        if _n_stale:
            st.caption(f"⚠ {_n_stale} open action(s) not touched in 30+ days — re-estimate, "
                       "act, or close them; a stale plan's estimate is decaying.")
        if bool(adf["WATCHED"].any()):
            _sev_rank = adf["SEVERITY"].astype(str).str.upper().map(
                {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}).fillna(4)
            adf = (adf.assign(_SR=_sev_rank)
                   .sort_values(["_SR", "WATCHED"], ascending=[True, False], kind="stable")
                   .drop(columns="_SR").reset_index(drop=True))
            st.caption(f"★ {int(adf['WATCHED'].sum())} action(s) on your watched entities, "
                       "pinned to the top of their severity band.")
        # Every other board on this page drills into Entity 360 on row click; the open-action
        # plan should too. A row click opens that action's SOURCE entity (no-op when it has none).
        display = adf[[column for column in (
            "WATCHED", "STALE", "SEVERITY", "TITLE", "SOURCE_ENTITY_TYPE",
            "SOURCE_ENTITY_KEY", "CONFIDENCE", "ESTIMATED_USD", "PERIOD", "OWNER", "DUE_DATE",
        ) if column in adf.columns]]

        def open_scenario_entity(index: int) -> None:
            row = adf.iloc[int(index)]
            kind = row.get("SOURCE_ENTITY_TYPE")
            key = row.get("SOURCE_ENTITY_KEY")
            if pd.notna(kind) and pd.notna(key) and str(kind).strip() and str(key).strip():
                _open_entity(str(kind).strip(), str(key).strip())

        selectable_nav_table(
            display, key="ds_scenarios", on_select=open_scenario_entity,
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
        # F56: point the empty state at where an experiment is actually created —
        # a selected work item's "Start optimization experiment" expander.
        # review fix: that expander is OPERATOR-gated, so the doorway renders
        # only for operators; viewers get accurate read-only wording.
        if is_operator():
            empty_state(
                "no_data_yet", "No optimization experiments have been started.",
                hint="Start one from a work item on Action Center — select an item and "
                     "use its 'Start optimization experiment' expander.",
                action_label="Open Action Center",
                on_action=_open_action_center,
                action_key="es_experiments_create",
            )
        else:
            empty_state(
                "no_data_yet", "No optimization experiments have been started.",
                hint="An operator starts one from a work item on Action Center; "
                     "results appear here for every viewer.",
            )
        return
    frame = result.df.reset_index(drop=True)
    status = frame["STATUS"].astype(str).str.upper()
    # C16: park the running/observing count for the section bar's "Experiments (n)" badge.
    # review fix: experiments are account-wide — no filter changes this count,
    # so the badge declares no scope dims and survives every filter flip.
    stash_section_count(_PAGE, "Experiments", status.isin(("RUNNING", "OBSERVING")).sum(),
                        dims=())
    # DS #24: show how long each experiment has existed — a long-running active one is a
    # stale-experiment signal ("RUNNING 38d") worth a look, not silent progress.
    frame["AGE_DAYS"] = experiment_age_days(frame, account_now())
    _active = status.isin(("PLANNED", "RUNNING", "OBSERVING"))
    _oldest_active = int(frame.loc[_active, "AGE_DAYS"].max()) if bool(_active.any()) else 0
    kpi_row([
        {"label": "Experiments", "value": f"{len(frame):,}"},
        {"label": "Running / observing", "value": f"{status.isin(('RUNNING', 'OBSERVING')).sum():,}"},
        {"label": "Oldest active", "value": (f"{_oldest_active:,} d" if _oldest_active else "—"),
         "severity": "warn" if _oldest_active >= 30 else "",
         "help": "Longest-running planned/running/observing experiment (days since it was "
                 "created). A long-lived active experiment may be stuck — verify or close it."},
        {"label": "Verified", "value": f"{status.eq('VERIFIED').sum():,}", "severity": "ok"},
        {"label": "Verified value",
         "value": format_usd(
             frame.loc[status.eq("VERIFIED"), "VERIFIED_USD"].map(safe_float).sum())},
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
        # Codex #22: a VERIFIED experiment books SAVINGS_LEDGER and feeds the director-
        # facing "Verified savings / Realization" headline, so it must be evidence-backed.
        # Gate the Save button on real proof (the SQL preview still renders so the operator
        # sees what would run). Proc-level enforcement in SP_VERIFY_EXPERIMENT is a follow-up.
        _proof_gaps: list[str] = []
        if update_status == "VERIFIED":
            if not str(result_note or "").strip():
                _proof_gaps.append("result evidence")
            if not (safe_float(verified_value) > 0):
                _proof_gaps.append("a verified $ amount above 0")
            _obs_end = pd.to_datetime(row.get("OBSERVATION_END"), errors="coerce")
            if pd.notna(_obs_end) and _obs_end.date() > account_now().date():
                _proof_gaps.append(
                    f"the observation window to close (ends {str(row.get('OBSERVATION_END'))[:10]})")
        statement = update_experiment_sql(
            experiment_id, status=update_status, result=result_note,
            verified_usd=verified_value if update_status == "VERIFIED" else None,
            actor=viewer_name(),
        )
        with st.expander("SQL preview"):
            st.code(statement, language="sql")
        if _proof_gaps:
            st.warning("Can't verify without proof — still needs: " + ", ".join(_proof_gaps)
                       + ". A verified experiment books the savings ledger and feeds the "
                       "'Verified savings' headline, so it must be evidence-backed.")
        if (st.button("Save experiment", key=f"experiment_save_{experiment_id}",
                      type="primary", disabled=bool(_proof_gaps))
                and write_gate_open(f"experiment_save_{experiment_id}")):
            ok, message = execute_statement(statement, page=_PAGE)
            stamp_write(f"experiment_save_{experiment_id}", ok)  # C48
            notify(ok, message)
            if ok:
                st.rerun()


# rec8: the page shell (header + primary section bar + dispatch) now lives in
# app/ui/pages/decision_studio.py; this module keeps only the section bodies.
