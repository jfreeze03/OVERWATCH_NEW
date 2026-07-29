"""Cost & Contract — the Spend & Attribution section bodies (spend by service,
cloud-services health, company attribution).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import pandas as pd
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
    ]


def _spend_tab(company: str, days: int, rate: float, ai_rate: float,
               *, metering_res=None, csr_res=None) -> None:
    # Hot path: the daily metering fact carries the same columns; fall back
    # to live ACCOUNT_USAGE only when the fact has no rows yet. metering_res is
    # the prefetched batch result (perf #15); None -> read it serially here.
    res = metering_res if metering_res is not None else run(
        mart_sql.fact_metering_by_service(days), page=_PAGE, key=f"metering_fact_{days}",
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
            "- Same telemetry, different lenses — each number is exact for its own question."
        )
    result_caption(res)

    st.markdown("**Cloud-services health by warehouse**")
    st.caption(
        "Above ~10% (WATCH) of a warehouse's credits usually means many tiny queries, "
        "metadata-heavy patterns, or compile-heavy SQL. ELEVATED starts past 20%, "
        "where the COST_CLOUD_SVC_RATIO alert fires (editable on Alerts)."
    )
    csr = csr_res if csr_res is not None else run(
        mart_sql.fact_cloud_services_ratio(days, company), page=_PAGE,
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
                     key=f"cs_shapes_{company}_{days}_{pick}", tier="recent",
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
                        key=f"cs_users_{company}_{days}_{pick}", tier="recent",
                        source="MART_CLOUD_SVC_DAILY (CS credits by user/role)")
            if guard(users, "No per-user cloud-services credits for this warehouse yet."):
                st.markdown("**Who's driving it** (user / role / tool)")
                styled_table(users.df, height=220, column_config={
                    "CS_CREDITS": st.column_config.NumberColumn("CS credits", format="%.4f")})
                result_caption(users)

def _attribution_tab(company: str, days: int, rate: float, database: str = "", schema_contains: str = "",
                     *, wh_res=None, daily_res=None) -> None:
    # wh_res / daily_res are the prefetched batch results (perf #15); None ->
    # read serially here. The live/historical fallbacks below are unchanged.
    wh = wh_res if wh_res is not None else run(
        mart_sql.fact_warehouse_window_vs_prior(days, company), page=_PAGE,
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
    daily = daily_res if daily_res is not None else run(
        mart_sql.fact_warehouse_daily(30, company), page=_PAGE,
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


def _account_storage_tiers(company: str, days: int, settings: dict) -> None:
    """Account-wide storage by tier (F1b/R3, V046). Table/stage/fail-safe bill
    at the standard rate; hybrid and archive cool/cold at their own SETTINGS
    rates. Account grain — STORAGE_USAGE carries no per-database split for these
    tiers, so the company filter does not narrow it."""
    st.markdown("**Account storage by tier (billing basis)**")
    res = run(cost_sql.storage_account_truth(days), page=_PAGE,
              key=f"stor_acct_{days}", tier="recent",
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
    styled_table(tdf, height=220)
    result_caption(res)


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
    res = run(cost_sql.storage_by_database_calendar(company, _db, prior=False), page=_PAGE,
              key=f"storage_mtd_{company}", tier="historical",
              source="FACT_STORAGE_DAILY (MTD daily-average, billing basis)")
    if not res.ok or res.empty:
        res = run(cost_sql.storage_by_database_calendar_live(company, _db, prior=False), page=_PAGE,
                  key=f"storage_mtd_live_{company}", tier="historical",
                  source="DATABASE_STORAGE_USAGE_HISTORY (MTD daily-average, live)")
    if guard(res, "No storage rows for this scope this month."):
        df = res.df.copy()
        rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), 23.0)
        df["TiB"] = (df["DB_BYTES"].map(safe_float) + df["FAILSAFE_BYTES"].map(safe_float)) / (1024**4)
        df["USD_MONTH"] = df["TiB"] * rate_tb
        mtd_tib = float(df["TiB"].sum())
        pri = run(cost_sql.storage_by_database_calendar(company, _db, prior=True), page=_PAGE,
                  key=f"storage_prior_{company}", tier="historical",
                  source="FACT_STORAGE_DAILY (prior full month daily-average)", probe=True)
        prior_tib = 0.0
        if pri.ok and not pri.empty:
            prior_tib = float(((pri.df["DB_BYTES"].map(safe_float)
                                + pri.df["FAILSAFE_BYTES"].map(safe_float)) / (1024**4)).sum())
        mom = ((mtd_tib - prior_tib) / prior_tib * 100.0) if prior_tib > 0 else None
        kpi_row([
            {"label": "Storage MTD (daily avg)", "value": f"{mtd_tib:,.2f} TiB",
             "delta": f"~{format_usd(mtd_tib * rate_tb)}/mo",
             "help": f"Month-to-date average of daily (active + fail-safe) bytes x "
                     f"${rate_tb:.2f}/TiB/mo (SETTINGS) — Snowflake's calendar-month billing "
                     "basis (binary TiB). Estimate; the org rate-card panel on Contract & Forecast is billing truth."},
            {"label": "Prior full month", "value": f"{prior_tib:,.2f} TiB",
             "delta": (f"{mom:+.1f}% MoM" if mom is not None else "no prior data"),
             "delta_color": "off"},
        ])
        charts.bar_usd(df.sort_values("USD_MONTH", ascending=False),
                       "DATABASE_NAME", "USD_MONTH", title="$/month by database (MTD est.)")
        result_caption(res)
    _account_storage_tiers(company, days, settings)
