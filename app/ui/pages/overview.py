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
from app.logic.formulas import format_usd, month_days, safe_float
from app.ui import charts
from app.ui.components import (
    budget_kpi,
    download_text_button,
    kpi_row,
    load_settings,
    page_header,
    result_caption,
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
    score = scoring.platform_score({
        "budget_pct": (mtd_spend / budget * 100) if budget > 0 else 0,
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "query_fail_pct": fail_pct,
        "task_fail_pct": task_fail_pct,
        "queue_minutes": queued_minutes,
        "spill_gb": spill_gb,
    })

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
            "help": "Run migration V002 so FACT_METERING_DAILY exists (billed credits incl. cloud-services adjustment).",
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
            "help": "From ALERT_EVENTS (V004). Alerts page has the queue." if alerts_res.ok
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
                "No spend history loaded yet for this scope. After V002/V003 run, the hourly "
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
    left, right = st.columns([1.15, 1.0])

    with left:
        st.subheader("Top actions")
        actions_res = run(mart_sql.action_queue(200), page=_PAGE, key="action_queue",
                          tier="live", source="ACTION_QUEUE")
        if not actions_res.ok:
            st.info("Action queue not deployed yet (migration V005). No placeholder rows are shown.")
        elif actions_res.empty:
            st.success("Action queue is empty — nothing is waiting on an owner.")
        else:
            ranked = rank_actions(actions_res.df, limit=5)
            if ranked.empty:
                st.success("No OPEN actions — everything in the queue is done or dropped.")
            else:
                st.dataframe(
                    ranked[["SEVERITY", "TITLE", "OWNER", "DUE_DATE", "ESTIMATED_USD"]],
                    hide_index=True, use_container_width=True,
                )
                result_caption(actions_res)

    with right:
        st.subheader("Top cost drivers")
        drivers = _board_panel(board, "COST_DRIVER")
        if not drivers.empty:
            view = (drivers.groupby("DIMENSION", as_index=False)["VALUE_USD"].sum()
                    .sort_values("VALUE_USD", ascending=False))
            charts.bar_usd(view, "DIMENSION", "VALUE_USD", title="Spend (USD)")
        elif not using_mart and not daily.empty:
            st.caption("Driver ranking needs the exec board mart (V003).")
        else:
            st.info("No cost-driver rows for this scope/window.")

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
    download_text_button("Download executive summary (.txt)", summary, "overwatch_executive_summary.txt")
