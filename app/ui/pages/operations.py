"""Operations — queries, tasks, warehouses, contention."""

from __future__ import annotations

import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.state import filters
from app.data import mart_sql, ops_sql
from app.logic.anomaly import flag_anomalies
from app.logic.formulas import credits_to_usd, safe_float
from app.ui import charts
from app.ui.components import guard, kpi_row, load_settings, page_header, result_caption

_PAGE = "Operations"


def _queries_tab(company: str, days: int, wh_filter: str, user_filter: str) -> None:
    summary = run(ops_sql.query_window_summary(days, company, wh_filter, user_filter),
                  page=_PAGE, key=f"q_summary_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.QUERY_HISTORY")
    if summary.usable():
        row = summary.df.iloc[0]
        qcount = safe_float(row.get("QUERY_COUNT"))
        failed = safe_float(row.get("FAILED_COUNT"))
        kpi_row([
            {"label": f"Queries ({days}d)", "value": f"{qcount:,.0f}"},
            {"label": "Fail rate", "value": f"{(failed / qcount * 100) if qcount else 0:.2f}%",
             "delta": f"{failed:,.0f} failed", "delta_color": "off"},
            {"label": "p95 runtime", "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s"},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.0f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(summary)

    st.markdown("**Heaviest queries**")
    top = run(ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter, user_filter),
              page=_PAGE, key=f"q_top_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY", max_rows=50)
    if guard(top, "No queries in this window/scope."):
        st.dataframe(
            top.df[["USER_NAME", "WAREHOUSE_NAME", "ELAPSED_SEC", "QUEUED_SEC",
                     "SPILL_REMOTE_GB", "EXECUTION_STATUS", "QUERY_PREVIEW"]],
            hide_index=True, use_container_width=True,
            column_config={
                "ELAPSED_SEC": st.column_config.NumberColumn("Elapsed s", format="%.1f"),
                "QUEUED_SEC": st.column_config.NumberColumn("Queued s", format="%.1f"),
                "SPILL_REMOTE_GB": st.column_config.NumberColumn("Spill GB", format="%.2f"),
            },
        )
        st.caption("Elapsed-time ranking. Per-query dollars are estimates; exact billing is per warehouse.")

    st.markdown("**Failures by error**")
    fails = run(ops_sql.failures_by_error(days, company), page=_PAGE,
                key=f"q_fails_{company}_{days}", tier="recent",
                source="ACCOUNT_USAGE.QUERY_HISTORY")
    if guard(fails, "No failed queries in this window."):
        st.dataframe(fails.df, hide_index=True, use_container_width=True)


def _tasks_tab(company: str, days: int) -> None:
    res = run(mart_sql.fact_task_daily(days, company), page=_PAGE, key=f"t_fact_{company}_{days}",
              tier="recent", source="FACT_TASK_DAILY")
    if not res.usable():
        res = run(ops_sql.task_runs(days, company), page=_PAGE, key=f"t_live_{company}_{days}",
                  tier="recent", source="ACCOUNT_USAGE.TASK_HISTORY (live fallback)")
    if guard(res, "No task runs recorded for this scope/window."):
        df = res.df.copy()
        failed_col = "FAILED" if "FAILED" in df.columns else None
        if failed_col:
            total_runs = safe_float(df.get("RUNS", 0).sum() if "RUNS" in df.columns else 0)
            total_failed = safe_float(df[failed_col].sum())
            kpi_row([
                {"label": f"Task runs ({days}d)", "value": f"{total_runs:,.0f}"},
                {"label": "Failed runs", "value": f"{total_failed:,.0f}",
                 "delta": f"{(total_failed / total_runs * 100) if total_runs else 0:.1f}%",
                 "delta_color": "inverse" if total_failed else "off"},
            ])
            df = df.sort_values(failed_col, ascending=False)
        st.dataframe(df, hide_index=True, use_container_width=True)
        result_caption(res)


def _warehouses_tab(company: str, rate: float) -> None:
    res = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE, key=f"w_fact_{company}",
              tier="recent", source="FACT_WAREHOUSE_DAILY")
    if not guard(res, "No warehouse dailies yet — V002 facts load them hourly.",
                 setup_hint="Live equivalent lives on Cost & Contract > Attribution."):
        return
    df = res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    flagged = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME")
    daily = df.groupby("DAY", as_index=False)["USD"].sum()
    charts.spend_trend(daily)
    anomalies = flagged[flagged["IS_ANOMALY"]]
    if anomalies.empty:
        st.success("No per-warehouse daily anomalies (30d, median/MAD z ≥ 3.5).")
    else:
        st.warning(f"{len(anomalies)} anomalous warehouse-day(s):")
        st.dataframe(
            anomalies[["DAY", "WAREHOUSE_NAME", "USD", "Z_SCORE"]].sort_values("Z_SCORE", ascending=False),
            hide_index=True, use_container_width=True,
            column_config={
                "USD": st.column_config.NumberColumn("Spend $", format="$%.0f"),
                "Z_SCORE": st.column_config.NumberColumn("Robust z", format="%.1f"),
            },
        )
    result_caption(res)


def _contention_tab(company: str, days: int) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Warehouse queue & spill pressure**")
        res = run(ops_sql.warehouse_pressure(days, company), page=_PAGE,
                  key=f"c_pressure_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.QUERY_HISTORY")
        if guard(res, "No queueing or spill pressure in this window."):
            charts.bar_count(res.df.sort_values("QUEUED_SEC", ascending=False),
                             "WAREHOUSE_NAME", "QUEUED_SEC", title="Queued seconds")
            st.dataframe(res.df, hide_index=True, use_container_width=True)
    with right:
        st.markdown("**Lock waits (account-wide)**")
        res = run(ops_sql.lock_contention(min(days, 14)), page=_PAGE, key=f"c_locks_{days}",
                  tier="recent", source="ACCOUNT_USAGE.LOCK_WAIT_HISTORY")
        if guard(res, "No lock waits recorded (or the view is not accessible in this edition)."):
            st.dataframe(res.df, hide_index=True, use_container_width=True)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    page_header("Operations", "Queries, tasks, warehouses, and contention.",
                scope_note=f"{f['company']} · last {f['days']} days")
    tab_q, tab_t, tab_w, tab_c = st.tabs(["Queries", "Tasks", "Warehouses", "Contention"])
    with tab_q:
        _queries_tab(f["company"], f["days"], f["warehouse_contains"], f["user_contains"])
    with tab_t:
        _tasks_tab(f["company"], f["days"])
    with tab_w:
        _warehouses_tab(f["company"], rate)
    with tab_c:
        _contention_tab(f["company"], f["days"])
