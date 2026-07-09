"""Overview — the executive glance page.

Contract (the old app broke all four of these):
1. Real data on first paint: the exec board mart loads automatically (one
   cheap cached query); the fallback is a bounded live aggregate, not zeros.
2. No synthetic series: charts render real days or an honest empty state.
3. The action list is the real ACTION_QUEUE, ranked — never template rows.
4. Budget math only appears when a budget is actually configured.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.result import QueryResult
from app.core.state import filters
from app.data import cost_sql, mart_sql
from app.logic import scoring
from app.logic.actions import rank_actions
from app.logic.forecast import MonthEndForecast, backtest_forecasts, month_end_projection
from app.logic.formulas import account_today, exec_summary_html, format_usd, month_days, safe_float
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
    month_start = account_today().replace(day=1)
    mtd_credits = frame[frame["DAY"] >= month_start]["CREDITS_BILLED"].map(safe_float).sum()
    return mtd_credits * rate, res.source


def _open_alert_counts(company: str = "ALL") -> tuple[QueryResult, int, int]:
    res = run(mart_sql.open_alert_events(500, company), page=_PAGE,
              key=f"open_alerts_{company}", tier="live",
              source="ALERT_EVENTS" if company == "ALL"
              else f"ALERT_EVENTS ({company} + account-level)")
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
        icon_name="overview",
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
    alerts_res, critical_alerts, high_alerts = _open_alert_counts(company)
    engine = str(settings.get("FORECAST_ENGINE") or "linear").strip().lower()
    forecast = None
    if engine == "ml_forecast":
        mlres = run(mart_sql.ml_forecast_daily(), page=_PAGE, key="ml_forecast",
                    tier="recent", source="FORECAST_ML_DAILY (SNOWFLAKE.ML.FORECAST)")
        if mlres.usable():
            mdf = mlres.df.copy()
            today = account_today()
            month_end = (today.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
            mdf["DAY"] = pd.to_datetime(mdf["DAY"]).dt.date
            mdf = mdf[(mdf["DAY"] > today) & (mdf["DAY"] < month_end)]
            if not mdf.empty:
                mtd_now = float(pd.to_numeric(
                    daily[pd.to_datetime(daily.iloc[:, 0]).dt.date >= today.replace(day=1)]
                    .iloc[:, -1], errors="coerce").fillna(0).sum()) if not daily.empty else 0.0
                add = float(pd.to_numeric(mdf["FORECAST_CREDITS"], errors="coerce").fillna(0).sum()) * rate
                lo = float(pd.to_numeric(mdf["LOWER_BOUND"], errors="coerce").fillna(0).sum()) * rate
                hi = float(pd.to_numeric(mdf["UPPER_BOUND"], errors="coerce").fillna(0).sum()) * rate
                forecast = MonthEndForecast(
                    ok=True, mtd_usd=round(mtd_now, 2),
                    projected_usd=round(mtd_now + add, 2),
                    low_usd=round(max(mtd_now, mtd_now + lo), 2),
                    high_usd=round(mtd_now + hi, 2),
                    daily_rate_usd=round(add / max(len(mdf), 1), 2),
                    days_remaining=len(mdf),
                    basis="SNOWFLAKE.ML.FORECAST via FORECAST_ML_DAILY (opt-in script).",
                )
        if forecast is None:
            engine = "seasonal"  # honest fallback when the ML view isn't installed
    if forecast is None:
        forecast = (month_end_projection(daily, account_today(), engine=engine)
                    if not daily.empty else month_end_projection(pd.DataFrame(), account_today(), engine=engine))

    queries = _board_metric(board, "QUERIES")
    failed_queries = _board_metric(board, "FAILED_QUERIES")
    fail_pct = (failed_queries / queries * 100) if queries else 0.0
    queued_minutes = _board_metric(board, "QUEUED_MINUTES")
    spill_gb = _board_metric(board, "SPILL_GB")
    task_runs = _board_metric(board, "TASK_RUNS")
    task_failures = _board_metric(board, "TASK_FAILURES")
    task_fail_pct = (task_failures / task_runs * 100) if task_runs else 0.0

    budget = safe_float(settings.get("MONTHLY_BUDGET_USD"))
    score_inputs = run(mart_sql.score_inputs_daily(30), page=_PAGE, key="score_inputs",
                       tier="recent", source="facts + ALERT_EVENTS (retro score inputs)")
    score_series = (scoring.score_history(score_inputs.df, scoring.resolve_weights(settings),
                                          budget, rate)
                    if score_inputs.usable() else pd.DataFrame())
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
    _spend_spark = (daily["USD"].tail(14).tolist() if not daily.empty else None)
    _score_sev = ("ok" if score.score >= 85 else "warn" if score.score >= 70 else "bad")
    kpis = [
        {
            "label": f"Spend, last {days}d ({company})",
            "value": format_usd(window_spend),
            "spark": _spend_spark,
            "help": "Warehouse-exact metering credits x rate "
                    f"(${rate:.2f}/credit from {settings.get('_source')}) — the "
                    "company-scopable lens. Serverless/AI and the cloud-services rebate "
                    "live on Cost -> Spend (account billed total); Snowsight's Cost "
                    "Management adds storage and data transfer, so it reads higher.",
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
            "severity": ("bad" if (alerts_res.ok and critical_alerts) else
                         "warn" if (alerts_res.ok and high_alerts) else "ok"),
            "help": "The Alerts page has the full queue." if alerts_res.ok
                    else f"Alert tables unreachable: {alerts_res.error}",
        },
        {
            "label": "Platform score",
            "value": f"{score.score}/100",
            "delta": score.state,
            "delta_color": "off",
            "severity": _score_sev,
            "spark": (score_series["SCORE"].tail(14).tolist()
                      if not score_series.empty else None),
            "help": "Evidence-based; every deduction is listed below the trend. "
                    "Sparkline = 14d retro score from facts.",
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
        daily_budget = (budget / month_days(account_today())[0]) if budget > 0 else 0.0
        band = (forecast.low_usd, forecast.high_usd) if forecast.ok and budget > 0 else None
        charts.spend_trend(daily, daily_budget_usd=daily_budget, band=None if band is None else band)
        activity = run(mart_sql.fact_daily_activity(14), page=_PAGE, key="spark_activity",
                       tier="recent", source="FACT_QUERY_HOURLY (daily)")
        adf = activity.df if activity.ok and not activity.empty else None
        spend14 = daily.tail(14) if len(daily) else None
        day_col = daily.columns[0] if len(daily.columns) else "DAY"
        usd_col = next((c for c in daily.columns if "USD" in str(c).upper() or "CREDIT" in str(c).upper()),
                       daily.columns[-1] if len(daily.columns) else "USD")
        charts.sparkline_row([
            ("Spend, 14d", spend14, day_col, usd_col),
            ("Queries, 14d", adf, "DAY", "QUERIES"),
            ("Failures, 14d", adf, "DAY", "FAILS"),
        ])
        result_caption(trend_source, note="mart-first" if using_mart else "live fallback — deploy marts for cheaper loads")
        with st.expander("Forecast accuracy — how the projection performed, last 3 months"):
            hist = run(mart_sql.fact_daily_spend(150), page=_PAGE, key="fact_daily_150",
                       tier="recent", source="FACT_METERING_DAILY (150d)")
            if not hist.usable() or len(hist.df) < 50:
                st.info("Needs ~2 months of daily facts before a backtest says anything.")
            else:
                bt_daily = hist.df.copy()
                bt_daily["USD"] = bt_daily["CREDITS_BILLED"].map(lambda c: safe_float(c) * rate)
                bt = backtest_forecasts(bt_daily.rename(columns={"DAY": "DAY"})[["DAY", "USD"]])
                if bt.empty:
                    st.info("No complete months in the window yet.")
                else:
                    styled_table(bt, height=240, column_config={
                        "ERROR_PCT": st.column_config.NumberColumn("Error %", format="%.1f%%"),
                    })
                    mae = bt.groupby("ENGINE")["ERROR_PCT"].apply(lambda x: x.abs().mean())
                    best = mae.idxmin()
                    st.caption(
                        "Mean absolute error — "
                        + " · ".join(f"{eng}: {err:.1f}%" for eng, err in mae.items())
                        + f". '{best}' has been the more reliable engine; the current engine "
                          f"is '{engine}' (FORECAST_ENGINE on Admin → Settings)."
                    )

    if score.drivers:
        with st.expander(f"Platform score deductions ({score.score}/100 · {score.state})"):
            for d in score.drivers:
                st.markdown(f"- **{d.driver}** −{d.penalty:.1f} pts — {d.evidence}")

    if not score_series.empty:
        with st.expander("Score trend — 30 days, retro-computed from facts"):
            charts.daily_metric_line(score_series, "DAY", "SCORE", title="Platform score (retro)")
            st.caption(
                "Same weights as the live score, replayed against each day's facts and "
                "alert history. RETRO: stale-source and open-action penalties aren't in "
                "the facts, so absolute values can sit a few points above the live score — "
                "judge the TREND. This history is also the raw material for calibrating "
                "the score weights on Admin → Settings."
            )

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
