"""Overview — the executive glance page.

Contract (the old app broke all four of these):
1. Real data on first paint: the exec board mart loads automatically (one
   cheap cached query); the fallback is a bounded live aggregate, not zeros.
2. No synthetic series: charts render real days or an honest empty state.
3. The action list is the real ACTION_QUEUE, ranked — never template rows.
4. Budget math only appears when a budget is actually configured.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run, run_batch
from app.core.result import QueryResult
from app.core.state import filters, request_navigation
from app.data import cost_sql, mart27_sql, mart_sql
from app.logic import scoring
from app.logic.actions import rank_actions
from app.logic.forecast import MonthEndForecast, backtest_forecasts, month_end_projection
from app.logic.formulas import (
    account_now,
    account_today,
    blended_billed_usd,
    exec_summary_html,
    format_usd,
    month_days,
    safe_float,
)
from app.ui import charts
from app.ui.components import (
    download_text_button,
    kpi_row,
    load_settings,
    page_header,
    result_caption,
    run_mart_first,
    section_scope_note,
    styled_table,
)

_PAGE = "Overview"

# N8: each score-deduction driver → the page that lets you act on it. Off-profile
# targets are clamped to Overview by consume_pending_navigation (B8), so this is safe.
_SCORE_DRIVER_NAV = {
    "Over budget": "Cost & Contract",
    "Critical alerts": "Alerts",
    "High alerts": "Alerts",
    "Query failures": "Operations",
    "Task failures": "Operations",
    "Queueing": "Operations",
    "Remote spill": "Operations",
    "Stale telemetry": "Control Room",
    "Owner queue": "Alerts",
    "Inputs unavailable": "Admin",
}

# C2/N5: the platform score's throughput+pressure health signals read a FIXED
# recent window, not the user's spend window — so flipping 7/30/90d never moves
# the score, the spike-sized thresholds see spike-sized inputs, and the headline
# shares the per-day basis of the retro sparkline. 1 = midnight-aligned 24-48h.
_SCORE_HEALTH_WINDOW_DAYS = 1


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
        page=_PAGE, key=f"exec_board_{company}_{days}", tier="hourly",
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


def _billed_usd_series(frame: pd.DataFrame, rate: float, ai_rate: float) -> pd.Series:
    """Per-day billed USD from the AI/OTHER split (C1): AI credits price at the
    AI rate, the rest at the compute rate. Falls back to the flat rate on the
    total when a frame predates the split columns (live fallback / old cache)."""
    if "CREDITS_BILLED_OTHER" in frame.columns and "CREDITS_BILLED_AI" in frame.columns:
        return (frame["CREDITS_BILLED_OTHER"].map(safe_float) * rate
                + frame["CREDITS_BILLED_AI"].map(safe_float) * ai_rate)
    return frame["CREDITS_BILLED"].map(safe_float) * rate


def _mtd_spend_usd(rate: float, ai_rate: float,
                   preloaded: QueryResult | None = None) -> tuple[float, str]:
    """MTD account billed spend (adjustment applied) from the daily fact,
    AI credits priced at the AI rate (C1)."""
    res = preloaded if preloaded is not None and preloaded.ok else run(
        mart_sql.fact_daily_spend(45), page=_PAGE, key="fact_daily_45",
        tier="hourly", source="FACT_METERING_DAILY")
    if not res.usable():
        return 0.0, ""
    frame = res.df.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    month_start = account_today().replace(day=1)
    mtd = frame[frame["DAY"] >= month_start]
    if "CREDITS_BILLED_OTHER" in mtd.columns and "CREDITS_BILLED_AI" in mtd.columns:
        spend = blended_billed_usd(mtd["CREDITS_BILLED_OTHER"].map(safe_float).sum(),
                                   mtd["CREDITS_BILLED_AI"].map(safe_float).sum(),
                                   rate, ai_rate)
    else:
        spend = mtd["CREDITS_BILLED"].map(safe_float).sum() * rate
    return spend, res.source


def _open_alert_counts(company: str = "ALL",
                       prefetched: QueryResult | None = None) -> tuple[QueryResult, int, int]:
    # A-score-1: count criticals/highs from the UNCAPPED severity aggregate (the same
    # source as the Alerts-page KPI, C4/C7), NOT a 500-row feed that undercounts in a
    # storm — these counts feed BOTH the KPI and the platform score, so a >500-alert
    # storm would otherwise inflate the score exactly when it must be trusted. Overview
    # renders no alert LIST, so the 500 rows were fetched only to be counted.
    # N4: accept a pre-fetched result from the first-paint run_batch, else read solo.
    res = prefetched if prefetched is not None else run(
        mart_sql.open_alert_severity_counts(company), page=_PAGE,
        key=f"alert_counts_{company}", tier="live",
        source="ALERT_EVENTS (COUNT_IF by severity, uncapped)")
    if not res.ok or res.empty:
        return res, 0, 0
    _row = res.df.iloc[0]
    return res, int(safe_float(_row.get("CRIT"))), int(safe_float(_row.get("HIGH")))


def _mtd_pace_kpi(mtd_spend: float, hist: QueryResult, rate: float,
                  ai_rate: float, budget: float) -> dict:
    """MTD paced against the prior month's same first-N-days (owner
    2026-07-13: the Monthly-budget KPI read 'Not configured' forever —
    replaced with a pace that needs no configuration). Reuses the 150d
    daily-spend frame already loaded for the forecast backtest — zero extra
    queries. A configured budget still shows, inside help, not as a KPI."""
    from app.logic.formulas import mtd_pace_vs_prior_month

    # C11: metering-daily has no company grain, so MTD is account-wide even under
    # a company filter — the badge says so on the card (the help already did).
    if not hist.usable():
        return {"label": "MTD spend", "value": format_usd(mtd_spend), "method": "billed", "scope": "account-wide",
                "help": "Billed credits incl. the cloud-services adjustment (account-wide)."}
    frame = hist.df.copy()
    frame["USD"] = _billed_usd_series(frame, rate, ai_rate)
    mtd, prior, pct = mtd_pace_vs_prior_month(frame[["DAY", "USD"]], account_today())
    budget_note = (f" Budget context: {mtd / budget * 100:,.0f}% of "
                   f"{format_usd(budget)} (MONTHLY_BUDGET_USD)." if budget > 0 else "")
    if pct is None:
        return {"label": "MTD spend", "value": format_usd(mtd), "method": "billed", "scope": "account-wide",
                "help": "Pace vs last month appears once the prior month has "
                        "daily facts (backfill_365.sql loads the year)." + budget_note}
    return {"label": "MTD vs last month (same days)",
            "value": format_usd(mtd), "method": "billed", "scope": "account-wide",
            "delta": f"{pct:+,.0f}% vs {format_usd(prior)}",
            "delta_color": "inverse",
            "help": "Billed credits, account-wide, at today's rate. The value is "
                    "this month to date; the pace delta compares the same number "
                    "of days both months share." + budget_note}


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)

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
    # One 150d metering read serves MTD here AND the forecast backtest below
    # (Codex r16 #17) — the separate 45d read survives only as the fallback
    # inside _mtd_spend_usd when this one fails.
    _bt_hist = run(mart_sql.fact_daily_spend(150), page=_PAGE, key="fact_daily_150",
                   tier="hourly", source="FACT_METERING_DAILY (150d)")
    mtd_spend, mtd_source = _mtd_spend_usd(rate, ai_rate, preloaded=_bt_hist)
    # Triage #1: the exec-board `daily` frame is windowed to the filter `days`
    # (default 7) and is company-scoped, so it truncates month-to-date for most of
    # the month and mismatches the account-wide "Projected month-end" KPI (which
    # sits beside the account-wide MTD KPI). Project from the account-wide 150d
    # frame already loaded above; fall back to the board frame only if it failed.
    if _bt_hist.usable():
        _proj = _bt_hist.df.copy()
        _proj["USD"] = _billed_usd_series(_proj, rate, ai_rate)
        proj_daily = _proj[["DAY", "USD"]]
    else:
        proj_daily = daily
    # N4: Overview never adopted the first-paint run_batch that Brief/Control Room
    # use. The two independent LIVE reads (uncapped alert counts + owner-action
    # queue) now fetch in one round trip. health_strip stays out (the shell already
    # fetched it — shared cache, r15 #14); the exec board stays out (filter-scoped
    # own key, Codex #4). Each key matches its solo cache key, so warm hits are
    # unchanged. A-score-1: the alert leg is the uncapped severity aggregate.
    _live_pf = run_batch([
        {"key": f"alert_counts_{company}", "sql": mart_sql.open_alert_severity_counts(company),
         "source": "ALERT_EVENTS (COUNT_IF by severity, uncapped)"},
        {"key": "action_queue", "sql": mart_sql.action_queue(200), "source": "ACTION_QUEUE"},
    ], page=_PAGE, tier="live") or {}
    alerts_res, critical_alerts, high_alerts = _open_alert_counts(
        company, prefetched=_live_pf.get(f"alert_counts_{company}"))
    engine = str(settings.get("FORECAST_ENGINE") or "linear").strip().lower()
    forecast = None
    if engine == "ml_forecast":
        mlres = run(mart_sql.ml_forecast_daily(), page=_PAGE, key="ml_forecast",
                    tier="hourly", source="FORECAST_ML_DAILY (SNOWFLAKE.ML.FORECAST)")
        if mlres.usable():
            mdf = mlres.df.copy()
            today = account_today()
            month_end = (today.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
            mdf["DAY"] = pd.to_datetime(mdf["DAY"]).dt.date
            mdf = mdf[(mdf["DAY"] > today) & (mdf["DAY"] < month_end)]
            if not mdf.empty:
                # mtd_now rides proj_daily, already AI-split-priced (C1). The
                # FORECAST_* legs price at the compute rate: FORECAST_ML_DAILY
                # forecasts one blended-credit number per day with no service
                # split, so an exact AI-aware forecast needs the mart to model AI
                # separately (queued for V061). Opt-in engine; 'linear' is default.
                mtd_now = float(pd.to_numeric(
                    proj_daily[pd.to_datetime(proj_daily.iloc[:, 0]).dt.date >= today.replace(day=1)]
                    .iloc[:, -1], errors="coerce").fillna(0).sum()) if not proj_daily.empty else 0.0
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
        forecast = (month_end_projection(proj_daily, account_today(), engine=engine)
                    if not proj_daily.empty else month_end_projection(pd.DataFrame(), account_today(), engine=engine))

    # C2/N5: pull the score's throughput+pressure signals from a FIXED recent
    # window (not the exec board, which is windowed to the user's 7/30/90d spend
    # scope). On the board, QUEUED_MINUTES/SPILL_GB are cumulative sums over the
    # window, so a normal week dwarfs the spike-sized 10-min/5-GB thresholds and
    # trips a near-constant deduction that grows with the window; and the retro
    # sparkline is per-day, an incomparable basis. This read is per-recent-day.
    # A-score-3: this is a midnight-aligned window (yesterday 00:00 → now), so it
    # spans the PREVIOUS + CURRENT calendar day (24h at midnight, ~48h by end of
    # day) — deliberately aligned to the live ops tiles, not a rolling 24h. rec 10:
    # the source refreshes hourly, so cache at the hourly tier (salt still forces refresh).
    # rec 9: the two hourly score-health reads (throughput + task) are independent and
    # company-scoped — batch them into one round trip (finishing N4 for the score path).
    # board/150d stay unbatched (filter-scoped + fixed cold-start each other, Codex #4);
    # health_strip stays on the shared shell cache; the live alert/action reads batch above.
    _thr_sql = mart_sql.fact_query_window_summary(_SCORE_HEALTH_WINDOW_DAYS, company)
    _tk_sql = mart_sql.fact_task_daily(_SCORE_HEALTH_WINDOW_DAYS, company)
    _score_pf = run_batch([
        {"key": f"score_throughput_{company}", "sql": _thr_sql,
         "source": "FACT_QUERY_HOURLY (prev + current calendar day)"},
        {"key": f"score_tasks_{company}", "sql": _tk_sql,
         "source": "FACT_TASK_DAILY (prev + current calendar day)"},
    ], page=_PAGE, tier="hourly") or {}
    _thr = _score_pf.get(f"score_throughput_{company}") or run(
        _thr_sql, page=_PAGE, key=f"score_throughput_{company}", tier="hourly",
        source="FACT_QUERY_HOURLY (prev + current calendar day)")
    _tr = _thr.df.iloc[0] if _thr.usable() and not _thr.empty else None
    queries = safe_float(_tr.get("QUERY_COUNT")) if _tr is not None else 0.0
    failed_queries = safe_float(_tr.get("FAILED_COUNT")) if _tr is not None else 0.0
    fail_pct = (failed_queries / queries * 100) if queries else 0.0
    queued_minutes = (safe_float(_tr.get("QUEUED_SEC")) / 60.0) if _tr is not None else 0.0
    spill_gb = safe_float(_tr.get("SPILL_REMOTE_GB")) if _tr is not None else 0.0
    # A-score-3: FACT_TASK_DAILY is DAY-grain, so this covers the previous + current
    # calendar day (it can't be sub-day windowed). rec 10: daily source → hourly tier.
    # rec 9: served from the score-read batch above.
    _tk = _score_pf.get(f"score_tasks_{company}") or run(
        _tk_sql, page=_PAGE, key=f"score_tasks_{company}", tier="hourly",
        source="FACT_TASK_DAILY (prev + current calendar day)")
    _tk_ok = _tk.usable() and not _tk.empty
    task_runs = float(_tk.df["RUNS"].map(safe_float).sum()) if _tk_ok else 0.0
    task_failures = float(_tk.df["FAILED"].map(safe_float).sum()) if _tk_ok else 0.0
    task_fail_pct = (task_failures / task_runs * 100) if task_runs else 0.0

    budget = safe_float(settings.get("MONTHLY_BUDGET_USD"))
    # V041 R8: the daily fact stores the four retro-score input aggregates;
    # weights stay in Python. The 4-source live aggregation is the fallback.
    score_inputs = run_mart_first(
        mart27_sql.platform_score_inputs(30), mart_sql.score_inputs_daily(30),
        page=_PAGE, key="score_inputs",
        mart_source="FACT_PLATFORM_SCORE_DAILY (daily snapshot)",
        live_source="facts + ALERT_EVENTS (retro score inputs, live fallback)",
        mart_tier="hourly", live_tier="hourly")  # rec 10: score facts refresh daily
    score_series = (scoring.score_history(score_inputs.df, scoring.resolve_weights(settings),
                                          budget, rate, ai_rate)  # C1: AI-rate-blended budget
                    if score_inputs.usable() else pd.DataFrame())
    # Triage #3: the Stale-telemetry and Owner-queue drivers (and their SETTINGS
    # weights) could never fire live because the caller omitted their signals.
    # stale_sources rides the shell-shared health_strip cache entry (same SQL +
    # key="health_strip" as main.py/brief — zero extra queries on a warm shell);
    # open_high_actions comes from the ACTION_QUEUE read hoisted above the score
    # (the Top-actions panel below reuses it).
    _hs = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="live",
              source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    stale_sources = 0
    if _hs.ok and not _hs.empty:
        _srow = _hs.df[_hs.df["METRIC"].astype(str) == "STALE_SOURCES"]
        if not _srow.empty:
            stale_sources = int(safe_float(_srow.iloc[0]["VALUE"]))
    actions_res = _live_pf.get("action_queue") or run(
        mart_sql.action_queue(200), page=_PAGE, key="action_queue",
        tier="live", source="ACTION_QUEUE")  # N4: reuse the first-paint batch
    open_high_actions = 0
    if actions_res.ok and not actions_res.empty:
        _adf = actions_res.df
        open_high_actions = int(((_adf["STATUS"].astype(str).str.upper() == "OPEN")
                                 & (_adf["SEVERITY"].astype(str).str.upper() == "HIGH")).sum())
    # C1: tell the score which health-bearing sources actually LOADED. If the
    # throughput read or the alerts read failed, their signals are silently 0 (an
    # outage would otherwise raise the score) — the score reports Incomplete instead
    # of a false green. C2/N5: the required health source is now the fixed-window
    # throughput read (not the exec board, which no longer feeds the score).
    _available = set()
    if _thr.usable():
        _available.add("throughput")
    if alerts_res.ok:
        _available.add("alerts")
    # A-score-2: extend C1's fail-closed principle to the OTHER penalty-bearing
    # sources. A read that hit a GENUINE outage (timeout/unknown_function/other — not
    # 'absent', which just means the mart isn't installed on a partial deployment)
    # silently zeros its penalty, which would raise the score. task/freshness/owner-
    # queue fail the score closed on outage; budget only matters when a budget is set
    # and the MTD read didn't resolve.
    _degraded = scoring.degraded_sources(
        {"task": _tk, "freshness": _hs, "owner-queue": actions_res})
    if budget > 0 and not mtd_source and getattr(_bt_hist, "error_kind", "") != "absent":
        _degraded.add("budget")
    score = scoring.platform_score(signals={
        "budget_pct": (mtd_spend / budget * 100) if budget > 0 else 0,
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "query_fail_pct": fail_pct,
        "task_fail_pct": task_fail_pct,
        "queue_minutes": queued_minutes,
        "spill_gb": spill_gb,
        "stale_sources": stale_sources,
        "open_high_actions": open_high_actions,
    }, weights=scoring.resolve_weights(settings), available=_available, degraded=_degraded)

    # ---- KPI row -----------------------------------------------------------
    _spend_spark = (daily["USD"].tail(14).tolist() if not daily.empty else None)
    _score_incomplete = score.state == "Incomplete"   # C1
    _score_sev = ("warn" if _score_incomplete
                  else "ok" if score.score >= 85 else "warn" if score.score >= 70 else "bad")
    kpis = [
        {
            "label": f"Spend, last {days}d ({company})",
            "value": format_usd(window_spend),
            "method": "metering", "scope": "company",  # rec 13: warehouse metering, company-scoped
            "spark": _spend_spark,
            "help": "Warehouse metering credits x "
                    f"${rate:.2f}/credit ({settings.get('_source')}) — the "
                    "company-scopable lens. Serverless/AI and the cloud-services rebate "
                    "are on Cost & Contract -> Spend & Attribution; Snowsight adds storage and transfer, so it "
                    "reads higher.",
        },
        _mtd_pace_kpi(mtd_spend, _bt_hist, rate, ai_rate, budget) if mtd_source else {
            "label": "MTD spend",
            "value": "Needs daily facts",
            "method": "billed", "scope": "account-wide",
            "help": "Appears once the daily metering facts are installed (billed credits incl. cloud-services adjustment).",
        },
        {
            "label": "Projected month-end",
            "value": format_usd(forecast.projected_usd) if forecast.ok else "Needs history",
            "method": "billed", "scope": "account-wide",
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
            "value": "Incomplete" if _score_incomplete else f"{score.score}/100",
            "delta": "inputs unavailable" if _score_incomplete else score.state,
            "delta_color": "off",
            "severity": _score_sev,
            "spark": (None if _score_incomplete else score_series["SCORE"].tail(14).tolist()
                      if not score_series.empty else None),
            "help": ("Throughput or alerts didn't load — a real score would be a lie while "
                     "health signals are missing (see the deductions below)."
                     if _score_incomplete
                     else "Throughput & pressure (queries, failures, queue, spill) read the "
                          "previous + current calendar day and are company-scoped; budget, "
                          "open alerts, stale telemetry and the owner queue are account-wide "
                          "— so the score does NOT move when you change the spend window. "
                          "Every deduction is itemized below the trend; sparkline = 14d retro."),
        },
    ]
    kpi_row(kpis)
    # N7: MTD & Projected are credit-billed services (compute + serverless + AI).
    # Storage and data-transfer are separate invoice lines the app reads on Cost &
    # Contract (org rate-card) — disclosed here so these figures aren't mistaken for
    # the whole bill. Not folded in: that would break the credits x rate contract.
    st.caption("MTD & Projected cover credit-billed services (compute, serverless, AI). "
               "Storage and data-transfer bill separately — see Cost & Contract → org rate card.")
    # rec 11: these headline KPIs are account-/company-scoped and do NOT honor the
    # warehouse/schema/user/database dimension chips. Surface that ONLY when such a
    # chip is actually set, so a user who narrowed by warehouse upstream isn't misled
    # into reading these numbers as filtered. (window-spend is company-scoped -> the
    # 'company' key is honored implicitly; the dimension chips are the misleading ones.)
    _scope_note = section_scope_note(f)
    if _scope_note:
        st.caption(_scope_note)

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
        _cur = account_today().strftime("%Y-%m")   # N6: account time, not the UTC server clock
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
        activity = run(mart_sql.fact_daily_activity(14, company), page=_PAGE,
                       key="spark_activity", tier="hourly",
                       source="FACT_QUERY_HOURLY (daily)")
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
        _bt = pd.DataFrame()
        if _bt_hist.usable() and len(_bt_hist.df) >= 50:
            _bt_daily = _bt_hist.df.copy()
            _bt_daily["USD"] = _billed_usd_series(_bt_daily, rate, ai_rate)
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
            # N8: every deduction maps to a page — make the highest-value screen
            # actionable (diagnosis + one-click prescription), reusing the nav plumbing.
            for _i, d in enumerate(score.drivers):
                _c1, _c2 = st.columns([6, 1])
                _c1.markdown(f"- **{d.driver}** −{d.penalty:.1f} pts — {d.evidence}")
                _dest = _SCORE_DRIVER_NAV.get(d.driver)
                if _dest and _c2.button("Investigate →", key=f"score_drv_{_i}", type="tertiary"):
                    request_navigation(_dest)

    if not score_series.empty:
        with st.expander("Score trend — 30 days, retro-computed from facts (account-wide)"):
            charts.daily_metric_line(score_series, "DAY", "SCORE", title="Platform score (retro, account-wide)")
            st.caption(
                "Live-score weights replayed over each day's facts. Stale-source and "
                "open-action penalties aren't in the facts, so retro sits a few points "
                "high — judge the trend, not the level. Weights calibrate on "
                "Admin → Settings. Note: the retro inputs (FACT_PLATFORM_SCORE_DAILY) "
                "have no company grain, so this trend is account-wide even under a "
                "company filter. The headline blends company-scoped 24h throughput/"
                "pressure (same per-day basis as this line) with account-wide budget, "
                "alerts, telemetry and owner-queue signals, so its level can differ."
            )

    # ---- Two-column: actions + cost drivers ---------------------------------
    action_lines: list[str] = []
    left, right = st.columns([1.15, 1.0])

    with left:
        st.subheader("Top actions")
        # actions_res loaded above the score (triage #3) — reused here.
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
    digest = run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="hourly",
                 source="DAILY_DIGEST (Cortex, grounded in the exec board)")
    if digest.usable():
        row = digest.df.iloc[0]
        with st.expander(f"Morning AI digest — {row.get('DIGEST_DATE')} ({row.get('MODEL')})",
                         expanded=False):
            st.markdown(str(row.get("BODY") or ""))
            st.caption("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts only.")

    # ---- Executive summary download -----------------------------------------
    # rec 5: export the SAME honest view-model the screen shows. An Incomplete score
    # must NOT export as a real-looking 0/100; account-wide figures must not sit under
    # a company-scoped heading unlabelled; and the footer must not blanket-claim the
    # cloud-services adjustment for the warehouse window-spend number (which excludes it).
    _score_export = ("Incomplete — health inputs unavailable" if _score_incomplete
                     else f"{score.score}/100 ({score.state})")
    _mtd_export = ((format_usd(mtd_spend) if mtd_source else "n/a (daily facts not deployed)")
                   + (f" vs budget {format_usd(budget)}" if budget > 0 else " (no budget configured)")
                   + " · account-wide")
    _fc_export = ((format_usd(forecast.projected_usd)
                   + f" ({format_usd(forecast.low_usd)}–{format_usd(forecast.high_usd)}) · account-wide")
                  if forecast.ok else "insufficient history")
    summary = (
        f"OVERWATCH executive summary — {company}, last {days} days — "
        f"{account_now().strftime('%Y-%m-%d %H:%M')} (account time)\n"
        f"Window spend ({company}, warehouse metering, credits × rate): {format_usd(window_spend)}\n"
        f"MTD spend: {_mtd_export}\n"
        f"Projected month-end: {_fc_export}\n"
        f"Open alerts: {critical_alerts} critical, {high_alerts} high\n"
        f"Platform score: {_score_export}"
        + ("".join(f"\n  - {d.driver}: -{d.penalty:.1f} pts ({d.evidence})" for d in score.drivers) if score.drivers else "")
    )
    html = exec_summary_html(
        company=company, days=days, generated=account_now().strftime("%Y-%m-%d %H:%M") + " (account time)",
        window_spend=f"{format_usd(window_spend)} · {company}, metering",
        mtd_line=_mtd_export,
        forecast_line=_fc_export,
        alerts_line=f"{critical_alerts} critical · {high_alerts} high",
        score_line=_score_export,
        drivers=[(d.driver, f"{d.penalty:.1f}", d.evidence) for d in score.drivers],
        actions=action_lines,
    )
    c_html, c_txt = st.columns(2)
    with c_html:
        st.download_button("Download executive summary (HTML)", html,
                           file_name="overwatch_executive_summary.html", mime="text/html",
                           use_container_width=True, on_click="ignore")
    with c_txt:
        download_text_button("Plain-text version (.txt)", summary, "overwatch_executive_summary.txt")
