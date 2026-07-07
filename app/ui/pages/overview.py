"""Overview — the executive glance page.

Contract (the old app broke all four of these):
1. Real data on first paint: the exec board mart loads automatically (one
   cheap cached query); the fallback is a bounded live aggregate, not zeros.
2. No synthetic series: charts render real days or an honest empty state.
3. The action list is the real ACTION_QUEUE, ranked — never template rows.
4. Budget math only appears when a budget is actually configured.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.result import QueryResult
from app.core.state import filters
from app.data import cost_sql, mart_sql
from app.logic import scoring
from app.logic.actions import rank_actions
from app.logic.forecast import month_end_projection
from app.logic.formulas import exec_summary_html, format_usd, month_days, safe_float
from app.ui import charts
from app.ui.components import (
    budget_kpi,
    download_text_button,
    kpi_row,
    load_settings,
    page_header,
    result_caption,
    styled_table,
)

_PAGE = "Overview"


def _board_metric(board: pd.DataFrame, metric: str, column: str = "VALUE") -> float:
    rows = board[(board["PANEL"] == "KPI") & (board["METRIC"] == metric)]
    if rows.empty:
        return 0.0
    return safe_float(rows.iloc[0].get(column))


def _board_panel(board: pd.DataFrame, panel: str) -> pd.DataFrame:
    return board[board["PANEL"] == panel].copy()


def _load_board(company: str, days: int) -> QueryResult:
    return run(
        mart_sql.exec_board(company, days),
        page=_PAGE, key=f"exec_board_{company}_{days}", tier="recent",
        source="MART_EXEC_BOARD",
    )


def _live_fallback_daily(company: str, days: int, rate: float) -> tuple[pd.DataFrame, QueryResult]:
    """Bounded live aggregate when the mart is not deployed — real data,
    clearly labeled, never fabricated."""
    res = run(
        cost_sql.warehouse_daily_credits(days, company),
        page=_PAGE, key=f"live_wh_daily_{company}_{days}", tier="historical",
        source="Live ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (bounded)",
    )
    if not res.usable():
        return pd.DataFrame(), res
    daily = res.df.groupby("DAY", as_index=False)["CREDITS_TOTAL"].sum()
    daily["USD"] = daily["CREDITS_TOTAL"].map(lambda c: safe_float(c) * rate)
    return daily[["DAY", "USD"]], res


def _mtd_spend_usd(rate: float) -> tuple[float, str]:
    """MTD account billed spend (adjustment applied) from the daily fact."""
    res = run(mart_sql.fact_daily_spend(45), page=_PAGE, key="fact_daily_45",
              tier="recent", source="FACT_METERING_DAILY")
    if not res.usable():
        return 0.0, ""
    frame = res.df.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    month_start = date.today().replace(day=1)
    mtd_credits = frame[frame["DAY"] >= month_start]["CREDITS_BILLED"].map(safe_float).sum()
    return mtd_credits * rate, res.source


def _open_alert_counts() -> tuple[QueryResult, int, int]:
    res = run(mart_sql.open_alert_events(500), page=_PAGE, key="open_alerts",
              tier="live", source="ALERT_EVENTS")
    if not res.ok or res.empty:
        return res, 0, 0
    sev = res.df["SEVERITY"].astype(str).str.upper()
    return res, int((sev == "CRITICAL").sum()), int((sev == "HIGH").sum())


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)

    page_header(
        "Overview",
        "Spend, risk, and the work that needs an owner.",
        scope_note=f"{company} · last {days} days",
    )

    # ---- data loads (mart-first, labeled live fallback) --------------------
    board_res = _load_board(company, days)
    board = board_res.df if board_res.usable() else pd.DataFrame(
        columns=["PANEL", "METRIC", "DIMENSION", "PERIOD_START", "VALUE", "VALUE_USD"]
    )
    using_mart = board_res.usable()

    if using_mart:
        daily_panel = _board_panel(board, "DAILY_SPEND")
        daily = daily_panel.rename(columns={"PERIOD_START": "DAY", "VALUE_USD": "USD"})[["DAY", "USD"]]
        daily["USD"] = daily["USD"].map(safe_float)
        daily = daily.groupby("DAY", as_index=False)["USD"].sum().sort_values("DAY")
        trend_source = board_res
    else:
        daily, trend_source = _live_fallback_daily(company, days, rate)

    window_spend = float(daily["USD"].sum()) if not daily.empty else _board_metric(board, "CREDITS", "VALUE_USD")
    mtd_spend, mtd_source = _mtd_spend_usd(rate)
    alerts_res, critical_alerts, high_alerts = _open_alert_counts()
    forecast = month_end_projection(daily, date.today()) if not daily.empty else month_end_projection(pd.DataFrame(), date.today())

    queries = _board_metric(board, "QUERIES")
    failed_queries = _board_metric(board, "FAILED_QUERIES")
    fail_pct = (failed_queries / queries * 100) if queries else 0.0
    queued_minutes = _board_metric(board, "QUEUED_MINUTES")
    spill_gb = _board_metric(board, "SPILL_GB")
    task_runs = _board_metric(board, "TASK_RUNS")
    task_failures = _board_metric(board, "TASK_FAILURES")
    task_fail_pct = (task_failures / task_runs * 100) if task_runs else 0.0

    budget = safe_float(settings.get("MONTHLY_BUDGET_USD"))
    score = scoring.platform_score(signals={
        "budget_pct": (mtd_spend / budget * 100) if budget > 0 else 0,
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "query_fail_pct": fail_pct,
        "task_fail_pct": task_fail_pct,
        "queue_minutes": queued_minutes,
        "spill_gb": spill_gb,
    }, weights=scoring.resolve_weights(settings))

    # ---- KPI row -----------------------------------------------------------
    kpis = [
        {
            "label": f"Spend, last {days}d ({company})",
            "value": format_usd(window_spend),
            "help": "Warehouse metering credits x configured rate "
                    f"(${rate:.2f}/credit from {settings.get('_source')}).",
        },
        budget_kpi(settings, mtd_spend) if mtd_source else {
            "label": "MTD spend",
            "value": "Needs daily facts",
            "help": "Appears once the daily metering facts are installed (billed credits incl. cloud-services adjustment).",
        },
        {
            "label": "Projected month-end",
            "value": format_usd(forecast.projected_usd) if forecast.ok else "Needs history",
            "help": (f"{forecast.basis} Range {format_usd(forecast.low_usd)}-{format_usd(forecast.high_usd)}."
                     if forecast.ok else forecast.basis),
        },
        {
            "label": "Open critical / high alerts",
            "value": f"{critical_alerts} / {high_alerts}" if alerts_res.ok else "Setup",
            "help": "The Alerts page has the full queue." if alerts_res.ok
                    else f"Alert tables unreachable: {alerts_res.error}",
        },
        {
            "label": "Platform score",
            "value": f"{score.score}/100",
            "delta": score.state,
            "delta_color": "off",
            "help": "Evidence-based; every deduction is listed below the trend.",
        },
    ]
    kpi_row(kpis)

    # ---- Spend trend ---------------------------------------------------------
    st.subheader("Spend trend")
    if daily.empty:
        if not trend_source.ok:
            st.error(f"Spend history unavailable: {trend_source.error}")
        else:
            st.info(
                "No spend history loaded yet for this scope. Once installed, the hourly "
                "task fills this in; the chart stays empty rather than showing invented numbers."
            )
    else:
        _, _, _ = month_days(date.today())
        daily_budget = (budget / month_days(date.today())[0]) if budget > 0 else 0.0
        band = (forecast.low_usd, forecast.high_usd) if forecast.ok and budget > 0 else None
        charts.spend_trend(daily, daily_budget_usd=daily_budget, band=None if band is None else band)
        result_caption(trend_source, note="mart-first" if using_mart else "live fallback — deploy marts for cheaper loads")

    if score.drivers:
        with st.expander(f"Platform score deductions ({score.score}/100 · {score.state})"):
            for d in score.drivers:
                st.markdown(f"- **{d.driver}** −{d.penalty:.1f} pts — {d.evidence}")

    # ---- Two-column: actions + cost drivers ---------------------------------
    action_lines: list[str] = []
    left, right = st.columns([1.15, 1.0])

    with left:
        st.subheader("Top actions")
        actions_res = run(mart_sql.action_queue(200), page=_PAGE, key="action_queue",
                          tier="live", source="ACTION_QUEUE")
        if not actions_res.ok:
            st.info("Action queue is not installed yet. No placeholder rows are shown.")
        elif actions_res.empty:
            st.success("Action queue is empty — nothing is waiting on an owner.")
        else:
            ranked = rank_actions(actions_res.df, limit=5)
            if ranked.empty:
                st.success("No OPEN actions — everything in the queue is done or dropped.")
            else:
                styled_table(ranked[["SEVERITY", "TITLE", "OWNER", "DUE_DATE", "ESTIMATED_USD"]])
                result_caption(actions_res)
                action_lines = [
                    f"[{a['SEVERITY']}] {a['TITLE']} — owner {a.get('OWNER') or 'unassigned'}"
                    for _, a in ranked.iterrows()
                ]

    with right:
        st.subheader("Top cost drivers")
        drivers = _board_panel(board, "COST_DRIVER")
        if not drivers.empty:
            view = (drivers.groupby("DIMENSION", as_index=False)["VALUE_USD"].sum()
                    .sort_values("VALUE_USD", ascending=False))
            charts.bar_usd(view, "DIMENSION", "VALUE_USD", title="Spend (USD)")
        elif not using_mart and not daily.empty:
            st.caption("Driver ranking appears once the exec board mart is installed.")
        else:
            st.info("No cost-driver rows for this scope/window.")

    # ---- Daily AI digest ------------------------------------------------------
    digest = run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="recent",
                 source="DAILY_DIGEST (Cortex, grounded in the exec board)")
    if digest.usable():
        row = digest.df.iloc[0]
        with st.expander(f"Morning AI digest — {row.get('DIGEST_DATE')} ({row.get('MODEL')})",
                         expanded=False):
            st.markdown(str(row.get("BODY") or ""))
            st.caption("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts only.")

    # ---- Executive summary download -----------------------------------------
    summary = (
        f"OVERWATCH executive summary — {company}, last {days} days — "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Window spend: {format_usd(window_spend)}\n"
        f"MTD spend: {format_usd(mtd_spend) if mtd_source else 'n/a (daily facts not deployed)'}"
        f"{' vs budget ' + format_usd(budget) if budget > 0 else ' (no budget configured)'}\n"
        f"Projected month-end: "
        f"{format_usd(forecast.projected_usd) + ' (' + format_usd(forecast.low_usd) + '-' + format_usd(forecast.high_usd) + ')' if forecast.ok else 'insufficient history'}\n"
        f"Open alerts: {critical_alerts} critical, {high_alerts} high\n"
        f"Platform score: {score.score}/100 ({score.state})"
        + ("".join(f"\n  - {d.driver}: -{d.penalty:.1f} pts ({d.evidence})" for d in score.drivers) if score.drivers else "")
    )
    html = exec_summary_html(
        company=company, days=days, generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        window_spend=format_usd(window_spend),
        mtd_line=(format_usd(mtd_spend) if mtd_source else "n/a")
                 + (f" vs {format_usd(budget)} budget" if budget > 0 else ""),
        forecast_line=(format_usd(forecast.projected_usd)
                       + f" ({format_usd(forecast.low_usd)}–{format_usd(forecast.high_usd)})")
                      if forecast.ok else "insufficient history",
        alerts_line=f"{critical_alerts} critical · {high_alerts} high",
        score_line=f"{score.score}/100 ({score.state})",
        drivers=[(d.driver, f"{d.penalty:.1f}", d.evidence) for d in score.drivers],
        actions=action_lines,
    )
    c_html, c_txt = st.columns(2)
    with c_html:
        st.download_button("Download executive summary (HTML)", html,
                           file_name="overwatch_executive_summary.html", mime="text/html",
                           use_container_width=True)
    with c_txt:
        download_text_button("Plain-text version (.txt)", summary, "overwatch_executive_summary.txt")
