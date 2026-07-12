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
from app.data import cost_sql, mart27_sql, mart_sql
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
    run_mart_first,
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


def _mtd_spend_usd(rate: float, preloaded: QueryResult | None = None) -> tuple[float, str]:
    """MTD account billed spend (adjustment applied) from the daily fact."""
    res = preloaded if preloaded is not None and preloaded.ok else run(
        mart_sql.fact_daily_spend(45), page=_PAGE, key="fact_daily_45",
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
    # Deliberately NOT batched together (Codex #4): the board is filter-scoped
    # while the 45d MTD fact is fixed — coupling them in one batch cache meant
    # every company/days change cold-started the fixed read. Serial keeps each
    # on its own cache key, so filter changes only refetch the board.
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
            "help": "Warehouse metering credits x "
                    f"${rate:.2f}/credit ({settings.get('_source')}) — the "
                    "company-scopable lens. Serverless/AI and the cloud-services rebate "
                    "are on Cost -> Spend; Snowsight adds storage and transfer, so it "
                    "reads higher.",
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
            "help": "Every deduction is itemized below the trend. "
                    "Sparkline = 14d retro score from facts.",
        },
    ]
    kpi_row(kpis)

    # ---- Monthly spend by warehouse (owner ask 2026-07-11: the boss chart) --
    st.subheader("Monthly spend by warehouse")
    _mres = run_mart_first(
        mart27_sql.monthly_spend_by_warehouse(12, company),
        mart27_sql.fact_monthly_spend_by_warehouse(12, company),
        page=_PAGE, key=f"ov_monthly_{company}",
        mart_source=f"MART_WAREHOUSE_EFFICIENCY_DAILY ({company} + account-level, accruing)",
        live_source="FACT_WAREHOUSE_DAILY (365d backfill, monthly rollup)",
        # r11 #2: the eff mart accrues from deploy day — until it spans a
        # year, the 13-month live view is the truer boss chart.
        mart_accept=lambda df: df["MONTH"].nunique() >= 12)
    if _mres.ok and not _mres.empty:
        _md = _mres.df.copy()
        _md["USD"] = _md["CREDITS"].map(safe_float) * rate
        _cur = pd.Timestamp.now().strftime("%Y-%m")
        charts.monthly_stacked_usd(_md, "MONTH", "WAREHOUSE_NAME", "USD",
                                   partial_month=_cur)
        _tot = _md.groupby("MONTH")["USD"].sum().sort_index()
        _full = _tot[_tot.index < _cur]
        if len(_full) >= 2:
            _mom = (_full.iloc[-1] - _full.iloc[-2]) / max(_full.iloc[-2], 0.01) * 100
            # Escaped dollars: two bare $ in one st.caption pair into a LaTeX
            # math span (live render bug 2026-07-11 — half the caption went
            # monospace and the $ vanished).
            st.caption(f"Last full month {_full.index[-1]}: "
                       f"{format_usd(_full.iloc[-1]).replace('$', chr(92) + '$')} "
                       f"({_mom:+.1f}% vs prior). "
                       "Current month is dimmed — partial, not a drop. "
                       f"Dollars at today's {chr(92)}${rate:.2f}/credit.")
        result_caption(_mres)

    # ---- Spend trend ---------------------------------------------------------
    st.subheader("Spend trend")
    if daily.empty:
        if not trend_source.ok:
            st.error(f"Spend history unavailable: {trend_source.error}")
        else:
            st.info(
                "No spend history for this scope yet — the hourly task fills it in "
                "once installed. Empty until then, never invented."
            )
    else:
        daily_budget = (budget / month_days(account_today())[0]) if budget > 0 else 0.0
        # Forecast range lives in the Projected month-end KPI — the floating
        # rectangle was the "what does this mean" magnet (owner, twice).
        charts.spend_trend(daily, daily_budget_usd=daily_budget)
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
        _bt_hist = run(mart_sql.fact_daily_spend(150), page=_PAGE, key="fact_daily_150",
                       tier="recent", source="FACT_METERING_DAILY (150d)")
        _bt = pd.DataFrame()
        if _bt_hist.usable() and len(_bt_hist.df) >= 50:
            _bt_daily = _bt_hist.df.copy()
            _bt_daily["USD"] = _bt_daily["CREDITS_BILLED"].map(lambda c: safe_float(c) * rate)
            _bt = backtest_forecasts(_bt_daily[["DAY", "USD"]])
        if not _bt.empty:
            # Compact forecast-quality readout (Codex r6 #17): the number
            # rides the page; per-month evidence stays in the expander.
            _mae = _bt.groupby("ENGINE")["ERROR_PCT"].apply(lambda x: x.abs().mean())
            _best = _mae.idxmin()
            st.caption("Forecast quality (3-month backtest): "
                       + " · ".join(f"{eng} ±{err:.1f}%" for eng, err in _mae.items())
                       + f" — '{_best}' most reliable; running '{engine}'.")
        with st.expander("Forecast accuracy — how the projection performed, last 3 months"):
            if not _bt_hist.usable() or len(_bt_hist.df) < 50:
                st.info("Needs ~2 months of daily facts before a backtest says anything.")
            elif _bt.empty:
                st.info("No complete months in the window yet.")
            else:
                styled_table(_bt, height=240, column_config={
                    "ERROR_PCT": st.column_config.NumberColumn("Error %", format="%.1f%%"),
                })
                st.caption("Mean absolute error per engine, per held-out month. Change "
                           "engines via FORECAST_ENGINE on Admin → Settings.")

    if score.drivers:
        with st.expander(f"Platform score deductions ({score.score}/100 · {score.state})"):
            for d in score.drivers:
                st.markdown(f"- **{d.driver}** −{d.penalty:.1f} pts — {d.evidence}")

    if not score_series.empty:
        with st.expander("Score trend — 30 days, retro-computed from facts"):
            charts.daily_metric_line(score_series, "DAY", "SCORE", title="Platform score (retro)")
            st.caption(
                "Live-score weights replayed over each day's facts. Stale-source and "
                "open-action penalties aren't in the facts, so retro sits a few points "
                "high — judge the trend, not the level. Weights calibrate on "
                "Admin → Settings."
            )

    # ---- Two-column: actions + cost drivers ---------------------------------
    action_lines: list[str] = []
    left, right = st.columns([1.15, 1.0])

    with left:
        st.subheader("Top actions")
        actions_res = run(mart_sql.action_queue(200), page=_PAGE, key="action_queue",
                          tier="live", source="ACTION_QUEUE")
        if not actions_res.ok:
            st.info("Action queue isn't installed yet — no placeholder rows.")
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
