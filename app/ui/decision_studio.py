"""Proof (v4.597, Option C — formerly Decision Studio): does OVERWATCH pay for itself, what each
verified saving rests on, and the priced pipeline ahead.

Two READ-ONLY section bodies, dispatched by the page shell (app/ui/pages/decision_studio.py; both
module paths and the ``decision_section`` key are kept this release):
  * Proof    — the merged Scorecard + ROI: the ROI multiple, run-rate, Saved to date (accrued dollars,
               Next-Fifty #31), realization (plus the carried realization vs OVERWATCH's own estimate),
               settling items, acceptance, alert precision, evidence coverage, and a per-item evidence
               table (what each saving rests on, who gets credit, and the changes later undone). Every
               headline total is a SQL aggregate; pandas only shapes per-item rows.
  * Pipeline — addressable $/mo (the Cost ▸ Optimization & Savings idle rollup, mart-only) plus the queued
               ACTION_QUEUE work normalised to a monthly run-rate, and a projection whose sliders
               default to MEASURED acceptance / realization (a fragment: slider moves cost 0 reads).

No write path lives here (the SLO editor and the Experiments editor were retired; the Portfolio moved
to Operations ▸ Optimize, the Cost Truth ratio to Cost ▸ Spend & Attribution), so the page is safe for
the EXECUTIVE profile; every cross-page doorway is gated on state.can_open. ``_products`` is kept
HIDDEN (unreachable) for a later revival.
"""

from __future__ import annotations

import contextlib

import pandas as pd
import streamlit as st

from app.config import SAVINGS_ACTIVE_MONTHS, SAVINGS_MONTH_DAYS
from app.core.identity import viewer_name
from app.core.query import cache_scope, run, run_batch
from app.core.state import can_open, request_navigation
from app.data import mart27_sql, mart_sql, security_sql, workbench_sql
from app.logic import insights
from app.logic.actions import ledger_totals, savings_by_lever, savings_month_calendar
from app.logic.date_windows import is_prior_month_window, window_phrase
from app.logic.decision import (
    monthly_equivalent,
    pipeline_frame,
    prioritize_workloads,
    scenario_projection,
)
from app.logic.formulas import (
    account_now,
    credits_to_usd,
    format_usd,
    md_dollars,
    safe_float,
)
from app.logic.insights import (
    idle_advisor,
    with_auto_suspend_settings,
    with_warehouse_settings,
)
from app.logic.proof import (
    acceptance_summary,
    account_precision,
    carried_realization,
    evidence_rows,
    evidence_split,
    ledger_with_attribution,
    proof_verdict,
    reverted_rows,
    roi_multiple,
    saved_to_date,
    saved_to_date_card,
    settle_schedule,
)
from app.logic.savings_rollup import (
    UNREAD_HANDOFF_KEY,
    idle_opportunities,
    lever_basis,
    lever_short,
    resize_opportunities,
    rollup_savings,
    unread_lever,
)
from app.logic.sizing import size_recommendations
from app.logic.verdict import decision_studio_signals, page_verdict
from app.logic.workbench import mark_watched_pairs, stale_planning
from app.ui import charts
from app.ui.components import (
    AUTHORED_CONFIDENCE_HELP,
    confidence_progress_column,
    empty_state,
    hero_metric,
    kpi_row,
    result_caption,
    section_header,
    selectable_nav_table,
    served_days,
    styled_table,
    watch_star,
    watch_star_column,
)

_PAGE = "Proof"

# Projection policy defaults. A measured value replaces the first two when one exists; the confidence
# floor is policy (the evidence weight an item needs before it is counted), never measured.
_ASSUMED_ADOPTION_PCT = 60
_ASSUMED_REALIZATION_PCT = 70
_CONFIDENCE_FLOOR = 0.6

# Next-Fifty #31: Proof ▸ Proof "Saved to date" — mart_sql.savings_summary_quarter's SAVED_* columns.
_SAVED_TO_DATE_HELP = (
    f"Dollars already saved, not a run-rate: each verified item's monthly saving ÷ {SAVINGS_MONTH_DAYS} × "
    "the whole days it has been in place. A change the daily change scan measured counts from the day the "
    "scan saw it (its measured window is the proof); an item verified by hand counts from the day it was "
    f"verified. Accrual stops when the scan sees the change undone, or {SAVINGS_ACTIVE_MONTHS} months after "
    "verification, when the item leaves the run-rate. Only the measured windows are measured; the days "
    "after are carried forward at the verified rate — the same assumption the run-rate makes. Priced at "
    "the credit rate in effect when each item settled, never re-priced; not an invoice line. It does not "
    "feed the ROI multiple.")


def _open_entity(kind: str, key: str) -> None:
    request_navigation(
        "Control Room", "Entity 360",
        context={"entity_type": kind, "entity_key": key},
    )


def _open_savings_ledger() -> None:
    """F56 doorway for an empty track record: estimated items are verified on the Cost ▸ Optimization
    & Savings ledger (a page every profile, EXECUTIVE included, can open)."""
    # land on the pill that holds the ledger + verify workflow, not the section default (Idle & sizing).
    # The nested lazy_sections widget is not instantiated on this run, so seeding its key is legal.
    st.session_state["opt_section"] = "Remediation & ledger"
    request_navigation("Cost Intelligence", "Optimization & Savings")


def _open_storage_waste() -> None:
    """Next-Fifty #35 doorway: the unread-maintenance lever is counted only after its scan ran (and confirmed)
    in Cost ▸ Optimization & Savings ▸ Storage & waste this session. Seeds the pill like _open_savings_ledger
    (the nested lazy_sections widget is not instantiated on this run, so seeding its key is legal)."""
    st.session_state["opt_section"] = "Storage & waste"
    request_navigation("Cost Intelligence", "Optimization & Savings")


# HIDDEN in v4.597 (Option C); revive via memo §4 #6 derived products. Nothing dispatches here (the
# section left the page's section bar), so it issues no read; its locks stay green on the kept body.
def _products(company: str, days: int, rate: float, *, bounds: tuple | None = None) -> None:
    _lm = "_lm" if bounds is not None else ""
    result = run(
        workbench_sql.data_product_economics(days, company, bounds=bounds), page=_PAGE,
        key=f"decision_products_{company}_{days}{_lm}", tier="historical",
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
    _cov = run(workbench_sql.product_mapping_totals(days, company, bounds=bounds), page=_PAGE,
               key=f"product_coverage_{company}_{days}{_lm}", tier="recent",
               source="FACT_OBJECT_COST_DAILY + ENTITY_CATALOG", probe=True)
    if _cov.usable():
        _cr = _cov.df.iloc[0]
        _total_obj = credits_to_usd(safe_float(_cr.get("TOTAL_OBJECT_CREDITS")), rate)
        _mapped_obj = float(frame["MEASURED_OBJECT_USD"].sum())
        _unmapped = max(0.0, _total_obj - _mapped_obj)
        _cov_pct = (_mapped_obj / _total_obj * 100.0) if _total_obj > 0 else 0.0
        _tot_e = int(safe_float(_cr.get("TOTAL_ENTITIES")))
        _map_e = int(safe_float(_cr.get("MAPPED_ENTITIES")))
        # deferred-item: the coverage trust-anchor folds from a 3-card row into one
        # dense caption — it frames the board below, it is not three headline metrics.
        _ent = (f"entity coverage {_map_e:,}/{_tot_e:,} ({_map_e / _tot_e * 100:.0f}% mapped)"
                if _tot_e else "no catalog entities mapped")
        st.caption(md_dollars(  # two $ amounts in one sink must not render as LaTeX math
            f"Coverage — product-mapped object cost {format_usd(_mapped_obj)} "
            f"({_cov_pct:.0f}% of account object cost) · unmapped {format_usd(_unmapped)} · {_ent}. "
            "The per-product economics below covers only the mapped share, so read its totals as a "
            "floor — not the whole account — until coverage is high."))

    # #28: cost-per-consumer + retirement candidates. Which products cost real money but
    # have lost their readers? Consumer reach + reads trend from ACCESS_HISTORY
    # (Enterprise-only, degrade-safe) joined to the object cost above.
    # r10: the RECENT-vs-PRIOR reads trend compares CURRENT vs the PRIOR CALENDAR MONTH under bounds —
    # valid only for LAST_MONTH. Under the period-to-date presets the current side is PARTIAL, so the
    # trend flipped retirement verdicts (false REVIEW / false RETIRE_CANDIDATE; Current-year masked
    # real declines). Pass calendar bounds only for Last month; else None (trailing equal-length).
    # DISTINCT_CONSUMERS / cost-per-consumer already align to the bounded current window separately.
    reads = run(workbench_sql.product_consumer_reads(
                    days, company, bounds=bounds if is_prior_month_window(bounds) else None),
                page=_PAGE,
                key=f"decision_product_consumers_{company}_{days}{_lm}", tier="recent",
                source="ENTITY_CATALOG + ACCESS_HISTORY reads", probe=True)
    verdicts = insights.product_retirement(
        result.df, reads.df if reads.usable() else pd.DataFrame(), rate)
    if not verdicts.empty:
        # Codex-adj P2: use the section_header primitive (icon + a11y heading) this page already
        # uses elsewhere, instead of a bold-Markdown pseudo-heading that bypasses it.
        section_header("Cost per consumer & retirement candidates", "", "cost")
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
        # This is the SUM of each product's distinct readers (DISTINCT_CONSUMERS is a per-product
        # COUNT(DISTINCT USER_NAME)), so an account that reads several of these products is counted
        # once PER product -- it is a total reach figure, not a union-distinct account count. The KPI
        # is labeled and helped as reach accordingly, not as "distinct accounts" (ds-hunt 2026-08-30).
        _reach = int(safe_float(verdicts.get("DISTINCT_CONSUMERS", pd.Series(dtype=float)).sum()))
        _cpc = verdicts["COST_PER_CONSUMER_USD"].dropna()
        # deferred-item: the three aggregate consumer metrics fold into a caption — the
        # per-product DISTINCT_CONSUMERS + COST_PER_CONSUMER_USD + verdict are columns in
        # the retire table directly below, which is the real evidence surface.
        _reach_txt = f"{_reach:,}" if _measured else "—"
        _cpc_txt = format_usd(float(_cpc.median())) if not _cpc.empty else "—"
        st.caption(
            f"Consumer reach {_reach_txt} (sum of each product's distinct readers, so it can exceed "
            f"the number of unique accounts) · median {_cpc_txt}/consumer · **{_retire:,}** retire "
            "candidate(s) — costly products with no or collapsing reads (a candidate to review, not "
            "an order). Per-product detail in the table below.")

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


_PROOF_MEMO: dict = {}


def reset_proof_memo() -> None:
    """Clear the per-render _proof_signals memo. Called once at the top of the Proof page render so
    the shared prove-it computation runs ONCE per render (the page-open verdict AND the Proof /
    Pipeline section both call _proof_signals) — never across renders, where the run() cache's TTL
    stays the freshness authority. Safe with the Pipeline projection fragment because that fragment
    NEVER calls _proof_signals: its measured slider defaults are computed in the full run and passed
    in, so a fragment-only rerun (which skips this reset) never reads a stale memo."""
    _PROOF_MEMO.clear()


def _proof_signals(rate: float) -> dict | None:
    """Wave 2 #8: the shared prove-it reads + compute behind BOTH the page-open verdict and the Proof
    section, so the hoisted verdict and the section's banner figures are one identical computation.
    Returns None when the savings ledger isn't set up yet. Keeps the `savings_ledger(limit=None)` /
    `decision_roi_ledger_full` read to ONE site (v4.597: the Scorecard and ROI merged into Proof, so
    the old second ROI read is gone).

    Memoized PER RENDER (reset_proof_memo at the page top): the callers share ONE computation instead
    of cache-deduped read sets — same Snowflake I/O (the run() cache already deduped it), but no
    duplicate cache-hit telemetry double-counting these keys in APP_QUERY_TELEMETRY, and the verdict
    and the section agree by construction, not just by cache.

    ``verified_active`` / ``verified_active_items`` / ``verified_qtd`` are the UNCAPPED SQL aggregates
    (savings_summary_quarter) — the headline run-rate and quarter figures; ``totals`` (pandas over the
    row-capped ledger frame) only feeds ratios, counts and per-item disclosure."""
    _k = round(float(rate), 6)
    if _PROOF_MEMO.get("rate") == _k:
        return _PROOF_MEMO.get("sig")
    ledger = run(mart_sql.savings_ledger(limit=None), page=_PAGE, key="decision_roi_ledger_full",
                 tier="recent", source="SAVINGS_LEDGER (full — all-time/QTD/realization economics)")
    if not ledger.ok:
        _PROOF_MEMO.update(rate=_k, sig=None)
        return None
    # perf: the ledger gate above must run first (it early-returns), but these three scorecard
    # reads are independent + non-probe — co-schedule them into ONE round trip instead of three
    # serial run()s (prefetch-else-run: a missing/failed member falls back to its serial read).
    # sc_precision stays a separate run() to keep its probe=True (classified-absence silencing).
    _sc_pf = run_batch([
        {"key": "sc_quarter", "sql": mart_sql.savings_summary_quarter(),
         "source": "SAVINGS_LEDGER (active run-rate + QTD)"},
        {"key": "sc_appcost", "sql": mart_sql.app_cost_last_30d(),
         "source": "FACT_WAREHOUSE_DAILY (app warehouse, trailing 30d)"},
        {"key": "sc_accept", "sql": mart_sql.action_acceptance(90), "source": "ACTION_QUEUE (decided in 90d)"},
    ], page=_PAGE, tier="recent")
    _sc_pf = _sc_pf if _sc_pf is not None else {}   # house law 8: a None batch is "no prefetch", not {}
    _q = _sc_pf.get("sc_quarter") or run(mart_sql.savings_summary_quarter(), page=_PAGE,
                                         key="sc_quarter", tier="recent",
                                         source="SAVINGS_LEDGER (active run-rate + QTD)")
    _ac = _sc_pf.get("sc_appcost") or run(mart_sql.app_cost_last_30d(), page=_PAGE, key="sc_appcost",
                                          tier="recent",
                                          source="FACT_WAREHOUSE_DAILY (app warehouse, trailing 30d)")
    _acc = _sc_pf.get("sc_accept") or run(mart_sql.action_acceptance(90), page=_PAGE, key="sc_accept",
                                          tier="recent", source="ACTION_QUEUE (decided in 90d)")
    _prec = run(mart_sql.rule_precision(90), page=_PAGE, key="sc_precision",
                tier="recent", source="ALERT_EVENTS resolution kinds", probe=True)
    totals = ledger_totals(ledger.df)
    # review r1: if the whole-ledger summary read fails, fall back to the ledger frame (disclosed on the
    # page as summary_ok False) instead of a false $0.00/mo run-rate
    _q_ok = _q.usable()
    verified_qtd = (safe_float(_q.df.iloc[0].get("VERIFIED_QTD_USD")) if _q_ok
                    else safe_float(totals.get("verified_qtd_usd")))
    # Next-Fifty #3: the ROI numerator is the ACTIVE verified monthly run-rate (verified in the
    # last SAVINGS_ACTIVE_MONTHS months), not this quarter's sum — a quarter-scoped numerator fell
    # to 0x on the first day of every quarter while the trailing-30d run cost did not.
    verified_active = (safe_float(_q.df.iloc[0].get("VERIFIED_ACTIVE_MONTHLY_USD"))
                       if _q_ok else safe_float(totals.get("verified_active_usd")))
    verified_active_items = (int(safe_float(_q.df.iloc[0].get("VERIFIED_ACTIVE_ITEMS")))
                             if _q_ok else int(totals["verified_active_count"]))
    # Next-Fifty #31: what the revert check took out of the active run-rate (the same SQL statement; the
    # ledger-frame fallback applies the same rule). .get(): an older-shaped summary lacks the columns.
    _row = _q.df.iloc[0] if _q_ok else None
    reverted_active_items = (int(safe_float(_row.get("REVERTED_ACTIVE_ITEMS"))) if _row is not None
                             else int(totals.get("reverted_active_count") or 0))
    reverted_active_usd = (safe_float(_row.get("REVERTED_ACTIVE_USD")) if _row is not None
                           else safe_float(totals.get("reverted_active_usd")))
    run_cost = safe_float(_ac.df.iloc[0].get("APP_CREDITS_30D")) * rate if _ac.usable() else 0.0
    sig = {
        "ledger": ledger, "totals": totals, "realization": totals["realization_pct"],
        "roi": roi_multiple(verified_active, run_cost),
        "verified_active": verified_active,
        "verified_active_items": verified_active_items,
        "verified_qtd": verified_qtd,
        "reverted_active_items": reverted_active_items,
        "reverted_active_usd": reverted_active_usd,
        # Proof "Saved to date": whole-ledger SQL aggregates only — None (the card reads "—") when the
        # summary read failed; never a fallback sum over the capped ledger frame, never an ROI input
        "saved": saved_to_date(_q.df) if _q_ok else None,
        "summary_ok": _q_ok,
        "acc": acceptance_summary(_acc.df if _acc.usable() else None),
        "prec": account_precision(_prec.df if _prec.usable() else None),
    }
    _PROOF_MEMO.update(rate=_k, sig=sig)
    return sig


def decision_verdict(rate: float) -> dict:
    """Wave 2 #8: the page-open 'should I worry?' line for Proof. Reuses the Proof section's reads (via
    _proof_signals, cache-shared) and the same proof_verdict, so the hoisted line agrees with the
    section exactly. Returns {} (the shell renders nothing) until the ledger is set up. hasattr-guarded
    st.status so a cold first paint reads as progress, matching the Operations verdict (#7)."""
    _load = (st.status("Reading the proof record…", expanded=False)
             if hasattr(st, "status") else contextlib.nullcontext())
    with _load:
        sig = _proof_signals(rate)
    if sig is None:
        return {}
    proof = proof_verdict(sig["roi"], sig["realization"], sig["acc"]["ACCEPTANCE_PCT"], sig["prec"])
    # The healthy sentence is proof_verdict's own headline, which names only MEASURED facts (and lists what
    # is not measured yet) - the old hard-coded sentence claimed realization, precision and follow-through
    # even while they were unmeasured (owner screenshot 2026-09-24).
    return page_verdict(decision_studio_signals(proof), healthy=proof["headline"])


def _proof_tab(rate: float) -> None:
    """Proof ▸ Proof (v4.597: the Scorecard + ROI, merged): does OVERWATCH work, does it pay for itself,
    and what does each verified saving rest on? Account-wide (SAVINGS_LEDGER has no company grain).
    Every HEADLINE total is a SQL aggregate — the run-rate and quarter figures from
    savings_summary_quarter, the attribution split from ledger_attribution's window columns — so a
    row-capped ledger frame can never shrink them; pandas only shapes ratios, counts and per-item rows.
    Read-only: no write control, and every cross-page doorway is gated on can_open."""
    section_header("Does OVERWATCH earn its keep?", "", "target")
    st.caption("Account-wide — SAVINGS_LEDGER has no company grain, so this record does not narrow to "
               "the Company filter. What the tool recommended, how much the team acted on, what verified "
               "out in dollars vs OVERWATCH's own run cost, and what each saving rests on.")
    # Wave 1 #48 + Wave 2 #8: name the heaviest cold-load, and read the prove-it signals through the
    # SAME shared helper the page-open verdict uses (a memo hit here: zero extra reads).
    _sc_load = (st.status("Reading the proof ledger…", expanded=False)
                if hasattr(st, "status") else contextlib.nullcontext())
    with _sc_load:
        sig = _proof_signals(rate)
    if sig is None:
        empty_state("needs_setup", "Apply the action + savings layer (V051+) to start the proof record.")
        return
    ledger = sig["ledger"]
    totals = sig["totals"]
    roi = sig["roi"]
    acc = sig["acc"]
    prec = sig["prec"]
    verified_active = sig["verified_active"]
    # Per-item attribution + the uncapped attribution split (its own read: it joins ALERT_EVENTS, so it
    # re-colds on alert acks — kept apart from the ledger read, which gates the verdict).
    attr = run(mart_sql.ledger_attribution(), page=_PAGE, key="proof_attribution", tier="recent",
               source="SAVINGS_LEDGER + change registry + REMEDIATION_LOG + ALERT_EVENTS (attribution)",
               probe=True)
    attr_df = attr.df if attr.usable() else None
    split = evidence_split(attr_df)
    carried = carried_realization(ledger_with_attribution(ledger.df, attr_df))
    settle = settle_schedule(ledger.df)

    evcov = None
    _port = run(workbench_sql.workload_portfolio(30, "ALL", 200), page=_PAGE, key="sc_portfolio",
                tier="historical", source="MART_PATTERN_COST_DAILY (evidence coverage)", probe=True)
    if _port.usable():
        _pf = prioritize_workloads(_port.df, rate, 30)
        if not _pf.empty and "EVIDENCE_COVERAGE" in _pf.columns:
            evcov = round(float(_pf["EVIDENCE_COVERAGE"].mean()) * 100, 0)

    # v4.461 P2: the prove-it verdict is hoisted ONCE above the section bar (pages/decision_studio.py
    # → decision_verdict → page_verdict_line); the ROI multiple is this section's focal number.
    hero_metric({
        "label": "Pays for itself",
        "value": (f"{roi['RATIO']:.1f}×" if roi["RATIO"] is not None else "—"),
        "severity": ("ok" if roi["PAYS"] else ("warn" if roi["RATIO"] is not None else "")),
        "delta": (f"{format_usd(roi['VERIFIED_USD'])}/mo verified run-rate vs "
                  f"{format_usd(roi['RUN_COST_USD'])}/mo run cost · "
                  f"{format_usd(sig.get('verified_qtd', 0.0))}/mo added this quarter"
                  if roi["RATIO"] is not None else "run cost or verified $ not measured yet"),
        "delta_color": "off",
        "help": f"Monthly savings run-rate of every item VERIFIED in the last {SAVINGS_ACTIVE_MONTHS} months "
                "(each verified amount is a monthly saving that keeps counting after its quarter, so this "
                "does not reset on the first day of a quarter), as a multiple of OVERWATCH's own "
                "trailing-30-day warehouse run cost (APP_WAREHOUSE credits × rate) — same monthly horizon "
                "on both sides. ≥1× means it pays for itself. A warehouse-setting change the daily change "
                "scan later sees undone (sized back up, a longer or no auto-suspend, more clusters, scaling "
                "back to Standard) leaves the run-rate the day the scan sees it; everything else counts "
                f"until it is {SAVINGS_ACTIVE_MONTHS} months old."})

    _real = totals["realization_pct"]
    _no_est = int(totals.get("verified_no_estimate_count") or 0)
    _no_est_auto = int(totals.get("verified_no_estimate_auto_count") or 0)
    # Next-Fifty #11 (V153): change-scan rows stay ESTIMATED until their 14-day measured window closes and
    # then settle themselves -- split them out so the delta never asks an operator to hand-verify them.
    _auto_pending = int(totals.get("auto_settle_pending_count") or 0)
    _by_hand = max(0, int(totals["estimated_count"]) - _auto_pending)
    _carried_txt = (f" · carried: {carried['carried_pct']:,.0f}% vs OVERWATCH's own estimate "
                    f"({carried['items']:,} item(s))"
                    if carried and carried.get("carried_pct") is not None else "")
    _next = settle.get("next")
    _settle_bits = []
    if _next is not None:
        _settle_bits.append(f"next ~{_next:%b} {_next.day}")
    if settle.get("overdue"):
        _settle_bits.append(f"{int(settle['overdue']):,} awaiting the daily scan")
    if _by_hand:
        _settle_bits.append(f"{_by_hand:,} more await proof by hand")
    # Next-Fifty #31: realization KEEPS reverted rows (accuracy, not persistence) while verified_count drops
    # them, so "nothing verified yet" keys on both -- never printed beside a real ratio.
    _ver_any = int(totals["verified_count"]) + int(totals.get("reverted_count") or 0)
    # Next-Fifty #31: what the revert check took out of the ACTIVE run-rate -- the uncapped SQL count (sig,
    # savings_summary_quarter; the ledger-frame fallback only when that read failed), never a count over the
    # row-capped ledger frame (review r1 F7).
    _rev_n = int(sig.get("reverted_active_items") or 0)
    # Saved to date: the SQL aggregates only (sig["saved"]; None when the whole-ledger summary failed).
    # Dollars, never "/mo"; never an input to roi_multiple / proof_verdict. A $0 total reads "nothing
    # verified yet" only when nothing is verified (review r1 F2/F6/F21: not beside a verified run-rate).
    _saved_value, _saved_delta = saved_to_date_card(
        sig.get("saved"), verified_any=_ver_any > 0 or int(sig.get("verified_active_items") or 0) > 0)
    kpi_row([
        # D4 (owner screenshot 2026-09-24): every VERIFIED_USD is a MONTHLY saving, so the run-rate reads
        # $/mo, never cumulative dollars. The VALUE is the uncapped SQL aggregate (the ROI numerator).
        {"label": "Verified savings run-rate", "value": f"{format_usd(verified_active)}/mo",
         "severity": "ok" if verified_active else "",
         "delta": (f"active: verified in the last {SAVINGS_ACTIVE_MONTHS} months · "
                   f"{format_usd(totals['verified_usd'])}/mo across all {totals['verified_count']:,} verified item(s)"
                   + (f" · {_rev_n:,} reverted, not counted" if _rev_n else "")),
         "delta_color": "off", "method": "measured",
         "help": "Each verified item is a recurring MONTHLY saving measured after the change (never an "
                 "estimate); this is their sum as a $/month run-rate, not cumulative dollars saved (that is "
                 "Saved to date) — the same figure the ROI multiple divides."},
        # Next-Fifty #31: accrued DOLLARS (whole-ledger SQL), chipped "accrued", not "measured".
        {"label": "Saved to date", "value": _saved_value,
         "delta": _saved_delta, "delta_color": "off", "method": "accrued",
         "help": _SAVED_TO_DATE_HELP},
        {"label": "Added this quarter", "value": f"{format_usd(sig.get('verified_qtd', 0.0))}/mo",
         "method": "measured",
         "help": "Monthly run-rate of the items verified since the quarter began (account clock)."},
        {"label": "Realization rate",
         "value": (f"{_real:,.0f}%" if _real is not None
                   else ("n/a" if _ver_any else "—")),
         "delta": ((f"{format_usd(totals['realized_verified_usd'])} of "
                    f"{format_usd(totals['realized_estimated_usd'])} estimated"
                    + (f" · {_no_est:,} item(s) without an estimate not in the ratio" if _no_est else ""))
                   if _real is not None else
                   (f"no up-front estimate on the {_no_est:,} verified item(s) ({_no_est_auto:,} auto-measured, "
                    f"{_no_est - _no_est_auto:,} verified by hand)"
                    if _ver_any else "nothing verified yet")) + _carried_txt,
         "delta_color": "off",
         "help": "Verified $ as a share of what those items were estimated to save — the honest "
                 "estimate-vs-actual (verified items that carried an estimate). Near 100% means the "
                 "estimates held up; above 100% means realized savings beat the estimate. 'Carried' is a "
                 "SEPARATE figure: the auto-measured row judged against the estimate OVERWATCH recorded "
                 "BEFORE the change (its manual twin, the executed remediation, or the idle alert's $/mo); "
                 "a rejected item that carried an estimate counts as 0 realized. It never replaces the rate."},
    ])
    kpi_row([
        # rows read 4 + 4 (Next-Fifty #31: Saved to date joined the first row, Settling heads this one)
        {"label": "Settling", "value": f"{_auto_pending:,}",
         "delta": (f"{_auto_pending:,} change(s) settle automatically when their 14-day window closes"
                   + (" · " + " · ".join(_settle_bits) if _settle_bits else "")
                   if _auto_pending else
                   (f"{totals['estimated_count']:,} item(s) awaiting proof" if totals["estimated_count"]
                    else "nothing measuring")),
         "delta_color": "off",
         "help": "Rows the daily change scan booked settle themselves on 14 days of measured actuals, "
                 "15–16 days after the change (V153); their dollars join the run-rate only then. Items "
                 "booked by hand are verified on Cost ▸ Optimization & Savings ▸ Remediation & ledger."},
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
    if not sig.get("summary_ok", True):
        # review r2: the fallback (see _proof_signals) is disclosed, and the truncation note below
        # never claims whole-ledger SQL when that read failed
        st.caption("The whole-ledger savings summary could not be read, so the run-rate, Added this "
                   "quarter and ROI figures come from the newest ledger rows read here"
                   + (" (the ledger holds more)." if ledger.truncated else "."))
    elif ledger.truncated:
        st.caption("The ledger holds more items than the newest read here: the run-rate, quarter and "
                   "attribution totals are whole-ledger SQL aggregates, while the realization, settling "
                   "counts and the per-item evidence below cover the newest items only.")
    if totals["superseded_count"]:
        # Next-Fifty #5: one warehouse change booked twice (the app's manual row + the change scan's
        # measured row) — the manual twin is excluded above; say so, and where to clean it up.
        st.caption(md_dollars(
            f"{totals['superseded_count']:,} manual booking(s) "
            f"({format_usd(totals['superseded_estimated_usd'])} estimated) were superseded by the "
            "auto-measured change row for the same warehouse change — excluded here so one change is "
            "never counted twice. Clean them up on Cost ▸ Optimization & Savings ▸ Remediation & ledger."))
    if totals.get("volume_confounded_count"):
        # Next-Fifty #11: disclosure only -- the measured dollars are never adjusted for volume.
        st.caption(md_dollars(
            f"{int(totals['volume_confounded_count']):,} verified item(s) "
            f"({format_usd(totals['volume_confounded_usd'])}/mo) whose 14-day measured window shows query "
            "volume outside 0.7–1.3x of baseline — part of that measured delta may be workload, not the "
            "lever. Counted as measured, not adjusted."))
    if verified_active > 0:
        # the SAME uncapped SQL figure as the run-rate KPI and the ROI numerator (review fix: the all-time
        # sum read as a run-rate; v4.597: no longer a pandas sum over the row-capped ledger frame)
        _active_n = int(sig.get("verified_active_items") or 0)
        _older = max(0, int(totals["verified_count"]) - _active_n)
        _avgd = totals["avg_days_to_verify"]
        st.markdown(md_dollars(
            f"OVERWATCH has verified **{format_usd(verified_active)}/mo** of active savings run-rate "
            f"across **{_active_n:,}** item(s) verified in the last {SAVINGS_ACTIVE_MONTHS} months"
            + (f" ({_older:,} older item(s) no longer counted)" if _older > 0 else "")
            + (f" ({_rev_n:,} reverted item(s) no longer counted)" if _rev_n else "")
            + (f", realizing **{_real:,.0f}%** of what they were estimated to save"
               if _real is not None else "")
            + (f", closing the loop in **{_avgd:g} days** on average." if _avgd is not None else ".")
            + f" **{format_usd(totals['estimated_usd'])}** more is estimated, awaiting proof."))
    elif int(sig.get("verified_active_items") or 0) > 0:
        # review r2: active items that total $0 (a hand-verified "saved nothing") are not "none verified"
        st.caption(md_dollars(
            f"{int(sig['verified_active_items']):,} item(s) verified in the last {SAVINGS_ACTIVE_MONTHS} "
            f"months, totalling {format_usd(verified_active)}/mo. "
            f"{format_usd(totals['estimated_usd'])} more is estimated, awaiting proof."))
    elif int(totals["verified_count"]) > 0:
        # verified items exist, none still counting inside the active window (or the summary read failed):
        # never the "nothing verified yet" empty state beside a populated evidence table (review r1)
        if _rev_n > 0:
            # Next-Fifty #31 (review r1 F3/F5): items WERE verified in the window and later undone -- say so,
            # never the age-only "none verified in the last 12 months"
            st.caption(md_dollars(
                f"{int(totals['verified_count']):,} older verified item(s) no longer count toward the "
                f"run-rate (verified more than {SAVINGS_ACTIVE_MONTHS} months ago), and every item verified "
                f"in the last {SAVINGS_ACTIVE_MONTHS} months ({_rev_n:,}) was later undone"
                + (" (see Reverted savings below)" if int(totals.get("reverted_count") or 0) > 0 else "")
                + f". {format_usd(totals['estimated_usd'])} more is estimated, awaiting proof."))
        else:
            st.caption(md_dollars(
                f"{int(totals['verified_count']):,} verified item(s), none verified in the last "
                f"{SAVINGS_ACTIVE_MONTHS} months (older items no longer count toward the run-rate). "
                f"{format_usd(totals['estimated_usd'])} more is estimated, awaiting proof."))
    elif int(totals.get("reverted_count") or 0) > 0:
        # Next-Fifty #31: every verified item was later undone -- never "No savings verified yet" beside the
        # Reverted savings list (realization still reads them)
        st.caption(md_dollars(
            f"{int(totals['reverted_count']):,} verified item(s), all later undone — the daily change scan "
            "saw the warehouse change reversed, so none count toward the run-rate (listed under Reverted "
            f"savings below). {format_usd(totals['estimated_usd'])} more is estimated, awaiting proof."))
    else:
        _cost_ok = can_open("Cost Intelligence")
        empty_state("no_data_yet",
                    f"No savings verified yet — {format_usd(totals['estimated_usd'])} is estimated across "
                    f"{totals['estimated_count']:,} item(s). Changes the daily change scan detects settle "
                    "automatically; verify the rest on Cost ▸ Optimization & Savings to start the track record.",
                    action_label="Open Optimization & Savings" if _cost_ok else "",
                    on_action=_open_savings_ledger if _cost_ok else None,
                    action_key="es_proof_verify")

    # ---- What each saving rests on (per-item evidence) -----------------------------------------
    section_header("What each saving rests on", "", "target")
    if split and split.get("active_usd", 0) > 0:
        _parts = [f"{format_usd(split['executed_usd'])}/mo executed by OVERWATCH",
                  f"{format_usd(split['recommended_usd'])}/mo recommended by OVERWATCH and executed elsewhere",
                  f"{format_usd(split['booked_usd'])}/mo booked in OVERWATCH",
                  f"{format_usd(split['elsewhere_usd'])}/mo detected elsewhere"]
        if split.get("experiment_usd"):
            _parts.append(f"{format_usd(split['experiment_usd'])}/mo from hand-verified experiments")
        _caveats = []
        if split.get("regressed_usd"):
            _caveats.append(f"{format_usd(split['regressed_usd'])}/mo came with a performance regression")
        if split.get("neutral_usd"):
            _caveats.append(f"{format_usd(split['neutral_usd'])}/mo with no measurable performance change")
        if split.get("short_window_usd"):
            _caveats.append(f"{format_usd(split['short_window_usd'])}/mo settled on a short pre-V153 window")
        st.markdown(md_dollars(
            f"Of the **{format_usd(split['active_usd'])}/mo** active run-rate: " + " · ".join(_parts) + "."
            + (" Of it, " + "; ".join(_caveats) + "." if _caveats else "")))
        st.caption("Every saving counts in the ROI whoever made the change — the split says who gets the "
                   "credit. Whole-ledger SQL totals, not a sum of the rows below.")
    elif split:
        st.caption("No active verified run-rate to attribute yet — the rows below show what each booked "
                   "item rests on.")
    elif not attr.ok:
        st.caption("Attribution is unavailable right now — the rows below show the ledger evidence "
                   "without who gets the credit.")
    evid = evidence_rows(ledger.df, attr_df)
    if evid.empty:
        empty_state("no_data_yet", "No ledger items to show evidence for yet.")
    else:
        styled_table(evid, height=320, slug="proof_evidence", sort_label="verified $/mo, largest first",
                     # VERIFIED_USD keeps the _USD auto-format (format_usd, "—" for NULL) — no override.
                     column_config={
                         "WINDOW": st.column_config.TextColumn(
                             "Window", help="Change-scan rows only: the 14-day measured window's state."),
                         "ATTRIBUTION": st.column_config.TextColumn(
                             "Credit", help="Who made the change: executed or recommended by OVERWATCH, "
                                            "booked in the app, or detected elsewhere."),
                         "FLAGS": st.column_config.TextColumn(
                             "Flags", help="Reverted / partly reverted (the change was undone, or a "
                                           "co-attributed change the same daily scan saw with it — one "
                                           "measured window; the saving left the run-rate), "
                                           "volume-confounded, performance regressed / unjudged, or an "
                                           "LBA-1 co-attributed $0 row."),
                     })
    # Next-Fifty #31: what the revert check took out of the run-rate -- the count + $ are the SQL figures
    # (sig, savings_summary_quarter; _rev_n above); the list below is display-only rows, never a total. The
    # list reads the row-capped ledger frame, so its title says so when the frame is truncated, and the
    # caption only points at it when it is there (review r1 F7).
    _rev = reverted_rows(ledger.df)
    if _rev_n:
        st.caption(md_dollars(
            f"{_rev_n:,} verified saving(s) ({format_usd(safe_float(sig.get('reverted_active_usd')))}/mo) left "
            "the run-rate because the daily change scan saw the change undone"
            + (" — see Reverted savings below." if not _rev.empty else ".")))
    if not _rev.empty:
        _rev_title = (f"Reverted savings — {len(_rev):,} change(s) undone"
                      + (" (newest ledger rows)" if ledger.truncated else ""))
        with st.expander(_rev_title):
            styled_table(_rev, height=220, slug="proof_reverted", column_config={
                "REVERTED_AT": st.column_config.DatetimeColumn("Undone (scan saw it)",
                                                               format="MMM DD, YYYY HH:mm"),
                "REVERTED_TO": st.column_config.TextColumn("Undone to"),
                "REVERT": st.column_config.TextColumn("Revert")})
            st.caption("A partial revert may still save part of the measured amount — re-measure it and "
                       "book the residual by hand on Cost ▸ Optimization & Savings ▸ Remediation & ledger. "
                       "Re-applying the change is booked again automatically by the daily change scan.")
    if can_open("Operations") and st.button("Change detail → Operations ▸ Change impact",
                                            key="proof_link_change_impact", type="tertiary"):
        request_navigation("Operations", "Change impact")

    month_df = savings_month_calendar(ledger.df, 12)
    lever_df = savings_by_lever(ledger.df)
    if not month_df.empty:
        # Newly-verified per month (the run-rate ADDED that month) — not the active run-rate above,
        # which is the trailing-12-month sum of these (adversarial review of #3).
        st.markdown("**Newly verified savings — by month** (monthly run-rate added each month; this month so "
                    "far; changes later undone are removed)")
        charts.monthly_bars_usd(month_df, "MONTH_LABEL", "VERIFIED_USD", y_title="$/mo added")
    if not lever_df.empty:
        st.markdown("**Where the realized savings come from — by lever**")
        charts.bar_usd(lever_df, "LEVER", "VERIFIED_USD", "verified $ by lever", top_n=10)
        # Codex-review rec30: the lever chart leads; its numeric breakdown (which duplicated the
        # chart at equal weight) moves into an expander. The CSV export rides along inside.
        with st.expander("Lever breakdown table"):
            styled_table(lever_df, height=220, column_config={
                "VERIFIED_USD": st.column_config.NumberColumn("Verified $", format="$%.2f"),
                "REALIZATION_PCT": st.column_config.NumberColumn("Realization %", format="%.0f%%"),
                "ITEMS": st.column_config.NumberColumn("Items", format="%d"),
            })

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
        if st.button("Priced pipeline → Pipeline", key="sc_link_pipeline", type="secondary"):
            request_navigation("Proof", "Pipeline")
    with _c2:
        # READER has no Alerts page: the doorway renders only for a profile that can open it.
        if can_open("Alerts") and st.button("Per-rule alert precision → Alerts ▸ Rules",
                                            key="sc_link_alerts", type="secondary"):
            request_navigation("Alerts", "Rules")
    result_caption(ledger)
    if attr.ok:
        result_caption(attr, note="attribution: who made each change; totals are whole-ledger window sums")


def _clamp_pct(value: object) -> int:
    return round(max(0.0, min(safe_float(value), 100.0)))


def _projection_defaults(sig: dict | None, carried: dict | None) -> dict:
    """The projection's slider defaults and where each came from. Adoption = the MEASURED acceptance
    rate (Proof ▸ Acted on), realization = the measured realization rate, else the carried realization
    vs OVERWATCH's own estimate; each falls back to a labelled assumption. The confidence floor is
    policy, never measured."""
    acc = (sig or {}).get("acc") or {}
    acc_pct = acc.get("ACCEPTANCE_PCT")
    if acc_pct is not None:
        adoption = _clamp_pct(acc_pct)
        adoption_help = (f"Measured: the team acted on {safe_float(acc_pct):,.0f}% of the recommendations "
                         f"it decided in the last 90 days ({acc.get('DONE_N', 0)} done · "
                         f"{acc.get('DROPPED_N', 0)} dismissed) — Proof ▸ Acted on.")
    else:
        adoption = _ASSUMED_ADOPTION_PCT
        adoption_help = (f"Assumed — nothing decided yet (no action DONE or DROPPED in 90 days), so "
                         f"{_ASSUMED_ADOPTION_PCT}% is a placeholder, not a measurement.")
    real = (sig or {}).get("realization")
    carried_pct = (carried or {}).get("carried_pct")
    if real is not None:
        realization = _clamp_pct(real)
        realization_help = (f"Measured: verified items realized {safe_float(real):,.0f}% of their up-front "
                            "estimates — Proof ▸ Realization rate.")
    elif carried_pct is not None:
        realization = _clamp_pct(carried_pct)
        realization_help = (f"Measured (carried): auto-measured changes realized {safe_float(carried_pct):,.0f}% "
                            "of the estimate OVERWATCH recorded before the change "
                            f"({int((carried or {}).get('items') or 0):,} item(s)).")
    else:
        realization = _ASSUMED_REALIZATION_PCT
        realization_help = (f"Assumed — no verified item carries an estimate yet, so "
                            f"{_ASSUMED_REALIZATION_PCT}% is a placeholder, not a measurement.")
    return {
        "adoption": adoption, "adoption_help": adoption_help,
        "realization": realization, "realization_help": realization_help,
        "conf_floor": _CONFIDENCE_FLOOR,
        "conf_help": ("Policy, not a measurement: an item counts only at or above this 0–1 confidence — "
                      "authored on queued actions; the advisor's evidence weight on addressable savings "
                      "(MEDIUM 0.6, LOW 0.3)."),
    }


@st.fragment
def _pipeline_projection(frame: pd.DataFrame, defaults: dict) -> None:
    """Fragment: the Proof ▸ Pipeline projection. A slider move reruns ONLY this block and issues
    zero reads — the de-duplicated pipeline frame and the measured defaults are computed in the full
    run and passed in. It never calls _proof_signals (the per-render memo is reset only at the page
    top, which a fragment rerun skips).

    Floor-compat (streamlit 1.52.2): each slider key is SEEDED into session state only when absent and
    the widget takes no value=, so a later measured default never collides with a pre-seeded key;
    "Reset to measured" rewrites the keys in an on_click callback, i.e. before the widgets render."""
    seed = {"proof_adoption": defaults["adoption"], "proof_realization": defaults["realization"],
            "proof_conf_floor": defaults["conf_floor"]}
    for _key, _value in seed.items():
        if _key not in st.session_state:
            st.session_state[_key] = _value

    def _reset_to_measured() -> None:
        for _k, _v in seed.items():
            st.session_state[_k] = _v

    c1, c2, c3 = st.columns(3)
    with c1:
        adoption = st.slider("Adoption %", 0, 100, step=1, key="proof_adoption",
                             help=defaults["adoption_help"])
    with c2:
        realization = st.slider("Realization %", 0, 100, step=1, key="proof_realization",
                                help=defaults["realization_help"])
    with c3:
        confidence = st.slider("Confidence floor", 0.0, 1.0, step=0.05, key="proof_conf_floor",
                               help=defaults["conf_help"])
    st.button("Reset to measured", key="proof_reset_measured", type="tertiary",
              on_click=_reset_to_measured,
              help="Put the sliders back on the measured values (or the labelled assumptions).")
    projection = scenario_projection(
        frame, adoption_pct=adoption, realization_pct=realization, confidence_floor=confidence,
    )
    has_candidates = projection["candidates"] > 0
    # Distinguish "no eligible entities" from "eligible but UNPRICED": scenario_projection counts an
    # item as a candidate on status + confidence alone, so eligible-but-unpriced work (a security
    # decision, a tracked query family) yields candidates > 0 with gross == 0. Rendering that as
    # "$0.00" reads as "worth nothing" when the dollars are unquantified, not zero (ds-hunt 2026-08-30).
    _priced = has_candidates and projection["gross_estimate"] > 0

    def _capture(value: float) -> str:
        if not has_candidates:
            return "No evidence"
        return format_usd(value) if _priced else "Unpriced"

    kpi_row([
        {"label": "Eligible entities", "value": f"{projection['candidates']:,.0f}",
         "help": "Addressable and queued items at or above the confidence floor, one per entity."},
        {"label": "In play $/mo", "value": _capture(projection["gross_estimate"]),
         "help": "The eligible items' monthly estimates, de-duplicated by entity, before the haircuts."},
        {"label": "Expected capture $/mo",
         "value": _capture(projection["expected_capture"]),
         "delta": (f"{format_usd(projection['low_capture'])} to "
                   f"{format_usd(projection['high_capture'])}") if _priced else None,
         "delta_color": "off",
         "help": "In play × adoption × realization; the range moves realization ±20 points. A model of "
                 "what is ahead, never a verified saving."},
    ])


def _pipeline_tab(company: str, days: int, rate: float, *, bounds: tuple | None = None) -> None:
    """Proof ▸ Pipeline (v4.597; replaces Scenarios): the priced work AHEAD. Addressable $/mo scopes to
    Company and Window; queued work is every open item for the Company (not windowed). Addressable $/mo
    is the Cost ▸ Optimization & Savings idle-timer rollup built from the SAME mart read
    (SQL + tier, so the cache is shared) — mart-only, never the live fallback; right-sizing joins it only
    behind a toggle, like Optimize; unread maintenance joins only from the Storage & waste session handoff
    (zero reads here). Queued work is the open ACTION_QUEUE normalised to $/mo by PERIOD.
    The two are unioned and de-duplicated by entity, then a fragment projects them with measured
    defaults. Read-only; the only doorway (a row's Entity 360) is gated on can_open."""
    _lm = "_lm" if bounds is not None else ""
    section_header("What's ahead — the priced pipeline", "", "target")
    # A memo hit: the page-open verdict already computed the proof signals this render (zero reads).
    sig = _proof_signals(rate)

    # ---- Addressable $/mo: the Cost ▸ Optimization & Savings idle rollup (mart only) ----------
    idle = run(mart27_sql.eff_idle_analysis(days, company, bounds=bounds), page=_PAGE,
               key=f"proof_idle_{company}_{days}{_lm}", tier="hourly",
               source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart, refreshed every 4h; today up to 4h behind)")
    _whs_cache: list[pd.DataFrame] = []

    def _warehouse_settings() -> pd.DataFrame:
        # SHOW WAREHOUSES carries the current AUTO_SUSPEND / size — eligibility, not decoration. Shared
        # (key + tier) with Cost ▸ Optimization & Savings, Operations and the sidebar jump box; read
        # at most once here.
        if not _whs_cache:
            _whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                       tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
            _whs_cache.append(_whs.df if (_whs.ok and not _whs.empty) else pd.DataFrame())
        return _whs_cache[0]

    opps = []
    # W12 (review r2): Current month / Current year pass a day OFFSET (Sep 2 MTD = 1); divide by the
    # bounds' day SPAN, as Operations ▸ Optimize and Cost ▸ Optimization & Savings do.
    _span = (bounds[1] - bounds[0]).days if bounds is not None else days
    _idle_days = _span
    if idle.usable():
        _idle_days = served_days(idle, _span)
        opps.extend(idle_opportunities(
            idle_advisor(with_auto_suspend_settings(idle.df, _warehouse_settings()), rate, _idle_days)))
    _sizing = st.toggle("Include right-sizing (mart profile)", key="proof_pipe_sizing",
                        help="Adds the Cost ▸ Optimization & Savings right-sizing opportunities from the efficiency "
                             "mart (one extra mart read). Off, the addressable figure is idle-timer only — "
                             "the same default Cost ▸ Optimization & Savings shows.")
    _sized_ok = False
    if _sizing:
        prof = run(mart27_sql.eff_sizing_profile(days, company, bounds=bounds), page=_PAGE,
                   key=f"proof_sizing_{company}_{days}{_lm}", tier="hourly",
                   source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart — p95 is peak daily)")
        if prof.usable():
            _sized_ok = True
            sized = size_recommendations(with_warehouse_settings(prof.df, _warehouse_settings()), rate,
                                         served_days(prof, _span))
            opps.extend(resize_opportunities(sized))
    # Next-Fifty #35: unread maintenance joins only from the Storage & waste session handoff (a confirmed scan,
    # this Company, Database filter clear, same cache scope, under 1h old): zero reads, no confirm run here.
    _unread = unread_lever(st.session_state.get(UNREAD_HANDOFF_KEY), company=company, scope=cache_scope(),
                           now=account_now(), where="Cost ▸ Optimization & Savings ▸ Storage & waste")
    opps.extend(_unread.opportunities)
    roll = rollup_savings(opps)

    # ---- Queued work: the open ACTION_QUEUE, normalised to $/mo -------------------------------
    actions = run(workbench_sql.action_center(company, False, 500, with_totals=True), page=_PAGE,
                  key=f"proof_queue_{company}", tier="recent",
                  source="ACTION_QUEUE with confidence and entity keys")
    queued, qsum = monthly_equivalent(actions.df if actions.usable() else None)
    # the headline reads the SQL window totals (every open item, uncapped); qsum (over the <=500 rows)
    # feeds only the projection (review r1: never a headline sum over the capped frame)
    _qt = dict(qsum)
    if actions.usable() and "OPEN_TOTAL" in actions.df.columns:
        _r0 = actions.df.iloc[0]
        _qt.update(items=int(safe_float(_r0.get("OPEN_TOTAL"))),
                   monthly_usd=safe_float(_r0.get("QUEUED_MONTHLY_TOTAL")),
                   unpriced_count=int(safe_float(_r0.get("UNPRICED_TOTAL"))),
                   one_time_count=int(safe_float(_r0.get("ONE_TIME_TOTAL"))),
                   unspecified_count=int(safe_float(_r0.get("NO_PERIOD_TOTAL"))))
    pipeline = pipeline_frame(roll.items, queued if actions.usable() else None)

    # ---- Settling (the ledger evidence already in hand: zero reads) ---------------------------
    # The count is ledger_totals' auto_settle_pending_count — the SAME figure as Proof ▸ Settling;
    # settle_schedule adds only the next settle day.
    settle = settle_schedule(sig["ledger"].df) if sig is not None else {}
    _pending = int(sig["totals"].get("auto_settle_pending_count") or 0) if sig is not None else 0
    _next = settle.get("next")

    _counted = [lever for lever, on in (("IDLE", idle.usable()), ("RESIZE", _sized_ok),
                                        ("UNREAD_MAINT", _unread.included)) if on]
    _absent: dict[str, str] = {}
    if not idle.ok:
        _absent["IDLE"] = "the efficiency mart could not be read"
    elif idle.empty:
        _absent["IDLE"] = "no warehouse metering in this window"
    if not _sizing:
        _absent["RESIZE"] = "turn on 'Include right-sizing (mart profile)' above"
    elif not _sized_ok:
        _absent["RESIZE"] = "the sizing mart returned no rows or could not be read"
    if not _unread.included:
        _absent["UNREAD_MAINT"] = _unread.reason
    _basis = lever_short(_counted)
    if not idle.ok:
        _addr = {"label": "Addressable $/mo", "value": "—", "delta": "efficiency mart unavailable",
                 "delta_color": "off"}
    elif idle.empty:
        _addr = {"label": "Addressable $/mo", "value": "—",
                 "delta": "no warehouse metering in this window", "delta_color": "off"}
    else:
        _addr = {"label": "Addressable $/mo", "value": format_usd(roll.total_monthly_usd),
                 "delta": (f"{len(roll.items):,} opportunit{'y' if len(roll.items) == 1 else 'ies'} · {_basis}"
                           + ("" if not _warehouse_settings().empty else " · AUTO_SUSPEND unverified")),
                 "delta_color": "off",
                 "help": "The Cost ▸ Optimization & Savings addressable net, de-duplicated: idle-timer savings "
                         "per warehouse (net of the resume tail) measured over "
                         f"{window_phrase(bounds, _idle_days)} of the efficiency mart; right-sizing when it is "
                         "included (the same warehouse counts once, the larger wins); and unread maintenance only "
                         "when Cost ▸ Optimization & Savings ▸ Storage & waste confirmed it against access history "
                         "this session, for this Company with the Database filter clear (the last 30 complete "
                         "days of maintenance, less objects already booked on the Savings ledger). The caption "
                         "below names the levers counted. Estimates, not verified savings."}
    kpi_row([
        _addr,
        ({"label": "Queued work $/mo", "value": format_usd(_qt["monthly_usd"]),
          "delta": (f"{_qt['items']:,} open · {_qt['unpriced_count']:,} unpriced · "
                    f"{_qt['one_time_count']:,} one-time"
                    + (f" · {_qt['unspecified_count']:,} no period" if _qt["unspecified_count"] else "")
                    + (f" · top {qsum['items']:,} projected" if _qt["items"] > qsum["items"] else "")),
          "delta_color": "off",
          "help": "Open Action Center estimates as a monthly run-rate: MONTHLY as-is, ANNUAL ÷ 12. One-time "
                  "and period-less estimates are counted but kept out of the run-rate."}
         if actions.ok else
         {"label": "Queued work $/mo", "value": "—", "delta": "action queue unavailable",
          "delta_color": "off"}),
        {"label": "Settling",
         "value": (f"{_pending:,}" if sig is not None else "—"),
         "delta": ((f"measuring · next ~{_next:%b} {_next.day}" if _pending and _next is not None
                    else ("measuring" if _pending else "nothing measuring"))
                   if sig is not None else "ledger not set up"),
         "delta_color": "off",
         "help": "Changes the daily scan booked that are still inside their 14-day measured window. "
                 "Their measured $ joins Proof's verified run-rate when the window closes — it never "
                 "enters this projection."},
    ])
    st.caption(md_dollars(lever_basis(_counted, _absent, {"UNREAD_MAINT": _unread.note})))
    if not _unread.included and can_open("Cost Intelligence") and st.button(
            "Check unread maintenance → Cost ▸ Optimization & Savings ▸ Storage & waste",
            key="proof_link_unread", type="tertiary"):
        _open_storage_waste()
    if not idle.ok:
        empty_state("unavailable", "The warehouse-efficiency mart could not be read — addressable "
                                   "savings are not sized (no live fallback on this page).",
                    detail=idle.error)
    if not actions.ok:
        empty_state("unavailable", "The action queue could not be read — queued work is not projected.",
                    detail=actions.error)
    if actions.ok and len(actions.df) >= 500:
        # Disclose the fetch cap so a >500-action queue's projection is not read as complete. The
        # lowest-severity / lowest-$ actions are the ones the ORDER BY drops (ds-hunt 2026-08-30).
        st.caption(
            "Projecting the top 500 open actions by severity, then overdue, then estimate — more "
            "exist in this scope; narrow the Company to bring the rest into the projection."
        )
    st.caption(
        "Queued estimates are normalised to a monthly run-rate from each action's PERIOD (monthly as-is, "
        "annual ÷ 12); one-time and period-less estimates are listed but kept out of the run-rate, and "
        "an unpriced action projects nothing. Addressable and queued work on the same warehouse or object is "
        "de-duplicated by entity (the larger $/mo counts once) before the adoption and realization "
        "haircuts. Verified savings never enter the projection."
    )
    if pipeline.empty:
        empty_state("no_data_yet",
                    "Nothing to project yet — no addressable savings from the levers counted in this scope "
                    "and no open actions. Create actions on Action Center, or widen the Window, to size a plan.")
        if idle.ok:
            result_caption(idle)
        return

    carried = None
    if sig is not None and sig.get("realization") is None:
        # No realization rate yet (auto-booked rows carry no up-front estimate): default the slider to
        # the carried realization instead. The same read + key Proof ▸ Proof uses (cache-shared).
        _attr = run(mart_sql.ledger_attribution(), page=_PAGE, key="proof_attribution", tier="recent",
                    source="SAVINGS_LEDGER + change registry + REMEDIATION_LOG + ALERT_EVENTS (attribution)",
                    probe=True)
        carried = carried_realization(
            ledger_with_attribution(sig["ledger"].df, _attr.df if _attr.usable() else None))
    _pipeline_projection(pipeline, _projection_defaults(sig, carried))

    # DS #1: pin items on watched entities to the top WITHIN their severity band, so a watched
    # entity's item surfaces first without burying a CRITICAL under a watched LOW. The watchlist and
    # queue carry write-invalidation salts, so the "recent" tier stays correct after a Watch click.
    _viewer = viewer_name()
    _wl_res = run(workbench_sql.watchlist(_viewer), page=_PAGE,
                  key="proof_pipeline_watchlist", tier="recent", source="USER_WATCHLIST"
                  ) if _viewer else None
    _wl = _wl_res.df if (_wl_res is not None and _wl_res.usable()) else None
    adf = pipeline.copy()
    adf["WATCHED"] = mark_watched_pairs(adf, _wl, "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY")
    # DS #34: flag open actions untouched for 30+ days — the plan was made and forgotten, so its
    # estimate is decaying. (Addressable rows are measured fresh each window: never stale.)
    adf["STALE"] = stale_planning(adf, account_now())
    _n_stale = int(adf["STALE"].sum())
    if _n_stale:
        st.caption(f"⚠ {_n_stale} open action(s) not touched in 30+ days — re-estimate, "
                   "act, or close them; a stale plan's estimate is decaying.")
    _sev_rank = adf["SEVERITY"].astype(str).str.upper().map(
        {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}).fillna(4)
    adf = (adf.assign(_SR=_sev_rank)
           .sort_values(["_SR", "WATCHED"], ascending=[True, False], kind="stable")
           .drop(columns="_SR").reset_index(drop=True))
    if bool(adf["WATCHED"].any()):
        st.caption(f"★ {int(adf['WATCHED'].sum())} item(s) on your watched entities, "
                   "pinned to the top of their severity band.")
    display = adf[[column for column in (
        "WATCHED", "STALE", "KIND", "SEVERITY", "TITLE", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY",
        "CONFIDENCE", "MONTHLY_USD", "AUTHORED_USD", "PERIOD", "OWNER", "DUE_DATE",
    ) if column in adf.columns]].copy()
    _pipe_cfg = {}
    if "WATCHED" in display.columns:          # F59: one star, not raw True/False
        display["WATCHED"] = display["WATCHED"].map(watch_star)
        _pipe_cfg["WATCHED"] = watch_star_column()
    if "CONFIDENCE" in display.columns:       # F60: one confidence encoding — a bar
        _pipe_cfg["CONFIDENCE"] = confidence_progress_column(
            "Confidence",
            AUTHORED_CONFIDENCE_HELP + " Addressable rows carry the Cost ▸ Optimization & Savings advisor's "
            "evidence "
            "weight instead (MEDIUM 0.6, LOW 0.3) — an evidence score, not an authored belief.")
    _sort = "severity band (watched first), then queue order; addressable savings after the queued work"
    if can_open("Control Room"):
        def open_pipeline_entity(index: int) -> None:
            row = adf.iloc[int(index)]
            kind = row.get("SOURCE_ENTITY_TYPE")
            key = row.get("SOURCE_ENTITY_KEY")
            if pd.notna(kind) and pd.notna(key) and str(kind).strip() and str(key).strip():
                _open_entity(str(kind).strip(), str(key).strip())

        # A row click opens the item's SOURCE entity in Control Room ▸ Entity 360 (no-op without one).
        selectable_nav_table(
            display, key="proof_pipeline_table", on_select=open_pipeline_entity,
            height=320, sort_label=_sort, column_config=_pipe_cfg,
        )
    else:
        # EXECUTIVE has no Control Room: the table stays, the dead drill doesn't.
        styled_table(display, height=320, sort_label=_sort, column_config=_pipe_cfg)
    # a neutral label (review r2): the page is read-only, and a READER lands where Track is not offered
    if can_open("Operations") and st.button("Open the query fix queue → Operations ▸ Optimize",
                                            key="proof_link_optimize", type="tertiary"):
        request_navigation("Operations", "Optimize")
    if idle.ok:
        result_caption(idle, note="addressable: idle-timer advisor over the mart (Cost ▸ Optimization & "
                                  "Savings' own)")
    if actions.ok:
        result_caption(actions)
