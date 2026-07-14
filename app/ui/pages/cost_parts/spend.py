"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import streamlit as st

from app.core.query import run
from app.data import cost_sql, mart27_sql, mart_sql
from app.logic.anomaly import anomaly_summary, flag_anomalies
from app.logic.formulas import credits_to_usd, format_usd, pct_delta, safe_float
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    result_caption,
    run_mart_first,
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
    "OPENFLOW_COMPUTE_SNOWFLAKE": "Serverless", "HYBRID_TABLE_REQUESTS": "Storage",
    "REPLICATION": "Replication", "STORAGE": "Storage",
}


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

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
         "help": "CREDITS_ADJUSTMENT_CLOUD_SERVICES — the rebate Snowflake applies before billing."},
        {"label": "Compute rate", "value": f"${rate:.2f}/cr", "help": "SETTINGS CREDIT_PRICE_USD."},
        {"label": "Cortex rate", "value": f"${ai_rate:.2f}/cr", "help": "SETTINGS AI_CREDIT_PRICE_USD."},
    ])
    st.caption("Account-wide by service (METERING_DAILY_HISTORY has no company grain; company split lives in Attribution).")
    charts.daily_stacked_usd(df, "DAY", "CATEGORY", "USD")
    with st.expander("Why totals differ across pages (and vs Snowsight)"):
        cat_usd = df.groupby("CATEGORY")["USD"].sum().to_dict()
        wh_usd = float(cat_usd.get("Warehouse", 0.0)) + float(cat_usd.get("Warehouse (reader)", 0.0))
        other_usd = float(sum(cat_usd.values())) - wh_usd
        st.markdown(
            f"- **This page — billed spend ({days}d): {format_usd(billed_usd)}.** Account-wide, "
            "every compute service, cloud-services rebate applied. The number that ties to the bill.\n"
            f"- **Overview / company cards — warehouse-exact: {format_usd(wh_usd)}** of the above is "
            "warehouse metering, the only grain Snowflake scopes per warehouse — which is why the "
            f"company filter lives there. The remaining {format_usd(other_usd)} (serverless, AI, "
            "replication, reader) has no warehouse to scope by.\n"
            "- **Snowsight → Cost Management reads higher than both:** it adds storage and data "
            "transfer dollars and prices from USAGE_IN_CURRENCY (list/contract currency), and its "
            "MTD window follows calendar-month boundaries in account time.\n"
            "- Same telemetry, different lenses — each number is exact for its own question."
        )
    result_caption(res)

    st.markdown("**Cloud-services health by warehouse**")
    st.caption(
        "Above ~10% of a warehouse's credits usually means many tiny queries, "
        "metadata-heavy patterns, or compile-heavy SQL. The COST_CLOUD_SVC_RATIO "
        "alert fires at ELEVATED (editable on Alerts)."
    )
    csr = run(mart_sql.fact_cloud_services_ratio(days, company), page=_PAGE,
              key=f"csr_fact_{company}_{days}", tier="recent",
              source="FACT_WAREHOUSE_DAILY (cloud-services share)")
    if not csr.usable():  # mart not deployed/loaded yet -> bounded live scan
        csr = run(cost_sql.cloud_services_ratio_by_warehouse(days, company), page=_PAGE,
              key=f"cs_ratio_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY")
    if guard(csr, "No warehouse metering in this window."):
        styled_table(csr.df, height=260)
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
            cs_types = run(cost_sql.cs_by_query_type(days, company), page=_PAGE,
                           key=f"cs_types_{company}_{days}", tier="historical",
                           source="ACCOUNT_USAGE.QUERY_HISTORY (CS credits by QUERY_TYPE)")
            if guard(cs_types, "No cloud-services credits recorded on queries in this window."):
                styled_table(cs_types.df, height=220)
                st.caption("Metadata storms show up here — SHOW/DESCRIBE floods bill "
                           "cloud services without ever touching a warehouse.")

def _attribution_tab(company: str, days: int, rate: float, database: str = "", schema_contains: str = "") -> None:
    wh = run(mart_sql.fact_warehouse_window_vs_prior(days, company), page=_PAGE,
             key=f"wh_vs_prior_fact_{company}_{days}", tier="recent",
             source="FACT_WAREHOUSE_DAILY (window vs prior, loaded hourly)")
    if not wh.usable():  # mart not deployed/loaded yet -> bounded live scan
        wh = run(cost_sql.warehouse_window_vs_prior(days, company), page=_PAGE,
                 key=f"wh_vs_prior_{company}_{days}", tier="historical",
                 source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (live fallback)")
    st.markdown("**By warehouse (exact usage)**")
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
        result_caption(wh, note="Equal-length windows excluding the current partial day for "
                                "completeness. Exact USAGE, not billed: totals include each "
                                "warehouse's idle time and its unadjusted cloud-services credits "
                                "— the account-level rebate lives on the Spend panel. "
                                "Company-wide: the database/schema filters don't narrow this table.")

        st.markdown("**By user and database (allocated — estimate)**")
        st.caption(
            "Snowflake bills at warehouse grain. These split the scoped warehouse spend "
            f"({format_usd(window_usd)}) by query elapsed-time share; treat as directionally "
            "correct. Shares stay global, so a database/schema filter shows that slice of "
            "the total — never 100% of it. NONE = queries with no database context; "
            "USER$ personal databases attribute to their owner's company. "
            "The mart path weights each share by that warehouse-hour's credits "
            "(size-aware); the live fallback (shown while facts load, or whenever a "
            "schema filter is set) uses elapsed-time share, which is warehouse-size-blind "
            "— a coarser estimate when one entity concentrates on unusually large or small "
            "warehouses."
        )
        col_u, col_d = st.columns(2)
        for col, dim, label in ((col_u, "USER_NAME", "user"), (col_d, "DATABASE_NAME", "database")):
            with col:
                _alloc_live = cost_sql.allocated_attribution(days, dim, company, database, schema_contains)
                if schema_contains:
                    # no allocation mart carries a schema grain — live only
                    res = run(_alloc_live, page=_PAGE, key=f"alloc_{dim}_{company}_{days}",
                              tier="historical", source="ACCOUNT_USAGE.QUERY_HISTORY (elapsed share)")
                else:
                    # P0-1/P0-2 (Codex 2026-07-14): BOTH unfiltered and database-
                    # filtered attribution read FACT_COST_ALLOC_XDIM_DAILY so company
                    # scope is warehouse-based on every path. The owner-scoped
                    # MART_COST_ALLOCATION_DAILY made the same user/DB total shift
                    # when a database filter was toggled. `database` is "" unfiltered.
                    res = run_mart_first(
                        mart27_sql.alloc_xdim_attribution(days, dim.replace("_NAME", ""), company, database),
                        _alloc_live, page=_PAGE, key=f"alloc_{dim}_{company}_{days}",
                        mart_source="FACT_COST_ALLOC_XDIM_DAILY (mart — warehouse-hour credit share)",
                        live_source="QUERY_HISTORY (elapsed share, live fallback)")
                if guard(res, f"No query history to allocate by {label}."):
                    alloc = res.df.copy()
                    # ONE formula on every path (live math fix 2026-07-11):
                    # share x the window total the caption states. Direct
                    # dollarization of mart credits used a different window
                    # and included idle — SYSTEM alone exceeded the caption.
                    alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"].map(safe_float) * window_usd
                    if len(alloc) > 1:
                        charts.waterfall_usd(alloc.head(10), "DIMENSION", "ALLOCATED_USD")
                        st.caption("Waterfall: top 10 contributors (allocated).")
                    charts.bar_usd(alloc, "DIMENSION", "ALLOCATED_USD", title=f"Allocated $ by {label}")
                    shown = float(alloc["ELAPSED_SHARE"].map(safe_float).sum())
                    st.caption(f"Rows shown cover {shown:.0%} of scoped spend "
                               f"({format_usd(shown * window_usd)} of {format_usd(window_usd)}).")

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
                st.warning(f"{h['label']}: daily spend ${h['value']:,.0f} is a statistical outlier (z {h['z']:+.1f}) — investigate.")
        else:
            st.success("No daily spend anomalies in the last 30 days (median/MAD z < 3.5).")
    else:
        st.caption("Anomaly flags appear once 30 days of per-warehouse daily facts have loaded.")
