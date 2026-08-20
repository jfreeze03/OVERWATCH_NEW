"""Cost & Contract — the Spend & Attribution section bodies (spend by service,
cloud-services health, company attribution).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

import pandas as pd
import streamlit as st

from app.config import MAX_LIVE_WINDOW_DAYS
from app.core.query import run, run_batch
from app.data import app_cost_sql, cost_sql, insights_sql, mart27_sql, mart_sql
from app.data.common import resolve_effective_window
from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    anomaly_summary,
    complete_days_only,
    flag_anomalies,
    suppress_expected_spikes,
)
from app.logic.anomaly_explain import explain_by_warehouse
from app.logic.cost_coverage import (
    SERVICE_CATEGORY,
    drill_ready_spend_share,
    service_category,
    service_coverage_inventory,
)
from app.logic.directory import resolve_display
from app.logic.formulas import (
    account_today,
    credits_to_usd,
    egress_effective_rate_per_tb,
    format_credits,
    format_usd,
    humanize_bytes,
    md_dollars,
    pct_delta,
    safe_float,
)
from app.ui import charts
from app.ui.components import (
    empty_state,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    panel_help,
    result_caption,
    run_mart_first,
    section_header,
    selectable_table,
    served_days,
    styled_table,
    user_display_map,
    with_user_name_parts,
    with_user_names,
)

_PAGE = "Cost & Contract"

_SERVICE_CATEGORY = SERVICE_CATEGORY


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

def _categorize(service: str) -> str:
    return service_category(service)


def _spend_attr_recent_jobs(company: str, days: int) -> list[dict]:
    """perf #15: the four INDEPENDENT tier='recent' mart reads that gate the
    eager Spend + Attribution first paint, so cost.py can submit them as one
    parallel run_batch instead of four serial round-trips. Each panel keeps its
    own live/historical fallback for a cold or missing mart (the batch carries
    only the mart leg). `daily` is fixed at 30d exactly as the panel reads it."""
    return [
        {"key": "metering", "sql": mart_sql.fact_metering_by_service(days),
         "source": "FACT_METERING_DAILY (mart, loaded hourly)"},
        {"key": "csr", "sql": mart_sql.fact_cloud_services_ratio(days, company),
         "source": "FACT_WAREHOUSE_DAILY (cloud-services share)"},
        {"key": "wh", "sql": mart_sql.fact_warehouse_window_vs_prior(days, company),
         "source": "FACT_WAREHOUSE_DAILY (window vs prior, loaded hourly)"},
        {"key": "daily", "sql": mart_sql.fact_warehouse_daily(30, company),
         "source": "FACT_WAREHOUSE_DAILY"},
        # v4.157.0: CoCo (Cortex Code) spend for the Spend-tab KPI tile. Account-wide
        # (ALL) to match the '(account)' basis of the other tiles; a small day×source
        # aggregate off FACT_AI_USAGE_DAILY, so it rides the same batch, not a serial
        # round-trip.
        {"key": "coco", "sql": mart27_sql.ai_code_daily(days, "ALL"),
         "source": "FACT_AI_USAGE_DAILY (Cortex Code Snowsight+CLI, daily loader)"},
    ]


def _spend_tab(company: str, days: int, rate: float, ai_rate: float, database: str = "",
               *, metering_res=None, csr_res=None, coco_res=None) -> None:
    # Hot path: the daily metering fact carries the same columns; fall back
    # to live ACCOUNT_USAGE only when the fact has no rows yet. metering_res is
    # the prefetched batch result (perf #15); None -> read it serially here.
    #
    # rec47: when the prefetch is unavailable (cost.py's batch -> None), the
    # spend-attribution eager reads run behind Streamlit's bare skeleton on a
    # cold first paint, which reads as a hang. Wrap the heaviest read behind a
    # collapsed st.status so the wait reads as progress; degrade to a no-op
    # context on Streamlit builds without st.status (same hasattr degrade
    # pattern as Control Room). What is read and the live fallback are unchanged.
    # Show the status ONLY on the fallback (metering_res is None): that is when
    # the reads run serially behind the bare skeleton. On the fast prefetched path
    # the read is already served, so a status here would just flash a no-op.
    _fallback = metering_res is None
    _load_status = (
        st.status("Loading Spend & Attribution…", expanded=False)
        if (_fallback and hasattr(st, "status")) else contextlib.nullcontext())
    with _load_status:
        res = metering_res if metering_res is not None else run(
            mart_sql.fact_metering_by_service(days), page=_PAGE, key=f"metering_fact_{days}",
            tier="hourly", source="FACT_METERING_DAILY (mart, loaded hourly)")
        if not res.ok or res.empty:
            res = run(cost_sql.metering_daily_by_service(days), page=_PAGE, key=f"metering_{days}",
                      tier="historical", source="ACCOUNT_USAGE.METERING_DAILY_HISTORY")
    if not guard(res, "No metering rows in this window yet (the view lags up to 24h)."):
        return
    panel_help(
        "Account-wide credit-billed services split by warehouse, serverless, and AI/Cortex: "
        "billed credits x configured rates with the cloud-services rebate applied. This is a "
        "modeled credit-spend view, not the full invoice: storage, transfer, marketplace, and "
        "organization currency adjustments remain in the org rate-card panels."
    )
    df = res.df.copy()
    df["CATEGORY"] = df["SERVICE_TYPE"].map(_categorize)
    df["RATE"] = df["CATEGORY"].map(lambda c: ai_rate if c == "AI / Cortex" else rate)
    df["USD"] = df["CREDITS_BILLED"].map(safe_float) * df["RATE"]
    # The cloud-services adjustment is a compute-side rebate; price it at the
    # compute rate regardless of the row's service category (a non-zero
    # adjustment on an AI/Cortex row would otherwise mis-price at ai_rate).
    df["ADJ_USD"] = df["CREDITS_ADJUSTMENT"].map(safe_float) * rate

    billed_usd = float(df["USD"].sum())
    rebate_usd = float(df["ADJ_USD"].sum())  # negative or zero
    total_credits = float(df["CREDITS_BILLED"].map(safe_float).sum())
    # v4.157.0: the two static rate tiles (compute/cortex rate — just SETTINGS
    # echoes) carried no daily signal. Replace with total credits (free from the
    # frame already read) and CoCo spend (Cortex Code, a near-real-time line
    # billed separately from metering). Rates still live in the "why totals
    # differ" expander below and on Admin.
    coco_usd = None
    if coco_res is None:
        coco_res = run(mart27_sql.ai_code_daily(days, "ALL"), page=_PAGE,
                       key=f"coco_spend_{days}", tier="hourly",
                       source="FACT_AI_USAGE_DAILY (Cortex Code Snowsight+CLI, daily loader)")
    if coco_res is not None and coco_res.usable() and "TOTAL_CREDITS" in coco_res.df.columns:
        coco_usd = credits_to_usd(float(coco_res.df["TOTAL_CREDITS"].map(safe_float).sum()), ai_rate)
    # rec #8: the all-in invoice total (org rate card) for the same window — the
    # storage / transfer / marketplace / adjustments the metering credit-spend tile
    # structurally omits, so the headline reconciles to the invoice. Degrades quietly
    # (org visibility required); org data is UTC and lags ~72h so it trails near today.
    allin_res = run(cost_sql.org_all_in_window_usd(days), page=_PAGE, key=f"org_allin_{days}",
                    tier="historical",
                    source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (this account, all-in)")
    _tiles = [
        {"label": f"Credit spend, {days}d (account)", "value": format_usd(billed_usd),
         "help": "Billed credits x configured rates, including the cloud-services adjustment. "
                 "The metering lens only — storage, transfer, and marketplace are in the "
                 "all-in tile; see the org rate card for the invoice total."},
    ]
    if allin_res.usable() and not allin_res.df.empty:
        _ar = allin_res.df.iloc[0]
        _cur = str(_ar.get("CURRENCY") or "USD")

        def _cur_fmt(v: object) -> str:
            # $ for USD (the common case); currency code suffix otherwise (format_usd is $-only).
            return format_usd(safe_float(v)) if _cur.upper() == "USD" else f"{safe_float(v):,.0f} {_cur}"

        _tiles.append({
            "label": f"All-in billed, {days}d ({_cur})",
            "value": _cur_fmt(_ar.get("TOTAL_USD")),
            "help": "The invoice total from ORGANIZATION_USAGE (org rate card) for this account "
                    f"and window — adds storage ({_cur_fmt(_ar.get('STORAGE_USD'))}), "
                    f"transfer ({_cur_fmt(_ar.get('TRANSFER_USD'))}), and marketplace/"
                    f"other ({_cur_fmt(_ar.get('OTHER_USD'))}) that the credit-spend tile "
                    "excludes. Org currency, UTC, lags ~72h so it trails the credit tile near today."})
    _tiles += [
        {"label": "Cloud-services rebate applied", "value": format_usd(abs(rebate_usd)),
         "help": "CREDITS_ADJUSTMENT_CLOUD_SERVICES — the rebate Snowflake applies before billing."},
        {"label": f"Total credits, {days}d", "value": format_credits(total_credits),
         "help": "Billed credits across all services this window (compute + serverless + AI, "
                 "cloud-services rebate applied). Credits are additive; the dollar split is on "
                 "the Credit-spend tile."},
        {"label": f"— of which CoCo, {days}d",
         "value": format_usd(coco_usd) if coco_usd is not None else "—",
         "help": "Cortex Code (Snowsight + CLI) token credits x the configured AI rate, from "
                 "FACT_AI_USAGE_DAILY (loaded daily). rec #7: a NON-ADDITIVE subset already "
                 "inside the Credit-spend and Total-credits tiles — post-V079 CoCo bills as "
                 "SNOWFLAKE_COCO_SNOWSIGHT within METERING_DAILY_HISTORY. Shown here from the "
                 "near-real-time loader for freshness; do NOT add it to the totals on the left. "
                 "'—' until the fact loads."},
    ]
    kpi_row(_tiles)
    st.caption("Account-wide by service (METERING_DAILY_HISTORY has no company grain; company split lives in Attribution).")
    charts.daily_stacked_usd(df, "DAY", "CATEGORY", "USD")
    with st.expander("Why totals differ across pages (and vs Snowsight)"):
        cat_usd = df.groupby("CATEGORY")["USD"].sum().to_dict()
        wh_usd = float(cat_usd.get("Warehouse", 0.0)) + float(cat_usd.get("Warehouse (reader)", 0.0))
        other_usd = float(sum(cat_usd.values())) - wh_usd
        st.markdown(md_dollars(  # $-escape: three dollar amounts in one markdown body
            f"- **This page — configured-rate credit spend ({days}d): {format_usd(billed_usd)}.** "
            "Account-wide credit-billed services with the cloud-services rebate applied; it "
            "excludes storage, transfer, marketplace, and org currency adjustments.\n"
            f"- **Warehouse portion of that billed spend: {format_usd(wh_usd)}** — account-wide, "
            "cloud-services rebate applied, reader metering included. Overview's company KPI "
            "prices UNADJUSTED warehouse usage (no rebate, main-account metering only) — a "
            "different basis, not a slice of this figure: a company scope usually reads below "
            "it, but ALL (or a dominant company) can read slightly above it, by about the "
            f"rebate. The remaining {format_usd(other_usd)} (serverless, AI, replication) has "
            "no warehouse to scope by.\n"
            "- **Snowsight → Cost Management reads higher than both:** it adds storage and data "
            "transfer dollars and prices from USAGE_IN_CURRENCY (list/contract currency), and its "
            "MTD window follows calendar-month boundaries in account time.\n"
            "- **Rate axis:** AI/Cortex credits bill at the configured AI rate, not the compute "
            "rate — this page and the Overview/Brief/Contract/Compare dollar figures all split "
            "the two, as do the seeded budget alerts (pace, forecast) since V061.\n"
            "- Same telemetry, different lenses — each number is exact for its own question."
        ))
    result_caption(res)

    st.markdown("**Cost drill coverage**")
    coverage = service_coverage_inventory(df, rate, ai_rate)
    ready_share = drill_ready_spend_share(coverage)
    material_gaps = coverage[
        (coverage["MATERIALITY"] == "Material")
        & (coverage["DRILL_STATUS"] == "Service total only")
    ] if not coverage.empty else coverage
    kpi_row([
        {
            "label": "Spend with a native drill path",
            "value": f"{ready_share:.1f}%",
            "help": "Share of billed metering dollars backed by a native Snowflake object-level view.",
        },
        {
            "label": "Material coverage gaps",
            "value": str(len(material_gaps)),
            "help": "Service totals above $500 or 1% of spend without a native object-level drill.",
        },
    ])
    styled_table(coverage, height=300, sort_label="$ desc",
                 column_config={"SHARE_PCT": st.column_config.NumberColumn("Share %", format="%.1f%%")})
    st.caption(
        "Coverage describes native attribution capability, not an allocation. Paid Marketplace "
        "charges use organization currency data and appear in the on-demand detail below."
    )

    if st.toggle(
        "Load detailed service attribution",
        key="cost_service_attribution_detail",
        help="Runs only the selected native service-detail query; off by default for a faster paint.",
    ):
        detail = lazy_sections(
            ["Replication", "Egress / data transfer", "Compute pools & notebooks", "Marketplace"],
            key="cost_service_detail_section",
            deep_link=False,
        )
        if detail == "Replication":
            rep = run(
                cost_sql.replication_by_database(days, company, database),
                page=_PAGE,
                key=f"replication_db_{company}_{database}_{days}",
                tier="historical",
                source="DATABASE_REPLICATION_USAGE_HISTORY (native replication detail)",
            )
            if rep.ok and rep.empty:
                # v4.158.0: DATABASE_REPLICATION_USAGE_HISTORY is CURRENT-account
                # only, but replication + data transfer bill on the secondary/DR
                # account — so this credits view is legitimately empty here while
                # the org rate card shows real spend (e.g. PRIMARY_DR REPLICATION).
                # Fall back to org-currency billing truth instead of claiming
                # nothing was recorded (cost rule d: show the currency as-is).
                org = run(
                    cost_sql.org_usage_in_currency(30), page=_PAGE,
                    key="org_replication_fallback", tier="historical",
                    source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (replication + transfer, billed)",
                    probe=True,
                )
                shown = False
                if org.usable() and "SERVICE_TYPE" in org.df.columns:
                    o = org.df.copy()
                    o["SERVICE_TYPE"] = o["SERVICE_TYPE"].astype(str).str.upper()
                    o = o[o["SERVICE_TYPE"].isin(["REPLICATION", "DATA_TRANSFER"])]
                    if not o.empty:
                        ccy = (str(o["CURRENCY"].dropna().iloc[0])
                               if o["CURRENCY"].notna().any() else "USD")
                        rep_amt = float(pd.to_numeric(
                            o.loc[o["SERVICE_TYPE"] == "REPLICATION", "USAGE_IN_CURRENCY"],
                            errors="coerce").fillna(0.0).sum())
                        by = (o.groupby(["ACCOUNT_NAME", "SERVICE_TYPE"], as_index=False)
                                ["USAGE_IN_CURRENCY"].sum()
                                .sort_values("USAGE_IN_CURRENCY", ascending=False))
                        _is_usd = ccy.upper() == "USD"
                        _spend_col = f"Spend ({ccy})"
                        by.columns = ["Account", "Service", _spend_col]
                        kpi_row([
                            {"label": f"Replication (30d, {ccy})",
                             "value": format_usd(rep_amt) if _is_usd else f"{rep_amt:,.2f} {ccy}",
                             "help": "Org rate-card billing truth. The native per-database credits "
                                     "view is empty because replication bills on the secondary/DR "
                                     "account, not this one."},
                            {"label": "Accounts", "value": str(by["Account"].nunique())},
                        ])
                        styled_table(by, height=240, sort_label=f"spend ({ccy}) desc",
                                     column_config={_spend_col: st.column_config.NumberColumn(
                                         _spend_col, format="$%.2f" if _is_usd else "%.2f")})
                        st.caption(
                            "Replication and data transfer bill on the secondary/DR account "
                            "(e.g. PRIMARY_DR); the native per-database credits view only sees the "
                            "current account, so it reads empty. Amounts are org rate-card currency, "
                            "last 30 days (org usage lags up to ~72h)."
                        )
                        result_caption(org)
                        shown = True
                if not shown:
                    empty_state(
                        "clean",
                        "No replication credits recorded on this account. Replication bills on the "
                        "secondary/DR account; enable ORGANIZATION_USAGE to surface its rate-card spend.",
                    )
            elif guard(rep, ""):
                rep_df = rep.df.copy()
                rep_df["USD"] = pd.to_numeric(rep_df["CREDITS"], errors="coerce").fillna(0.0) * rate
                detail_credits = float(rep_df["CREDITS"].sum())
                metered_credits = float(
                    pd.to_numeric(
                        df.loc[df["SERVICE_TYPE"].astype(str).str.upper() == "REPLICATION", "CREDITS_BILLED"],
                        errors="coerce",
                    ).fillna(0.0).sum()
                )
                ratio = detail_credits / metered_credits * 100.0 if metered_credits > 0 else 0.0
                kpi_row([
                    {"label": "Replication detail", "value": format_usd(float(rep_df["USD"].sum()))},
                    {
                        "label": "Detail vs billed credits",
                        "value": f"{ratio:.1f}%" if metered_credits > 0 else "No billed baseline",
                        "help": "Usage-history credits divided by billed metering credits; latency and adjustments can differ.",
                    },
                    {"label": "Databases", "value": str(len(rep_df))},
                ])
                styled_table(rep_df, height=300, sort_label="credits desc")
                st.caption(
                    "Native secondary-database grain. Company and Database filters are applied to "
                    "DATABASE_NAME; this is measured usage, while the headline uses billed credits."
                )
                result_caption(rep)
        elif detail == "Compute pools & notebooks":
            batch = run_batch(
                [
                    {
                        "key": "pools",
                        "sql": cost_sql.compute_pool_usage(days),
                        "source": "SNOWPARK_CONTAINER_SERVICES_HISTORY (native pool detail)",
                    },
                    {
                        "key": "notebooks",
                        "sql": cost_sql.notebook_container_usage(days),
                        "source": "NOTEBOOKS_CONTAINER_RUNTIME_HISTORY (native notebook detail)",
                    },
                ],
                page=_PAGE,
                tier="historical",
            ) or {}
            pools = batch.get("pools")
            notebooks = batch.get("notebooks")
            if pools is not None and pools.ok and pools.empty:
                empty_state("clean", "No Snowpark Container Services credits were recorded in this window.")
            elif pools is not None and guard(pools, ""):
                pool_df = pools.df.copy()
                pool_df["USD"] = pd.to_numeric(pool_df["CREDITS"], errors="coerce").fillna(0.0) * rate
                kpi_row([
                    {"label": "SPCS spend", "value": format_usd(float(pool_df["USD"].sum()))},
                    {"label": "Compute pools", "value": str(pool_df["COMPUTE_POOL_NAME"].nunique())},
                    {"label": "Applications", "value": str(pool_df["APPLICATION_NAME"].nunique())},
                ])
                _pool_sel = selectable_table(pool_df, key="spcs_pool_sel", height=300,
                                             sort_label="credits desc")
                result_caption(pools)
                st.caption("Select a compute pool to see the users driving its cost.")
                # Drill: users on the selected pool. The pool feed itself is metered at
                # the POOL level (no user column), so per-user attribution comes from the
                # notebook-runtime feed — which covers notebook workloads only. A
                # native-app pool (e.g. Posit) has no notebook rows -> honest note.
                # Bounds guard: st.dataframe's selection is sticky and survives a
                # window-narrow that shrinks pool_df, so a stale index must not iloc
                # out of range (would red-traceback the whole page).
                if _pool_sel is not None and 0 <= int(_pool_sel) < len(pool_df):
                    from app.logic.spcs import compute_pool_user_costs
                    _prow = pool_df.iloc[int(_pool_sel)]
                    _pool_name = str(_prow["COMPUTE_POOL_NAME"])
                    _app_name = str(_prow["APPLICATION_NAME"])
                    _pool_usd = safe_float(_prow["USD"])
                    _nb_ok = notebooks is not None and notebooks.usable()
                    _users = compute_pool_user_costs(notebooks.df if _nb_ok else None,
                                                     _pool_name, rate)
                    section_header(f"Users driving {_pool_name}", "info", "chargeback")
                    if _users.empty:
                        # Be honest about WHY there's no per-user split — don't brand every
                        # empty case a "native-app pool" (a user-owned non-notebook pool
                        # shows APPLICATION_NAME='Unassigned'; the notebook feed can also
                        # simply be empty this window).
                        if not _nb_ok:
                            st.info(
                                f"**{_pool_name}** — {format_usd(_pool_usd)} — the per-user drill reads the "
                                "notebook-runtime feed (NOTEBOOKS_CONTAINER_RUNTIME_HISTORY), which has no "
                                "rows for this window, so no per-user split can be shown here."
                            )
                        elif _app_name and _app_name.upper() != "UNASSIGNED":
                            st.info(
                                f"**{_pool_name}** — {format_usd(_pool_usd)}, app **{_app_name}** — is a "
                                "native-app pool, metered by Snowflake at the pool level; the usage views "
                                f"carry no per-user split for it. For **{_app_name}**, use the app's own "
                                "admin/usage console for per-user sessions."
                            )
                        else:
                            st.info(
                                f"**{_pool_name}** — {format_usd(_pool_usd)} — runs non-notebook Snowpark "
                                "Container Services, which Snowflake meters at the pool level (no per-user "
                                "split). Per-user cost is available only for notebook-backed pools."
                            )
                    else:
                        _attributed = float(_users["USD"].sum())
                        kpi_row([
                            {"label": "Users on this pool", "value": str(len(_users))},
                            {"label": "Attributed spend", "value": format_usd(_attributed),
                             "help": "Notebook-runtime credits on this pool, priced at the configured rate — "
                                     "a subset of the pool total when non-notebook services also run here."},
                        ])
                        styled_table(with_user_name_parts(_users, _PAGE), sort_label="credits desc")
                        st.caption("Per-user notebook-runtime cost on this pool "
                                   "(NOTEBOOKS_CONTAINER_RUNTIME_HISTORY).")
            if notebooks is not None and notebooks.ok and notebooks.empty:
                empty_state("no_data_yet", "No notebook container runtime was recorded in this window.")
            elif notebooks is not None and guard(notebooks, ""):
                notebook_df = with_user_name_parts(notebooks.df, _PAGE)
                notebook_df["USD"] = (
                    pd.to_numeric(notebook_df["CREDITS"], errors="coerce").fillna(0.0) * rate
                )
                st.markdown("**Notebook subset**")
                styled_table(notebook_df, height=300, sort_label="credits desc")
                result_caption(notebooks)
            st.caption(
                "Account-wide: these views carry no company key. Notebook credits are a subset of "
                "SPCS credits and must not be added to the compute-pool total."
            )
        elif detail == "Egress / data transfer":
            st.caption("Account-wide — DATA_TRANSFER_HISTORY is region-to-region byte metering "
                       "with no company/warehouse grain, so the company filter does not narrow this.")
            # rec#11: egress was tracked only as a bytes signal on Security and never
            # dollarized. Price billable (cross-region / cross-cloud) transfer from the
            # org rate card's implied $/TB, reconciled to billed TRANSFER_USD; same-
            # region transfer is free and priced at $0.
            eg = run(
                cost_sql.transfer_egress_priced(days),
                page=_PAGE, key=f"egress_priced_{days}", tier="historical",
                source="DATA_TRANSFER_HISTORY (egress bytes by source/target, billable flag)",
            )
            if eg.ok and eg.empty:
                empty_state("clean", "No outbound data transfer was recorded in this window.")
            elif guard(eg, ""):
                # skeptic-verify (rec#11): org billing TRUTH must be read over the SAME
                # window as the egress bytes (not a fixed 30d, or the $/TB and the total
                # skew by 30/days), and via the VERIFIED RATING_TYPE='DATA_TRANSFER' path
                # (org_all_in_window_usd), not a SERVICE_TYPE string match.
                org = run(
                    cost_sql.org_all_in_window_usd(days), page=_PAGE,
                    key=f"org_transfer_truth_{days}", tier="historical",
                    source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (data transfer, verified rating type)",
                    probe=True,
                )
                org_transfer, ccy = 0.0, "USD"
                if org.usable() and not org.df.empty and "TRANSFER_USD" in org.df.columns:
                    _row = org.df.iloc[0]
                    org_transfer = safe_float(_row.get("TRANSFER_USD"))
                    ccy = (str(_row.get("CURRENCY")).upper()
                           if pd.notna(_row.get("CURRENCY")) else "USD")
                edf = eg.df.copy()
                edf["TB"] = pd.to_numeric(edf["TB"], errors="coerce").fillna(0.0)
                edf["BYTES"] = pd.to_numeric(edf["BYTES"], errors="coerce").fillna(0.0)
                edf["BILLABLE"] = edf["BILLABLE"].astype(bool)
                billable_tb = float(edf.loc[edf["BILLABLE"], "TB"].sum())  # rate math is $/TB
                # Owner finding 2026-08-17: TB at 3 decimals rendered sub-GB transfer as
                # "0.000 TB" and there was no TOTAL to reconcile against Snowsight's
                # headline (which sums ALL bytes, billable + free). Humanize + total.
                total_bytes = float(edf["BYTES"].sum())
                billable_bytes = float(edf.loc[edf["BILLABLE"], "BYTES"].sum())
                free_bytes = float(edf.loc[~edf["BILLABLE"], "BYTES"].sum())
                edf["VOLUME"] = edf["BYTES"].map(humanize_bytes)
                fallback = safe_float(load_settings(_PAGE).get("DATA_TRANSFER_USD_PER_TB"), 20.0)
                # only reconcile to org truth when it is USD — folding a non-USD bill into
                # $-denominated KPIs would mislabel the currency. The resolver also rejects
                # an implausibly high implied $/TB (billable under-count) back to the setting.
                org_usd = org_transfer if ccy == "USD" else 0.0
                rate_per_tb, used_org = egress_effective_rate_per_tb(org_usd, billable_tb, fallback)
                edf["EST_USD"] = (edf["TB"] * rate_per_tb).where(edf["BILLABLE"], 0.0).round(2)
                kpi_row([
                    {"label": f"Total transferred ({days}d)",
                     "value": humanize_bytes(total_bytes),
                     "help": "ALL outbound + cross-region bytes, billable and free — the figure "
                             "that reconciles to Snowsight ▸ Cost Management ▸ Consumption ▸ Data "
                             "Transfer for this account. Most of it is usually free same-region "
                             "transfer; only the billable slice below carries a $."},
                    {"label": f"Estimated egress ({days}d)",
                     "value": format_usd(float(edf["EST_USD"].sum())),
                     "help": "Billable (cross-region / cross-cloud) transfer x the $/TB at right. "
                             "Same-region transfer is free and priced at $0 — so this is often "
                             "near $0 even when total transfer is large."},
                    {"label": "Billable transfer", "value": humanize_bytes(billable_bytes),
                     "sub": f"{humanize_bytes(free_bytes)} free (same region)"},
                    {"label": "Effective $/TB", "value": format_usd(rate_per_tb),
                     "method": "org rate card" if used_org else "setting fallback",
                     "help": ("Implied from billed TRANSFER_USD / billable TB over this window, "
                              "so the estimate reconciles to the org bill." if used_org else
                              "DATA_TRANSFER_USD_PER_TB setting — org billing truth in USD was not "
                              "usable (not visible, non-USD, or an implausibly high implied rate "
                              "from a billable under-count), so this is a fallback, not billing truth.")},
                ])
                styled_table(
                    edf[["EST_USD", "BILLABLE", "SOURCE_CLOUD", "SOURCE_REGION",
                         "TARGET_CLOUD", "TARGET_REGION", "TRANSFER_TYPE", "VOLUME", "BYTES"]],
                    height=320, sort_label="estimated $ desc",
                    column_config={
                        "EST_USD": st.column_config.NumberColumn("Est. $", format="$%.2f"),
                        "VOLUME": st.column_config.TextColumn("Volume"),
                        # raw bytes kept for true numeric sort + CSV; Volume is the display.
                        "BYTES": st.column_config.NumberColumn("Bytes", format="%d"),
                    },
                )
                if used_org:
                    st.caption(
                        f"Reconciliation: billed data transfer ({days}d) is {format_usd(org_transfer)} "
                        "on the org rate card; the estimate distributes that across source/target/type "
                        "by billable bytes. BILLABLE is the app's cross-boundary estimate — Snowflake "
                        "owns the exact billing determination."
                    )
                elif org_transfer > 0.0:
                    st.caption(
                        f"Org billed {org_transfer:,.2f} {ccy} for data transfer ({days}d), shown for "
                        "reference; the USD estimate above uses the DATA_TRANSFER_USD_PER_TB setting "
                        "(org bill non-USD, or the implied $/TB was implausibly high because the app's "
                        "cross-boundary billable estimate under-counts). Edit the rate on Admin."
                    )
                else:
                    st.caption(
                        "Priced from the DATA_TRANSFER_USD_PER_TB setting (org billing truth not "
                        "visible). BILLABLE is the app's cross-region / cross-cloud estimate; edit the "
                        "rate on Admin to match your rate card."
                    )
                result_caption(eg)
        else:
            market = run(
                cost_sql.marketplace_paid_usage(days),
                page=_PAGE,
                key=f"marketplace_paid_{days}",
                tier="historical",
                source="ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY",
            )
            if market.ok and market.empty:
                empty_state("clean", "No paid Marketplace charges were recorded for this account.")
            elif guard(market, ""):
                market_df = market.df.copy()
                totals = (
                    market_df.groupby("CURRENCY", dropna=False)["CHARGE"]
                    .sum()
                    .sort_values(ascending=False)
                )
                kpi_row([
                    {
                        "label": f"Marketplace ({currency})",
                        "value": f"{float(amount):,.2f} {currency}",
                    }
                    for currency, amount in totals.head(4).items()
                ])
                styled_table(market_df, height=320, sort_label="charge desc")
                st.caption(
                    "Organization billing truth for this consumer account. Charges remain in their "
                    "native currency and are not converted through the credit-rate setting."
                )
                result_caption(market)

    st.markdown("**Cloud-services health by warehouse**")
    st.caption(
        "Above ~10% (WATCH) of a warehouse's credits usually means many tiny queries, "
        "metadata-heavy patterns, or compile-heavy SQL. ELEVATED starts past 20%, "
        "where the COST_CLOUD_SVC_RATIO alert fires (editable on Alerts)."
    )
    csr = csr_res if csr_res is not None else run(
        mart_sql.fact_cloud_services_ratio(days, company), page=_PAGE,
        key=f"csr_fact_{company}_{days}", tier="hourly",
        source="FACT_WAREHOUSE_DAILY (cloud-services share)")
    if not csr.usable():  # mart not deployed/loaded yet -> bounded live scan
        csr = run(cost_sql.cloud_services_ratio_by_warehouse(days, company), page=_PAGE,
              key=f"cs_ratio_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY")
    if guard(csr, "No warehouse metering in this window."):
        styled_table(csr.df, height=260,
                     column_config={"CLOUD_SVC_PCT": st.column_config.NumberColumn(
                         "Cloud svc %", format="%.1f%%")})
        result_caption(csr)
        elevated = csr.df[csr.df["STATUS"].astype(str) == "ELEVATED"]
        if not elevated.empty:
            st.markdown("**Why is it elevated? Compile-heavy query families**")
            comp = run_mart_first(
                mart27_sql.family_compile_heavy(days, company),
                cost_sql.compile_heavy_families(days, company),
                page=_PAGE, key=f"compile_fams_{company}_{days}",
                mart_source="MART_QUERY_FAMILY_DAILY (mart, run-weighted averages)",
                live_source="ACCOUNT_USAGE.QUERY_HISTORY (COMPILATION_TIME, live fallback)")
            if guard(comp, "No query family with 20+ runs averages >0.5s compile time — "
                           "the ratio driver is likely many tiny/metadata queries instead."):
                styled_table(comp.df)
                result_caption(comp)
            st.markdown("**Cloud-services credits by statement type**")
            # P7: this panel only renders when a warehouse is ELEVATED — precisely the
            # accounts whose QUERY_HISTORY is heaviest, where the live scan measured ~25s.
            # MART_CLOUD_SVC_DAILY already carries per-query cloud-services credits, so
            # read the mart first (K2 contract with Group A: cs_by_query_type_mart emits
            # the same columns as cost_sql.cs_by_query_type) and keep the live scan as the
            # labeled fallback for accounts where V055 isn't deployed/loaded yet.
            cs_types = run_mart_first(
                mart_sql.cs_by_query_type_mart(days, company),
                cost_sql.cs_by_query_type(days, company),
                page=_PAGE, key=f"cs_types_{company}_{days}",
                mart_source="MART_CLOUD_SVC_DAILY (CS credits by QUERY_TYPE, loaded hourly)",
                live_source="ACCOUNT_USAGE.QUERY_HISTORY (CS credits by QUERY_TYPE, live fallback)")
            if guard(cs_types, "No cloud-services credits recorded on queries in this window."):
                styled_table(cs_types.df, height=220)
                result_caption(cs_types)
                # K1: the live builder clamps to MAX_LIVE_WINDOW_DAYS, so on a long page
                # window the fallback answers a SHORTER window than the header implies.
                # served_days() reports what actually got scanned; never re-derive it here.
                _cs_days = served_days(cs_types, days)
                st.caption("Metadata storms show up here — SHOW/DESCRIBE floods bill "
                           "cloud services without ever touching a warehouse."
                           + (f" Scanned {_cs_days}d of the {days}d window (the live "
                              "fallback caps its scan)." if _cs_days != days else ""))

    # V055: shape/user drill-down from MART_CLOUD_SVC_DAILY — for ANY warehouse
    # (not only ELEVATED), no live QUERY_HISTORY scan. Names the exact query
    # shapes and the users/tools burning the cloud-services credits.
    wh_opts = [str(w) for w in csr.df["WAREHOUSE_NAME"].tolist()] if csr.usable() and not csr.df.empty else []
    # perf: the drill-in fires two MART_CLOUD_SVC_DAILY reads; gate it behind a
    # toggle (off by default) so the Spend & Attribution first paint doesn't pay
    # for a breakdown most viewers don't open. The ratio table above stays live.
    if wh_opts and st.toggle("Drill in — cloud-services credits by query shape & user",
                             key=f"cs_drill_toggle_{company}_{days}",
                             help="Loads the per-shape and per-user cloud-services breakdown "
                                  "(2 mart reads) on demand."):
        _ALL = "(all warehouses)"
        pick = st.selectbox("Warehouse", [_ALL, *wh_opts], key=f"cs_drill_wh_{company}_{days}",
                            help="Ranked by cloud-services %, most elevated first. Any warehouse — "
                                 "not only ELEVATED. '(all warehouses)' includes the no-warehouse "
                                 "metadata bucket (WAREHOUSE_NAME resolves to NONE).")
        wh_arg = "" if pick == _ALL else pick
        shapes = run(mart_sql.cloud_svc_top_shapes(days, company, wh_arg), page=_PAGE,
                     key=f"cs_shapes_{company}_{days}_{pick}", tier="hourly",
                     source="MART_CLOUD_SVC_DAILY (per-query CS credits, loaded hourly)")
        if guard(shapes, "No cloud-services credits recorded for this warehouse yet "
                         "(the mart loads hourly; needs V055 deployed)."):
            st.caption("Gross cloud-services credits per shape (before the account-level 10% rebate "
                       "shown above). High RUNS + tiny AVG_EXEC_S + high AVG_CACHE_PCT = a polling / "
                       "metadata storm; a heavy CS_PER_1K_RUNS on a SELECT = a compile-heavy plan. "
                       "That triage is the fix.")
            styled_table(shapes.df, height=300, column_config={
                "CS_CREDITS": st.column_config.NumberColumn("CS credits", format="%.4f"),
                "AVG_CACHE_PCT": st.column_config.NumberColumn("Cache %", format="%d%%")})
            result_caption(shapes)
            users = run(mart_sql.cloud_svc_by_user(days, company, wh_arg), page=_PAGE,
                        key=f"cs_users_{company}_{days}_{pick}", tier="hourly",
                        source="MART_CLOUD_SVC_DAILY (CS credits by user/role)")
            if guard(users, "No per-user cloud-services credits for this warehouse yet."):
                st.markdown("**Who's driving it** (user / role / tool)")
                styled_table(with_user_names(users.df, _PAGE), height=220, column_config={
                    "CS_CREDITS": st.column_config.NumberColumn("CS credits", format="%.4f")})
                result_caption(users)

def _attribution_tab(company: str, days: int, rate: float, database: str = "", schema_contains: str = "",
                     *, wh_res=None, daily_res=None) -> None:
    # wh_res / daily_res are the prefetched batch results (perf #15); None ->
    # read serially here. The live/historical fallbacks below are unchanged.
    wh = wh_res if wh_res is not None else run(
        mart_sql.fact_warehouse_window_vs_prior(days, company), page=_PAGE,
        key=f"wh_vs_prior_fact_{company}_{days}", tier="hourly",
        source="FACT_WAREHOUSE_DAILY (window vs prior, loaded hourly)")
    if not wh.usable():  # mart not deployed/loaded yet -> bounded live scan
        wh = run(cost_sql.warehouse_window_vs_prior(days, company), page=_PAGE,
                 key=f"wh_vs_prior_{company}_{days}", tier="historical",
                 source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (live fallback)")
    st.markdown("**By warehouse (exact usage)**")
    panel_help(
        "Warehouse spend is EXACT metering (credits x rate, company-scopable); the user/database "
        "split below it is an elapsed-time-share ESTIMATE, not billing. Trust the by-warehouse "
        "table for the real breakdown and read the allocated user/database dollars as "
        "directional — the coverage ladder shows how much of the bill each grain actually explains."
    )
    if guard(wh, "No warehouse credits in this window."):
        view = wh.df.copy()
        view["USD_CURRENT"] = view["CREDITS_CURRENT"].map(lambda c: credits_to_usd(c, rate))
        view["USD_PRIOR"] = view["CREDITS_PRIOR"].map(lambda c: credits_to_usd(c, rate))
        view["DELTA_PCT"] = view.apply(lambda r: pct_delta(r["USD_CURRENT"], r["USD_PRIOR"]), axis=1)
        window_usd = float(view["USD_CURRENT"].sum())
        prior_window_usd = float(view["USD_PRIOR"].sum())
        styled_table(  # rec21: + delta sign-coloring, status tint, CSV
            view[["WAREHOUSE_NAME", "COMPANY", "USD_CURRENT", "USD_PRIOR", "DELTA_PCT"]],
            column_config={
                "USD_CURRENT": st.column_config.NumberColumn("Current $", format="$%.2f"),
                "USD_PRIOR": st.column_config.NumberColumn("Prior $", format="$%.2f"),
                "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            },
            totals=(("Current spend", format_usd(window_usd)),
                    ("Prior spend", format_usd(prior_window_usd))),
        )
        # r4: settle the allocated-share vs pool WINDOW mismatch. The live-share
        # fallback scans only <= MAX_LIVE_WINDOW_DAYS of QUERY_HISTORY (a deliberate
        # cost guardrail we KEEP), so for a >90d window its shares are 90d-scoped;
        # multiplying them by the full-window pool mis-attributed older-half spend (an
        # entity active only 91-182d ago got $0 while its dollars stayed in the pool).
        # A LIVE-served dimension now gets a pool matched to the same clamped window —
        # a cheap mart read, fetched once and only when a live dim needs it.
        _pool_eff, _ = resolve_effective_window(days)
        _live_eff, _ = resolve_effective_window(days, max_days=MAX_LIVE_WINDOW_DAYS)
        _live_pool: list[float] = []

        def _alloc_pool(res_source: str) -> float:
            if _live_eff >= _pool_eff or "QUERY_HISTORY" not in str(res_source):
                return window_usd            # mart-served, or the windows already match
            if not _live_pool:
                _wp = run_mart_first(
                    mart_sql.fact_warehouse_window_vs_prior(_live_eff, company),
                    cost_sql.warehouse_window_vs_prior(_live_eff, company),
                    page=_PAGE, key=f"alloc_pool_live_{company}_{_live_eff}",
                    mart_source="pool", live_source="pool")
                _live_pool.append(
                    float(_wp.df["CREDITS_CURRENT"].map(lambda c: credits_to_usd(c, rate)).sum())
                    if _wp.usable() else window_usd)
            return _live_pool[0]

        result_caption(wh, note="Equal-length windows excluding the current partial day for "
                                "completeness. Exact USAGE, not billed: totals include each "
                                "warehouse's idle time and its unadjusted cloud-services credits "
                                "— the account-level rebate lives on the Spend panel. "
                                "Company-wide: the database/schema filters don't narrow this table.")

        # Pre-fetch both allocation dims so the intro caption states the SAME pool the bars
        # use. r5-bug: both dims read the same mart over the same window, so their serving is
        # identical — but a cold mart on a >90d window serves them live (90d pool) while the
        # caption used to guess the full-window pool. Resolve the served pool from the actual
        # path. Keys match the render pass below, so these reads are cached, not doubled.
        def _fetch_alloc(dim: str):
            _alloc_live = cost_sql.allocated_attribution(days, dim, company, database, schema_contains)
            if schema_contains:
                # no allocation mart carries a schema grain — live only
                return run(_alloc_live, page=_PAGE, key=f"alloc_{dim}_{company}_{days}",
                           tier="historical", source="ACCOUNT_USAGE.QUERY_HISTORY (elapsed share)")
            # P0-1/P0-2 (Codex 2026-07-14): BOTH unfiltered and database-filtered attribution
            # read FACT_COST_ALLOC_XDIM_DAILY so company scope is warehouse-based on every
            # path. The owner-scoped MART_COST_ALLOCATION_DAILY made the same user/DB total
            # shift when a database filter was toggled. `database` is "" unfiltered.
            return run_mart_first(
                mart27_sql.alloc_xdim_attribution(days, dim.replace("_NAME", ""), company, database),
                _alloc_live, page=_PAGE, key=f"alloc_{dim}_{company}_{days}",
                mart_source="FACT_COST_ALLOC_XDIM_DAILY (mart — warehouse-hour credit share)",
                live_source="QUERY_HISTORY (elapsed share, live fallback)")

        _dim_res = {dim: _fetch_alloc(dim) for dim in ("USER_NAME", "DATABASE_NAME")}
        # both dims share serving; read the pool off the path that answered.
        _served = _dim_res["USER_NAME"] if _dim_res["USER_NAME"].usable() else _dim_res["DATABASE_NAME"]
        _intro_pool = _alloc_pool(_served.source)

        st.markdown("**By user and database (allocated — estimate)**")
        # $-escape (screenshot bug 2026-07-31): format_usd's '$' paired with 'USER$' below
        # into a LaTeX math span — half this caption rendered in serif-italic math font.
        st.caption(md_dollars(
            "Snowflake bills at warehouse grain. These split the scoped warehouse spend "
            f"({format_usd(_intro_pool)}) by query elapsed-time share; treat as directionally "
            "correct. Shares stay global, so a database/schema filter shows that slice of "
            "the total — never 100% of it. NONE = queries with no database context; "
            "USER$ personal databases attribute to their owner's company. "
            "The mart and live estimates diverge on three axes: (1) TIME BASIS — the mart "
            "shares by EXECUTION time, the live fallback by ELAPSED (wall-clock) time, which "
            "also counts queuing and compilation; (2) STATUS — the mart includes queries of "
            "every status, the live fallback only SUCCEEDED ones; (3) WEIGHTING — the mart "
            "weights each share by that warehouse-hour's credits (size-aware), while the live "
            "fallback (shown while facts load, or whenever a schema filter is set) uses an "
            "unweighted elapsed-time share that is warehouse-size-blind — a coarser estimate "
            "when one entity concentrates on unusually large or small warehouses."
        ))
        col_u, col_d = st.columns(2)
        for col, dim, label in ((col_u, "USER_NAME", "user"), (col_d, "DATABASE_NAME", "database")):
            with col:
                res = _dim_res[dim]
                if guard(res, f"No query history to allocate by {label}."):
                    alloc = res.df.copy()
                    # r4: multiply by the pool over the SAME window the share was
                    # computed on (full for a mart-served dim, <=90d for a live one).
                    _pool = _alloc_pool(res.source)
                    # ONE formula on every path (live math fix 2026-07-11):
                    # share x the window total the caption states. Direct
                    # dollarization of mart credits used a different window
                    # and included idle — SYSTEM alone exceeded the caption.
                    alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"].map(safe_float) * _pool
                    if dim == "USER_NAME":   # show people, not logins (leave DB names as-is)
                        _nm = user_display_map(_PAGE)
                        alloc["DIMENSION"] = [resolve_display(u, _nm) for u in alloc["DIMENSION"]]
                    # rec 12: ONE sorted contribution bar — the old waterfall + bar plotted
                    # the SAME top-10 twice, and the waterfall's cumulative form falsely
                    # implied full reconciliation. Append an explicit "Other / not shown" row
                    # (= the scoped pool minus the shown contributors) so the chart accounts
                    # for 100% of the pool, not just the top rows.
                    _top = (alloc.sort_values("ALLOCATED_USD", ascending=False)
                            .head(10)[["DIMENSION", "ALLOCATED_USD"]])
                    _top_usd = float(_top["ALLOCATED_USD"].map(safe_float).sum())
                    _other = max(0.0, _pool - _top_usd)
                    # codex#32: the coverage caption is computed from the SAME top-10 the chart
                    # draws (and its 'Other' bar), so "Named rows cover X%" reconciles with the
                    # chart. The old `shown` summed ELAPSED_SHARE over ALL ~100 rows while
                    # 'Other' used only the top-10 pool remainder — the two disagreed.
                    shown = (_top_usd / _pool) if _pool else 0.0
                    _bar = (pd.concat([_top, pd.DataFrame(
                        [{"DIMENSION": "Other / not shown", "ALLOCATED_USD": _other}])],
                        ignore_index=True) if _other > 0 else _top)
                    charts.bar_usd(_bar, "DIMENSION", "ALLOCATED_USD",
                                   title=f"Allocated $ by {label}", top_n=11)
                    # $-escape: two dollar amounts in one caption pair into a math span
                    st.caption(md_dollars(f"Top rows cover {shown:.0%} of scoped spend "
                               f"({format_usd(_top_usd)} of {format_usd(_pool)}); "
                               "'Other / not shown' is the remainder of the pool."))

        # rec17: one read-only "coverage ladder" — the per-grain residuals/coverage
        # already exist scattered across surfaces; this consolidates the MAP (which
        # grain explains how much, and where each residual is proven) in one place,
        # anchored on the exact warehouse pool. No new marts/queries.
        with st.expander("Cost coverage ladder — how much of the bill each grain explains"):
            st.markdown(
                f"- **Billed / metered pool (this window):** {format_usd(window_usd)} — the "
                "warehouse-metering total above. **Exact at warehouse grain** (100% covered; "
                "includes each warehouse's idle time + unadjusted cloud-services credits).\n"
                "- **Allocated to user / database:** the elapsed-time (or credit-weighted) "
                "share above — a **directional estimate**. The 'Named rows cover N%' caption on "
                "each chart is its coverage; 'Other / not shown' is the residual.\n"
                "- **Measured-query / object grain:** the **Object cost ledger** (Operations → "
                "Optimize) splits query credits into read / write / **residual** (queries that "
                "touched no base object) and proves *arms + residual = attribution credits* "
                "(the additive-contract recon on Admin).\n"
                "- **Billed-vs-model residual:** the **rate-card reconciliation** (Cost & "
                "Contract → Contract) frames the gap to the invoice as storage / transfer / "
                "serverless / discounts.\n\n"
                "Non-additive tracks (idle, serverless, AI, storage, the cloud-services rebate) "
                "are on the Spend service breakdown, **not** folded into this query-grain ladder."
            )
            st.caption("Each rung narrows scope and adds estimation error — the warehouse row is "
                       "the only exact one. Nothing here re-bills; it maps where the residual lives.")

        # V077: measured cost by CLIENT APPLICATION x user — which program (and who)
        # drove the credits, so a misconfigured tool is findable. Behind a toggle
        # because the live fallback (a 3-way ACCOUNT_USAGE join) is heavy until
        # FACT_APP_COST_DAILY loads. MEASURED warehouse compute only.
        st.markdown("**Cost by application × user (measured)**")
        if st.toggle("Load cost by application", key="spend_app_cost_load",
                     help="Attributes measured query credits to the client program "
                          "(Tableau, dbt, a driver, a named tool) and the user. Runs a heavy "
                          "live join until the FACT_APP_COST_DAILY fact (V077) is loaded."):
            _app = run_mart_first(
                app_cost_sql.app_cost_mart(days, company),
                app_cost_sql.app_cost_live(days, company),
                page=_PAGE, key=f"app_cost_{company}_{days}",
                mart_source="FACT_APP_COST_DAILY (V077)",
                live_source="SESSIONS x QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY (live)")
            if guard(_app, "No measured application cost in this window.",
                     setup_hint="Apply V077 to materialize FACT_APP_COST_DAILY; until then this "
                                "runs the live join (needs QUERY_ATTRIBUTION_HISTORY access)."):
                _adf = _app.df.copy()
                _adf["USD"] = _adf["CREDITS"].map(safe_float).map(lambda c: credits_to_usd(c, rate))
                _nm = user_display_map(_PAGE)
                _adf["USER_NAME"] = [resolve_display(u, _nm) for u in _adf["USER_NAME"]]
                _byapp = (_adf.groupby("APPLICATION", as_index=False)
                          .agg(USD=("USD", "sum"), QUERIES=("QUERIES", "sum"))
                          .sort_values("USD", ascending=False).head(15))
                charts.bar_usd(_byapp, "APPLICATION", "USD",
                               title="Measured $ by application", top_n=15)
                styled_table(
                    _adf[["APPLICATION", "USER_NAME", "QUERIES", "USD"]].head(300),
                    height=320,
                    column_config={"USD": st.column_config.NumberColumn("Measured $", format="$%.2f")},
                    sort_label="measured $ desc")
                st.caption(md_dollars(
                    "MEASURED warehouse compute + query acceleration, attributed to the client "
                    "program and user; excludes idle, serverless, storage and AI. '(unknown)' = a "
                    "session that reported no application or could not be joined to a session. A "
                    "high line for one program is where to look for a misconfiguration. "
                    "The FACT_APP_COST_DAILY window fills in as the daily loader runs, so soon "
                    "after V077 is applied it may be shorter than the page window; the live "
                    "fallback (this toggle before V077 loads) covers up to 90 days. Ranking by "
                    "program is reliable even while the window is short."))
                result_caption(_app)

    st.markdown("**Daily anomaly check (per warehouse)**")
    daily = daily_res if daily_res is not None else run(
        mart_sql.fact_warehouse_daily(30, company), page=_PAGE,
        key=f"fact_wh_daily_{company}", tier="hourly", source="FACT_WAREHOUSE_DAILY")
    if daily.usable():
        flagged = flag_anomalies(
            complete_days_only(daily.df)  # B4: don't score today's partial row
            .assign(USD=lambda d: d["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))),
            "USD", group_col="WAREHOUSE_NAME",
            min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS,
        )
        # Known-spike calendar: a predictable month-end/quarter-end/named-event spike
        # is labeled "expected", not flagged — collapses always still flag.
        _cal = str(load_settings(_PAGE).get("EXPECTED_SPIKE_CALENDAR") or "")
        flagged = suppress_expected_spikes(flagged, _cal)
        _n_expected = int((flagged["EXPECTED_SPIKE"] != "").sum()) if "EXPECTED_SPIKE" in flagged else 0
        if _n_expected:
            _kinds = ", ".join(sorted(set(
                flagged.loc[flagged["EXPECTED_SPIKE"] != "", "EXPECTED_SPIKE"])))
            st.caption(f"{_n_expected} spike(s) matched the known-spike calendar ({_kinds}) and "
                       "are labeled expected, not anomalous — edit EXPECTED_SPIKE_CALENDAR on "
                       "Admin. Collapses are never suppressed.")
        hits = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")
        if hits:
            for h in hits[:5]:
                # Carry the flagged DAY into the message so the operator knows
                # exactly which day fired without eyeballing the chart.
                _on = f" on {h['day']}" if h.get("day") else ""
                # N10: a collapse (z<0) reads differently from an overspend spike.
                if float(h.get("z", 0) or 0.0) < 0:
                    st.warning(f"{h['label']}: daily spend ${h['value']:,.0f}{_on} collapsed "
                               f"(z {h['z']:+.1f}) — possible stalled workload / dead pipeline.")
                else:
                    st.warning(f"{h['label']}: daily spend ${h['value']:,.0f}{_on} is a statistical "
                               f"outlier (z {h['z']:+.1f}) — investigate.")
            # rec#5: don't stop at "something spiked" — decompose the strongest flag's
            # day into the warehouses that drove it, with a one-line narrative and a
            # contribution waterfall (the deltas sum to the day's total move).
            _top = hits[0]
            if _top.get("day"):
                exp = explain_by_warehouse(flagged, _top["day"])
                if exp.ok and exp.drivers:
                    with st.expander(f"Why did {exp.flagged_day} move? — root-cause waterfall",
                                     expanded=False):
                        # md_dollars: the narrative carries several $ amounts; unescaped,
                        # Streamlit renders everything between two $ as serif-italic LaTeX math.
                        st.markdown(md_dollars(exp.narrative))
                        wf = pd.DataFrame([
                            {"Warehouse": d.name, "Usual $": d.baseline_usd,
                             "That day $": d.actual_usd, "Delta $": d.delta_usd,
                             "Share %": d.share_pct}
                            for d in exp.drivers])
                        styled_table(wf, column_config={
                            "Usual $": st.column_config.NumberColumn(format="$%.2f"),
                            "That day $": st.column_config.NumberColumn(format="$%.2f"),
                            "Delta $": st.column_config.NumberColumn(format="$%.2f"),
                            "Share %": st.column_config.NumberColumn(format="%.1f%%")})
            st.caption(
                "How to investigate a flag: the waterfall above names the warehouses that moved; "
                "then **Operations → Queries** (filter to the warehouse, widen to the flagged day) "
                "and the **Wasted spend** board show the queries that drove it."
            )
        else:
            st.success("No daily spend anomalies in the last 30 days (median/MAD z < 3.5).")
    else:
        st.caption("Anomaly flags appear once 30 days of per-warehouse daily facts have loaded.")
    # Repo review wave 2: Snowflake's managed ML anomaly feed as an INDEPENDENT
    # second opinion beside the z-score sweep above. Optional (SNOWFLAKE.LOCAL),
    # probe-gated; schema renders as-is on purpose (raw honesty over guessed cols).
    # TOGGLE, not expander (review #2 + cost.py's own rule): an expander body runs
    # every rerun, and a failed probe is never cached — an account without the
    # feed would pay a failing round-trip on every default-paint rerun.
    if st.toggle("Second opinion — Snowflake's native anomaly feed", key="native_anom_toggle",
                 help="Reads SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS (Snowflake's managed "
                      "cost-anomaly model) on demand, beside the z-score sweep above."):
        na = run(cost_sql.native_anomaly_insights(), page=_PAGE, key="native_anomalies",
                 tier="historical", source="SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS", probe=True)
        if not na.ok:
            st.caption("The native ANOMALY_INSIGHTS feed isn't available on this account yet "
                       "(it appears once Snowflake's cost-anomaly detection is active). The "
                       "z-score sweep above still runs.")
        elif na.empty:
            st.success("Snowflake's native model reports no cost anomalies.")
        else:
            styled_table(na.df, height=240)
            st.caption("Snowflake's own ML anomaly verdicts, raw. A day flagged by BOTH this "
                       "feed and the z-score sweep above is high-confidence; native-only rows "
                       "catch multi-factor/seasonal shifts the z-score misses; z-score-only "
                       "flags are tuning candidates.")
            result_caption(na)


def _account_storage_tiers(company: str, days: int, settings: dict) -> None:
    """Account-wide storage by tier (F1b/R3, V046). Table/stage/fail-safe bill
    at the standard rate; hybrid and archive cool/cold at their own SETTINGS
    rates. Account grain — STORAGE_USAGE carries no per-database split for these
    tiers, so the company filter does not narrow it."""
    st.markdown("**Account storage by tier (billing basis)**")
    res = run(cost_sql.storage_account_truth(days), page=_PAGE,
              key=f"stor_acct_{days}", tier="hourly",
              source="FACT_STORAGE_ACCOUNT_DAILY (avg of daily bytes)", probe=True)
    if not res.ok or res.empty:
        res = run(cost_sql.storage_account_truth_live(days), page=_PAGE,
                  key=f"stor_acct_live_{days}", tier="historical",
                  source="ACCOUNT_USAGE.STORAGE_USAGE (avg of daily bytes, live)", probe=True)
    if not res.ok:
        st.caption("Account storage tiers need migration V046 "
                   "(FACT_STORAGE_ACCOUNT_DAILY) or STORAGE_USAGE access — an admin "
                   "can apply it on Admin → Migrations & freshness.")
        return
    if res.empty:
        st.caption("No account storage rows in this window yet.")
        return
    row = res.df.iloc[0]
    std = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
    stage_rate = safe_float(settings.get("STORAGE_STAGE_USD_PER_TB_MONTH"), std)
    hybrid_rate = safe_float(settings.get("STORAGE_HYBRID_USD_PER_TB_MONTH"), 348.16)
    cool_rate = safe_float(settings.get("STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH"), 4.0)
    cold_rate = safe_float(settings.get("STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH"), 1.0)
    tiers = [
        ("Table", "TABLE_BYTES", std),
        ("Stage", "STAGE_BYTES", stage_rate),
        ("Fail-safe", "FAILSAFE_BYTES", std),
        ("Hybrid tables", "HYBRID_BYTES", hybrid_rate),
        ("Archive cool", "ARCHIVE_COOL_BYTES", cool_rate),
        ("Archive cold", "ARCHIVE_COLD_BYTES", cold_rate),
    ]
    rows = []
    for label, col, rate in tiers:
        tb = safe_float(row.get(col)) / (1024**4)
        rows.append({"Tier": label, "TiB": round(tb, 4),
                     "$/TiB/mo": round(rate, 2), "USD/mo": round(tb * rate, 2)})
    tdf = pd.DataFrame(rows)
    total_usd = float(tdf["USD/mo"].sum())
    kpi_row([{"label": "Account storage (all tiers)", "value": f"{format_usd(total_usd)}/mo",
              "help": "Avg of daily bytes over the window x per-tier SETTINGS rates. "
                      "Estimate — STORAGE_USAGE is Snowflake's own approximation and won't "
                      "match the invoice exactly; the org rate-card panel on Contract & Forecast is billing truth. "
                      "Stage/hybrid/archive are account-wide (no per-database split)."}])
    shown = tdf[tdf["USD/mo"] > 0]
    if not shown.empty:
        charts.bar_usd(shown.sort_values("USD/mo", ascending=False), "Tier", "USD/mo",
                       title="Storage $/month by tier (est.)")
    styled_table(tdf, height=220, column_config={
        "TiB": st.column_config.NumberColumn("TiB", format="%.2f"),
        "$/TiB/mo": st.column_config.NumberColumn("$/TiB/mo", format="$%.2f"),
        "USD/mo": st.column_config.NumberColumn("USD/mo", format="$%.0f"),
    })
    result_caption(res)


def _storage_table_drill(company: str, settings: dict, db_names: list) -> None:
    """Drill the per-database storage bars down to the TABLE level, in dollars
    (owner ask 2026-08-17: "breakdown of what's driving storage costs ... drill to
    the table level and calculate ... adjust time travel or purge"). Prices each
    table's active / time-travel / fail-safe split at the configured $/TiB, and flags
    STALE (no DML in 90 days) reduce/purge candidates. Read-only: the retention ALTER
    plus the tracked execute + savings booking live on Optimization ▸ Storage & waste."""
    st.markdown("**Drill to tables — what's driving a database's storage**")
    st.caption(
        "Current on-disk snapshot (TABLE_STORAGE_METRICS) priced at the configured $/TiB — "
        "an instantaneous figure for spotting drivers and candidates, so it won't sum exactly "
        "to the per-database month-to-date billing average in the card above."
    )
    dbs = [d for d in dict.fromkeys(str(x) for x in db_names) if d.strip()]
    if not dbs:
        st.caption("No databases in scope to drill into.")
        return
    rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
    pick = st.selectbox("Database", dbs, key="storage_drill_db")
    res = run(insights_sql.table_storage_breakdown(company, str(pick)), page=_PAGE,
              key=f"storage_drill_{pick}", tier="historical",
              source="ACCOUNT_USAGE.TABLE_STORAGE_METRICS + TABLE_DML_HISTORY")
    if not guard(res, f"No table storage rows for {pick} (or TABLE_STORAGE_METRICS is unavailable)."):
        return
    t = res.df.copy()

    def _usd(gb_col: str) -> pd.Series:
        col = t[gb_col] if gb_col in t.columns else pd.Series(0.0, index=t.index)
        return col.map(safe_float) / 1024.0 * rate_tb

    t["Active $"] = _usd("ACTIVE_GB")
    t["Time-travel $"] = _usd("TIME_TRAVEL_GB")
    t["Fail-safe $"] = _usd("FAILSAFE_GB")
    t["Clone $"] = _usd("CLONE_GB")
    # Non-active = everything billed beyond live data. NOT all "reclaimable": only
    # time-travel is directly reducible (via retention), and only its tail past the
    # new window; fail-safe + clone-retained just age out once the data is dropped.
    t["Non-active $"] = t["Time-travel $"] + t["Fail-safe $"] + t["Clone $"]
    t["Total $"] = t["Active $"] + t["Non-active $"]
    is_stale = t["STATUS"].astype(str) == "STALE"
    kpi_row([
        {"label": "Top-50 table storage", "value": f"{format_usd(float(t['Total $'].sum()))}/mo",
         "help": "The 50 largest tables in this database (not necessarily the whole database), "
                 "priced at the configured $/TiB — active + time-travel + fail-safe + clone-retained."},
        {"label": "Time-travel + fail-safe + clone",
         "value": f"{format_usd(float(t['Non-active $'].sum()))}/mo", "delta_color": "off",
         "help": "Storage beyond live data. Only time-travel is directly reducible (lower "
                 "DATA_RETENTION_TIME_IN_DAYS — and only the portion past the new window); fail-safe "
                 "and clone-retained aren't deletable and shrink only once the data drops and ages "
                 "out (fail-safe 7 days)."},
        {"label": "On STALE tables (90d no writes)",
         "value": f"{format_usd(float(t.loc[is_stale, 'Non-active $'].sum()))}/mo",
         "delta_color": "inverse" if bool(is_stale.any()) else "off",
         "help": "Non-active storage on tables with no WRITES in 90 days — the clearest "
                 "reduce-retention candidates. STALE does not consider READS: verify there are no "
                 "consumers before dropping a table."},
    ])
    show_cols = ["SCHEMA_NAME", "TABLE_NAME", "Active $", "Time-travel $", "Fail-safe $",
                 "Clone $", "Total $", "STATUS", "RETENTION_DAYS", "LAST_DML"]
    # $%.2f (cents): a per-table storage $ is often sub-dollar, and $%.0f rendered
    # the whole tail as "$0" (reconciliation fix 2026-08-17).
    cfg = {c: st.column_config.NumberColumn(c, format="$%.2f")
           for c in ("Active $", "Time-travel $", "Fail-safe $", "Clone $", "Total $")}
    styled_table(t[[c for c in show_cols if c in t.columns]], height=340, column_config=cfg)
    result_caption(res)
    # Read-only drill: the retention ALTER-generator + tracked execute + savings-ledger
    # booking are owned by Cost → Optimization & Savings ▸ Storage & waste, which
    # generates the SAME DATA_RETENTION_TIME_IN_DAYS change. This panel just names the
    # drivers and STALE candidates.
    st.caption("This is a read-only breakdown. To reduce a table's "
               "DATA_RETENTION_TIME_IN_DAYS with a tracked execute and a savings-ledger "
               "booking, use **Cost → Optimization & Savings ▸ Storage & waste**.")


def _storage_tab(company: str, days: int, settings: dict) -> None:
    """Storage economics (moved from Chargeback & AI, v4.50): per-database
    calendar-month billing basis plus the account tier split. Spend-lens
    material — storage is neither chargeback nor AI."""
    st.markdown("**Storage by database**")
    # Item 7 (2026-07-14): storage bills on the CALENDAR-month average of
    # daily bytes, so the card shows month-to-date (excl. today's partial
    # day) with the prior completed month for trend — not a trailing-N
    # window. Fact-first with a live DATABASE_STORAGE_USAGE_HISTORY fallback.
    _db = st.session_state.get("flt_database", "")
    today = account_today()
    res = run(cost_sql.storage_by_database_calendar(company, _db, prior=False), page=_PAGE,
              key=f"storage_mtd_{company}", tier="historical",
              source="FACT_STORAGE_DAILY (MTD daily-average, billing basis)")
    # C4/#20: coverage guard. The calendar builder divides SUM(bytes) by
    # days-in-period, so a stalled loader (missing recent days) prices those days
    # as zero storage — a silent understatement the old empty-only fallback never
    # caught. #20: gate freshness on the loader WATERMARK (the NEWEST LATEST_DAY
    # across the scoped databases) plus the active-db inventory, NOT MIN(LATEST_DAY)
    # across ALL historical databases: a DROPPED database's LATEST_DAY is frozen at
    # its drop date, so the old stalest_day (MIN) read the scope as stale forever and
    # forced the live fallback on every render. A db is "active" only if it reaches
    # the watermark; a not-recently-seen db is treated as gone, not as loader loss.
    def _loader_watermark(r: object) -> object:
        if not (getattr(r, "ok", False) and not r.empty) or "LATEST_DAY" not in r.df.columns:
            return None
        ser = pd.to_datetime(r.df["LATEST_DAY"], errors="coerce").dropna()
        return ser.max().date() if not ser.empty else None
    fact_latest = _loader_watermark(res)
    fact_stale = res.ok and not res.empty and (
        fact_latest is None or fact_latest < today - timedelta(days=2))
    if not res.ok or res.empty or fact_stale:
        res = run(cost_sql.storage_by_database_calendar_live(company, _db, prior=False), page=_PAGE,
                  key=f"storage_mtd_live_{company}", tier="historical",
                  source="DATABASE_STORAGE_USAGE_HISTORY (MTD daily-average, live)")
    if guard(res, "No storage rows for this scope this month."):
        df = res.df.copy()
        rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
        df["TiB"] = (df["DB_BYTES"].map(safe_float) + df["FAILSAFE_BYTES"].map(safe_float)) / (1024**4)
        df["USD_MONTH"] = df["TiB"] * rate_tb
        mtd_tib = float(df["TiB"].sum())
        # C4/#21: the prior full month gets the SAME coverage contract as MTD —
        # a completeness guard, a live fallback, and averaging over OBSERVED
        # days. The old fact-only read had none: it divided a partial fact month
        # (stalled loader) by the full calendar month, understating the prior
        # baseline and INFLATING MoM (a smaller denominator makes growth look
        # larger). For a COMPLETED month every existing database should carry
        # every day, so a short row is a loader gap — average over the days
        # actually present, not the calendar length.
        first_this = today.replace(day=1)
        last_prior = first_this - timedelta(days=1)      # last day of the prior month
        period_days_prior = last_prior.day               # calendar length of the prior month
        pri = run(cost_sql.storage_by_database_calendar(company, _db, prior=True), page=_PAGE,
                  key=f"storage_prior_{company}", tier="historical",
                  source="FACT_STORAGE_DAILY (prior full month daily-average)", probe=True)
        pri_latest = _loader_watermark(pri)
        pri_stale = pri.ok and not pri.empty and (
            pri_latest is None or pri_latest < last_prior - timedelta(days=2))
        if not pri.ok or pri.empty or pri_stale:
            pri = run(cost_sql.storage_by_database_calendar_live(company, _db, prior=True), page=_PAGE,
                      key=f"storage_prior_live_{company}", tier="historical",
                      source="DATABASE_STORAGE_USAGE_HISTORY (prior full month daily-average, live)")
        prior_tib = 0.0
        if pri.ok and not pri.empty:
            pdf = pri.df
            avg_bytes = pdf["DB_BYTES"].map(safe_float) + pdf["FAILSAFE_BYTES"].map(safe_float)
            # #19: the builder already divided each db's bytes by the prior month's
            # CALENDAR length — the lifecycle-correct billing basis: a database that
            # existed only part of the month contributes 0 on its absent days, exactly
            # how Snowflake bills the monthly average of daily on-disk bytes. The old
            # code then RESCALED each db by period/observed-days, which assumed every
            # missing day was loader loss and inflated a short-lived (created/dropped
            # mid-month) db ~30x toward a full-month average. Preserve lifecycle zeros:
            # keep the builder's per-db value, and backfill ONLY at the ACCOUNT level
            # when the loader WATERMARK proves the whole month is under-LOADED (the
            # loader never reached the month's end — a uniform tail-day loss across
            # every db, not a lifecycle gap in one).
            prior_tib = float((avg_bytes / (1024**4)).sum())
            first_prior = last_prior.replace(day=1)
            pri_watermark = _loader_watermark(pri)
            if pri_watermark is not None and pri_watermark < last_prior:
                loaded_days = (pri_watermark - first_prior).days + 1
                if loaded_days > 0:
                    prior_tib *= period_days_prior / loaded_days
        mom = ((mtd_tib - prior_tib) / prior_tib * 100.0) if prior_tib > 0 else None
        kpi_row([
            {"label": "Storage MTD (daily avg)", "value": f"{mtd_tib:,.2f} TiB",
             "delta": f"~{format_usd(mtd_tib * rate_tb)}/mo",
             # r6-bug15: a monthly cost estimate is not a favorable move — neutral, like
             # the "Prior full month" sibling below (else a rising cost shows green up).
             "delta_color": "off",
             "help": f"Month-to-date average of daily (active + Time Travel + fail-safe) bytes x "
                     f"${rate_tb:.2f}/TiB/mo (SETTINGS) — Snowflake's calendar-month billing "
                     "basis (binary TiB). rec #30: per-database covers ACTIVE + TIME-TRAVEL "
                     "+ FAIL-SAFE (AVERAGE_DATABASE_BYTES already includes Time Travel); "
                     "hybrid-table, stage, and archive storage carry no per-database split, so "
                     "they are on the account-by-tier panel above, not here — a hybrid-heavy "
                     "database reads low in this view. rec #33: an estimate at the configured "
                     "$/TiB; the org rate-card panel on Contract & Forecast is billing truth."},
            {"label": "Prior full month", "value": f"{prior_tib:,.2f} TiB",
             "delta": (f"{mom:+.1f}% MoM" if mom is not None else "no prior data"),
             "delta_color": "off"},
        ])
        charts.bar_usd(df.sort_values("USD_MONTH", ascending=False),
                       "DATABASE_NAME", "USD_MONTH", title="$/month by database (MTD est.)")
        result_caption(res)
        # C4: expose coverage so a short month reads as short, not as low spend.
        # Expected complete days = day-of-month minus today's excluded partial day.
        expected_days = max(today.day - 1, 0)
        # #20: coverage from the loader WATERMARK (newest loaded day this month),
        # not MIN(DAYS_AVERAGED) across all dbs — a short-lived or dropped database
        # legitimately covers fewer days and must not read as "the loader is behind".
        latest = _loader_watermark(res)
        covered = ((latest - first_this).days + 1) if latest else 0
        latest_txt = latest.isoformat() if latest else "n/a"
        if expected_days > 0 and covered < expected_days - 2:
            st.warning(f"Storage MTD covers {covered} of {expected_days} elapsed days "
                       f"(latest {latest_txt}) — the daily loader is behind, so this "
                       "average understates. Backfill FACT_STORAGE_DAILY to correct it.")
        else:
            st.caption(f"Coverage: averaged {covered} of {expected_days} month-to-date "
                       f"days; latest {latest_txt}.")
        st.divider()
        _storage_table_drill(company, settings, df["DATABASE_NAME"].astype(str).tolist())
    _account_storage_tiers(company, days, settings)
