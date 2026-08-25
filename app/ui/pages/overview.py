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
    ExecutiveSummaryView,
    account_now,
    account_today,
    blended_billed_usd,
    budget_pace_variance,
    contract_runway,
    credits_to_usd,
    executive_slide_bullets,
    executive_summary_csv,
    executive_summary_html,
    format_credits,
    format_usd,
    md_dollars,
    month_days,
    pct_delta,
    safe_float,
)
from app.logic.verdict import Signal, page_verdict
from app.ui import charts
from app.ui.components import (
    contract_runway_bar,
    daily_spend_wide,
    download_text_button,
    entity_nav_table,
    export_button,
    kpi_row,
    load_settings,
    page_header,
    page_verdict_line,
    panel_help,
    result_caption,
    run_mart_first,
    section_filter_contract,
    section_header,
    selectable_nav_table,
    styled_table,
)
from app.ui.sizing import TABLE_H_MD

_PAGE = "Overview"

# N8: each score-deduction driver → the page that lets you act on it. Off-profile
# targets are clamped to Overview by consume_pending_navigation (B8), so this is safe.
_SCORE_DRIVER_NAV = {
    "Budget pace": "Cost & Contract",
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


def _load_board(company: str, days: int, window: object = None) -> QueryResult:
    return run(
        mart_sql.exec_board(company, days, window),
        page=_PAGE, key=f"exec_board_{company}_{window or days}", tier="hourly",
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


def _billed_split_available(frame: pd.DataFrame) -> bool:
    """True when the AI/OTHER split columns are present (rec#28).

    Every mart builder emits the split, so this is normally True. When it is
    False — a pre-split cached frame or a live shape carrying only the bare
    CREDITS_BILLED total — `_billed_usd_series` cannot split and prices every
    credit at the compute rate, overstating AI/Cortex-heavy spend. Callers read
    this to surface a 'flat-rate est.' badge instead of a silent overstatement.
    """
    return {"CREDITS_BILLED_OTHER", "CREDITS_BILLED_AI"}.issubset(frame.columns)


def _billed_usd_series(frame: pd.DataFrame, rate: float, ai_rate: float) -> pd.Series:
    """Per-day billed USD from the AI/OTHER split (C1): AI credits price at the
    AI rate, the rest at the compute rate. Falls back to the flat rate on the
    total when a frame predates the split columns (live fallback / old cache);
    callers disclose that fallback via _billed_split_available (rec#28)."""
    if _billed_split_available(frame):
        return (frame["CREDITS_BILLED_OTHER"].map(safe_float) * rate
                + frame["CREDITS_BILLED_AI"].map(safe_float) * ai_rate)
    return frame["CREDITS_BILLED"].map(safe_float) * rate


def _mtd_spend_usd(rate: float, ai_rate: float,
                   preloaded: QueryResult | None = None) -> tuple[float, str]:
    """MTD account billed spend (adjustment applied) from the daily fact,
    AI credits priced at the AI rate (C1)."""
    res = preloaded if preloaded is not None and preloaded.ok else daily_spend_wide(_PAGE)
    if not res.usable():
        return 0.0, ""
    frame = res.df.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    month_start = account_today().replace(day=1)
    mtd = frame[frame["DAY"] >= month_start]
    if _billed_split_available(mtd):
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
        _mtd_credits = safe_float(mtd_spend) / rate if rate > 0 else None
        return {"label": "MTD credit spend", "value": format_usd(mtd_spend),
                "sub": f"{format_credits(_mtd_credits)} cr" if _mtd_credits is not None else None,
                "method": "billed", "scope": "account-wide",
                "help": "Credit-billed services at configured rates, including the "
                        "cloud-services adjustment. Storage and transfer are separate."}
    frame = hist.df.copy()
    # rec#28: if this refresh lacks the AI/OTHER split, _billed_usd_series prices
    # every credit at the compute rate — overstating AI/Cortex spend. Badge the
    # method 'flat-rate est.' and disclose it in help, rather than reading 'billed'.
    _split_ok = _billed_split_available(frame)
    _method = "billed" if _split_ok else "flat-rate est."
    _split_note = ("" if _split_ok else
                   " Note: the AI/compute rate split is unavailable on this refresh, so every "
                   "credit is priced at the compute rate — AI/Cortex-heavy spend may read high.")
    frame["USD"] = _billed_usd_series(frame, rate, ai_rate)
    mtd, prior, pct = mtd_pace_vs_prior_month(frame[["DAY", "USD"]], account_today())
    # Credit sub-line: when the AI/OTHER split is present, sum billed CREDITS directly over
    # the SAME MTD window (run a credits series through mtd_pace) rather than back-solving
    # mtd_usd/rate — the USD blends AI credits at ai_rate, so mtd/rate would undercount
    # credits by the AI portion (~13% on AI-heavy spend). Without the split, mtd/rate is exact.
    if _split_ok:
        _cr_frame = frame[["DAY"]].assign(
            USD=frame["CREDITS_BILLED_OTHER"].map(safe_float)
            + frame["CREDITS_BILLED_AI"].map(safe_float))
        _mtd_cr, _, _ = mtd_pace_vs_prior_month(_cr_frame, account_today())
        _mtd_credits = safe_float(_mtd_cr)
    else:
        _mtd_credits = safe_float(mtd) / rate if rate > 0 else None
    budget_note = (f" Budget context: {mtd / budget * 100:,.0f}% of "
                   f"{format_usd(budget)} (MONTHLY_BUDGET_USD)." if budget > 0 else "")
    if pct is None:
        return {"label": "MTD credit spend", "value": format_usd(mtd),
                "sub": f"{format_credits(_mtd_credits)} cr" if _mtd_credits is not None else None,
                "method": _method, "scope": "account-wide",
                "help": "Pace vs last month appears once the prior month has "
                        "daily facts (backfill_365.sql loads the year)." + budget_note + _split_note}
    return {"label": "MTD credit spend vs last month",
            "value": format_usd(mtd),
            "sub": f"{format_credits(_mtd_credits)} cr" if _mtd_credits is not None else None,
            "method": _method, "scope": "account-wide",
            "delta": f"{pct:+,.0f}% vs {format_usd(prior)}",
            "delta_color": "inverse",
            "help": "Credit-billed services, account-wide, at configured rates. The value is "
                    "this month to date; the pace delta compares the same number "
                    "of days both months share. Storage and transfer are separate."
                    + budget_note + _split_note}


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
        scope_note=f"{company} · {f['window_label']}",
        icon_name="overview",
    )

    # ---- data loads (mart-first, labeled live fallback) --------------------
    # Deliberately NOT batched together (Codex #4): the board is filter-scoped
    # while the 45d MTD fact is fixed — coupling them in one batch cache meant
    # every company/days change cold-started the fixed read. Serial keeps each
    # on its own cache key, so filter changes only refetch the board.
    board_res = _load_board(company, days, f["window"])
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

    # rec1/rec36: drop today's still-filling partial row from the headline total and
    # the per-day average, so "Spend, last N days" uses the today-EXCLUDED convention
    # the Cost page's "By warehouse (exact usage)" uses (common.resolve_effective_window).
    # Every other windowed/per-day dollar in the app already excludes today's partial;
    # Overview's board panel is partial-inclusive, which made the two pages disagree.
    # The trend spark keeps the full series for visual continuity — only totals change.
    daily_complete = (
        daily[pd.to_datetime(daily["DAY"], errors="coerce").dt.date < account_today()]
        if not daily.empty else daily
    )
    # rec1/rec36: with no complete day in the window, the honest complete-days total
    # is 0 — NOT the board CREDITS KPI, which is a today-INCLUSIVE window aggregate.
    # Falling back to it would contradict this tile's "complete days only" label, the
    # 'as of' watermark (built from daily_complete), and the reconciliation caption.
    window_spend = (float(daily_complete["USD"].sum()) if not daily_complete.empty
                    else 0.0)
    # rec28: the credits behind the dollar headline, for reconciling against Snowsight.
    # This card's value IS credits x rate (warehouse metering), so credits = USD / rate —
    # exact and column-independent (the `daily` frame here is only [DAY, USD], no credits col).
    _win_credits = safe_float(window_spend) / rate if rate > 0 else None
    # One 150d metering read serves MTD here AND the forecast backtest below
    # (Codex r16 #17) — the separate 45d read survives only as the fallback
    # inside _mtd_spend_usd when this one fails.
    _bt_hist = daily_spend_wide(_PAGE)   # PERF #46: the shared wide read (also serves MTD above)
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
        {"key": f"action_queue_{company}", "sql": mart_sql.action_queue(200, company),
         "source": "ACTION_QUEUE"},
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
            now = account_now()
            month_end = (today.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
            mdf["DAY"] = pd.to_datetime(mdf["DAY"]).dt.date
            # #24: the forecast slice below keeps days STRICTLY AFTER today, so the
            # rest of TODAY (the hours past the metering already booked into mtd_now)
            # was dropped entirely — the projection omitted today's remaining spend.
            # Prorate today's OWN forecast row by the fraction of the account day
            # still ahead and add it back as an explicit today-remainder term.
            _secs_elapsed = now.hour * 3600 + now.minute * 60 + now.second
            _frac_left = max(0.0, min(1.0, 1.0 - _secs_elapsed / 86400.0))
            today_remainder_cr = float(pd.to_numeric(
                mdf.loc[mdf["DAY"] == today, "FORECAST_CREDITS"], errors="coerce"
            ).fillna(0).sum()) * _frac_left
            mdf = mdf[(mdf["DAY"] > today) & (mdf["DAY"] < month_end)]
            if not mdf.empty:
                # mtd_now rides proj_daily, already AI-split-priced (C1). The
                # FORECAST_* legs price via formulas.credits_to_usd at the compute
                # rate: FORECAST_ML_DAILY forecasts one blended-credit number per day
                # with no service split, so an exact AI-aware forecast needs the mart
                # to model AI separately (queued for V061). The KPI basis string
                # DISCLOSES this as a compute-rate estimate. Opt-in engine; 'linear'
                # is default.
                mtd_now = float(pd.to_numeric(
                    proj_daily[pd.to_datetime(proj_daily.iloc[:, 0]).dt.date >= today.replace(day=1)]
                    .iloc[:, -1], errors="coerce").fillna(0).sum()) if not proj_daily.empty else 0.0
                _fut_cr = float(pd.to_numeric(mdf["FORECAST_CREDITS"], errors="coerce").fillna(0).sum())
                add = credits_to_usd(_fut_cr + today_remainder_cr, rate, round_cents=False)
                lo = credits_to_usd(
                    float(pd.to_numeric(mdf["LOWER_BOUND"], errors="coerce").fillna(0).sum())
                    + today_remainder_cr, rate, round_cents=False)
                hi = credits_to_usd(
                    float(pd.to_numeric(mdf["UPPER_BOUND"], errors="coerce").fillna(0).sum())
                    + today_remainder_cr, rate, round_cents=False)
                forecast = MonthEndForecast(
                    ok=True, mtd_usd=round(mtd_now, 2),
                    projected_usd=round(mtd_now + add, 2),
                    low_usd=round(max(mtd_now, mtd_now + lo), 2),
                    high_usd=round(mtd_now + hi, 2),
                    daily_rate_usd=round(
                        credits_to_usd(_fut_cr, rate, round_cents=False) / max(len(mdf), 1), 2),
                    days_remaining=len(mdf),
                    basis="SNOWFLAKE.ML.FORECAST via FORECAST_ML_DAILY (opt-in script); "
                          "today's remainder prorated in; a disclosed compute-rate "
                          "estimate (AI/OTHER split queued for V061).",
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
    # C8: QUEUED_SEC and SPILL_REMOTE_GB are cumulative SUMs over a MIDNIGHT-ALIGNED
    # window that grows from 24h (just after midnight) to ~48h (late evening). Fed
    # raw into fixed 10-minute / 5-GB thresholds, an unchanged workload therefore
    # earned a penalty that doubled through the day and snapped back at midnight —
    # a sawtooth that reordered the "why is the score 74?" driver list by clock time,
    # not by anything happening on the platform. Divide by the days the window has
    # actually covered to get a per-DAY rate: stable across the day, and the same
    # basis the retro score sparkline uses (score_history feeds one day per row).
    # The failure percentages are ratios and were already time-invariant.
    # The SQL window anchors on CURRENT_DATE(), which under SiS is the UTC server
    # date (ALTER SESSION TIMEZONE is a no-op) — NOT account time. The de-cumulation
    # divisor must share that clock: in the Chicago evening UTC has already rolled to
    # the next date, so an account-time anchor sits ~a day off and the per-day rate is
    # diluted (or inflated). Derive both anchor and elapsed from the same UTC midnight
    # the SQL used.
    _now_utc = pd.Timestamp.utcnow().tz_localize(None)
    _win_start = _now_utc.normalize() - pd.Timedelta(days=_SCORE_HEALTH_WINDOW_DAYS)
    # Floor at 1.0: the window opens a full day before UTC midnight of today, so
    # elapsed is >= 1 by construction — the clamp only defends against a skewed clock,
    # and it errs toward the smaller divisor (never dilutes a real penalty away).
    _elapsed_days = max((_now_utc - _win_start).total_seconds() / 86400.0, 1.0)
    queued_minutes = (safe_float(_tr.get("QUEUED_SEC")) / 60.0 / _elapsed_days) if _tr is not None else 0.0
    spill_gb = (safe_float(_tr.get("SPILL_REMOTE_GB")) / _elapsed_days) if _tr is not None else 0.0
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
    # Perf: 'recent' (300s) to match the other health_strip sites — see main.py.
    _hs = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="recent",
              source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    stale_sources = 0
    if _hs.ok and not _hs.empty:
        _srow = _hs.df[_hs.df["METRIC"].astype(str) == "STALE_SOURCES"]
        if not _srow.empty:
            stale_sources = int(safe_float(_srow.iloc[0]["VALUE"]))
    actions_res = _live_pf.get(f"action_queue_{company}") or run(
        mart_sql.action_queue(200, company), page=_PAGE, key=f"action_queue_{company}",
        tier="live", source="ACTION_QUEUE")  # N4: reuse the first-paint batch
    open_high_actions = 0
    if actions_res.ok and not actions_res.empty:
        _adf = actions_res.df
        # codex#24: count CRITICAL as well as HIGH — an open CRITICAL action was scoring
        # ZERO penalty. The action_queue fetch now orders by severity before its cap
        # (codex#23), so every open HIGH/CRITICAL survives into this frame.
        open_high_actions = int(((_adf["STATUS"].astype(str).str.upper() == "OPEN")
                                 & (_adf["SEVERITY"].astype(str).str.upper().isin(("HIGH", "CRITICAL")))).sum())
    # C1: tell the score which health-bearing sources actually LOADED. If the
    # throughput read or the alerts read failed, their signals are silently 0 (an
    # outage would otherwise raise the score) — the score reports Incomplete instead
    # of a false green. C2/N5: the required health source is now the fixed-window
    # throughput read (not the exec board, which no longer feeds the score).
    _available = set()
    # A SUCCESSFUL throughput read is available even on a genuinely idle account
    # (QUERY_COUNT 0/NULL) — a quiet window is real health data, not a missing input,
    # so it must not force the whole score to read Incomplete. The per-day penalty
    # math is 0-query-safe (fail_pct guards on `queries`; queue/spill divide by
    # elapsed days, never by query count).
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
        # rec #37: drive the budget penalty off the PROJECTED month-end vs budget
        # (a leading signal) rather than cumulative MTD/budget, which only crosses
        # 100% late in the month once the overspend is already locked in. An account
        # on pace for 200% now contributes a penalty now, not in week 4. Falls back
        # to the cumulative ratio when no projection is available.
        "budget_pct": (
            (forecast.projected_usd / budget * 100) if (budget > 0 and forecast.ok)
            else (mtd_spend / budget * 100) if budget > 0 else 0
        ),
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "query_fail_pct": fail_pct,
        "task_fail_pct": task_fail_pct,
        "queue_minutes": queued_minutes,
        "spill_gb": spill_gb,
        "stale_sources": stale_sources,
        "open_high_actions": open_high_actions,
    }, weights=scoring.resolve_weights(settings), available=_available, degraded=_degraded)

    # ---- Decision bands ----------------------------------------------------
    _spend_spark = (daily["USD"].tail(14).tolist() if not daily.empty else None)
    _score_incomplete = score.state == "Incomplete"   # C1
    _score_sev = ("warn" if _score_incomplete
                  else "ok" if score.score >= 85 else "warn" if score.score >= 70 else "bad")
    drivers = _board_panel(board, "COST_DRIVER")
    driver_view = (
        drivers.groupby("DIMENSION", as_index=False)["VALUE_USD"]
        .sum()
        .sort_values("VALUE_USD", ascending=False)
        if not drivers.empty
        else pd.DataFrame(columns=["DIMENSION", "VALUE_USD"])
    )
    # Ov15: 'as of <date>' watermarks for the $ KPIs, from frames already in
    # memory (no query). The last COMPLETE day behind window spend, and the last
    # metering day behind MTD/projected — the honest data-through, not today.
    def _asof_of(df, col="DAY"):
        if df is None or getattr(df, "empty", True) or col not in getattr(df, "columns", []):
            return None
        ts = pd.to_datetime(df[col], errors="coerce").max()
        return ts.strftime("%Y-%m-%d") if pd.notna(ts) else None

    _ov_asof_company = _asof_of(daily_complete)
    _ov_asof_meter = _asof_of(_bt_hist.df) if _bt_hist.usable() else None

    # Ov19: vs-prior-period delta on the flagship spend tile. Mart-only (keeps
    # overview's live-scan budget at 1); hidden when the mart is absent or the
    # prior window is zero (pct_delta returns None — never a fabricated 0%).
    _ov_spend_delta = None
    _vp = run(mart_sql.fact_warehouse_window_vs_prior(days, company), page=_PAGE,
              key=f"ov_spend_vs_prior_{company}_{days}", tier="recent",
              source="FACT_WAREHOUSE_DAILY (window vs prior, loaded hourly)")
    if _vp.usable():
        _cur = float(_vp.df["CREDITS_CURRENT"].map(safe_float).sum())
        _prior = float(_vp.df["CREDITS_PRIOR"].map(safe_float).sum())
        _pct = pct_delta(_cur, _prior)
        if _pct is not None:
            _ov_spend_delta = f"{_pct:+,.0f}% vs prior {days}d"

    company_kpis = [
        {
            "label": f"Spend, {str(f['window_label']).lower()} ({company})",
            "value": format_usd(window_spend),
            "as_of": _ov_asof_company,
            "delta": _ov_spend_delta,
            "delta_color": "inverse",   # spend up = red
            "sub": f"{format_credits(_win_credits)} cr" if _win_credits is not None else None,  # rec28
            "method": "metering", "scope": "company",  # rec 13: warehouse metering, company-scoped
            "spark": _spend_spark,
            "help": "Warehouse metering credits x "
                    f"${rate:.2f}/credit ({settings.get('_source')}) — the "
                    "company-scopable lens, complete days only (today's partial excluded, "
                    "so it reconciles with Cost & Contract -> By warehouse). Serverless/AI "
                    "and the cloud-services rebate are on Cost & Contract -> Spend & "
                    "Attribution; Snowsight adds storage and transfer, so it reads higher.",
        },
    ]
    # rec36: divide by COMPLETE observed days (today's partial already dropped from
    # daily_complete), matching window_spend and every other per-day rate in the app.
    observed_days = int(daily_complete["DAY"].nunique()) if "DAY" in daily_complete.columns else 0
    if observed_days:
        company_kpis.append({
            "label": "Average per observed day",
            "value": format_usd(window_spend / observed_days),
            "method": "metering",
            "scope": "company",
            "help": "Window warehouse spend divided by the complete days present in the "
                    "served series (today's still-filling partial is excluded, matching "
                    "the window total above).",
        })
    account_kpis = [
        _mtd_pace_kpi(mtd_spend, _bt_hist, rate, ai_rate, budget) if mtd_source else {
            "label": "MTD credit spend",
            "value": "Needs daily facts",
            "method": "billed", "scope": "account-wide",
            "help": "Appears once daily metering facts are installed. This is configured-rate "
                    "credit spend, not the full invoice; storage and transfer are separate.",
        },
        {
            "label": "Projected month-end credit spend",
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
            "help": f"{company} plus account-level events — the same scope as the Alerts queue."
                    if alerts_res.ok
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
                          "previous + current calendar day and are company-scoped; budget "
                          "and stale telemetry are account-wide, and open alerts and the owner "
                          "queue are company plus account-level "
                          "— so the score does NOT move when you change the spend window. "
                          "Every deduction is itemized below the trend; sparkline = 14d retro."),
        },
    ]
    # Ov15: stamp the two headline $ account cards with the last metering day.
    if _ov_asof_meter and mtd_source:
        account_kpis[0]["as_of"] = _ov_asof_meter
    if _ov_asof_meter and forecast.ok:
        account_kpis[1]["as_of"] = _ov_asof_meter

    # #11: signed pace vs the budget's OWN straight-line expected-to-date — isolates
    # "ahead of the budget calendar right now" from the structural "will we end over"
    # the projected-month-end KPI answers. Budget-gated (config contract: no budget ->
    # no card); reuses mtd_spend + month_days, no extra query. Inserted at [1] AFTER
    # the as-of stamping so the already-stamped MTD[0]/Projected cards keep their as_of.
    if budget > 0 and mtd_source:
        _pace_var, _expected_td = budget_pace_variance(mtd_spend, budget, account_today())
        _dim, _elapsed, _rem = month_days(account_today())
        _pace_word = "ahead of" if _pace_var > 0 else "behind" if _pace_var < 0 else "on"
        _pace_sign = "+" if _pace_var > 0 else "-" if _pace_var < 0 else ""
        account_kpis.insert(1, {
            "label": "Pace vs budget calendar",
            "value": f"{_pace_sign}{format_usd(abs(_pace_var))}",
            "delta": (f"{_pace_word} straight-line "
                      f"({format_usd(_expected_td)} expected by day {_elapsed}/{_dim})"),
            # neutral delta (flat dash) — the severity stripe carries good/bad; the prose
            # delta has no leading sign, so a colored arrow would always point the same way.
            "delta_color": "off",
            # Early month (0 completed days -> expected 0) has no meaningful pace signal
            # yet; treat it as neutral rather than let the 0.15*0 threshold redden any
            # spend. From day 2 (expected > 0) the graded threshold applies as before.
            "severity": ("ok" if _expected_td <= 0
                         else "bad" if _pace_var > 0.15 * _expected_td
                         else "warn" if _pace_var > 0 else "ok"),
            "method": "billed", "scope": "account-wide",
            "as_of": _ov_asof_meter,
            "help": "Signed variance of MTD billed spend vs the budget's own straight-line "
                    "expected-to-date (MONTHLY_BUDGET_USD / days_in_month x day_of_month). "
                    "Positive = ahead of the flat daily budget target (burning fast); negative = "
                    "behind. Isolates calendar PACE from the structural 'will we end over' the "
                    "projected month-end KPI. Account-wide billed credits (AI at the AI rate).",
        })

    # CoCo do-first #1: a page-level "should I worry?" opener from signals already
    # computed above (platform score band, open alerts, budget pace) — no new query.
    _vsig = []
    if _score_incomplete:
        _vsig.append(Signal("warn", "platform health incomplete — inputs unavailable"))
    elif score.score < 70:
        _vsig.append(Signal("bad", f"platform score {score.score}/100 ({score.state})"))
    elif score.score < 85:
        _vsig.append(Signal("warn", f"platform score {score.score}/100 ({score.state})"))
    if alerts_res.ok and critical_alerts:
        _vsig.append(Signal("bad", f"{critical_alerts} open critical alert(s)"))
    elif alerts_res.ok and high_alerts:
        _vsig.append(Signal("warn", f"{high_alerts} open high alert(s)"))
    if budget > 0 and forecast.ok and forecast.projected_usd > budget:
        _over = (forecast.projected_usd / budget - 1) * 100
        _vsig.append(Signal("bad" if _over >= 15 else "warn",
                            f"projected month-end {_over:,.0f}% over budget"))
    page_verdict_line(page_verdict(
        _vsig, healthy="platform score healthy and no open critical alerts"))
    # CoCo Overview #20: the contract runway is the one committed-spend number an exec
    # needs on every visit — a persistent %-consumed bar (cheap cached mart read).
    _rw = run(mart_sql.contract_exhaustion(), page=_PAGE, key="ov_contract_runway",
              tier="recent", source="SETTINGS + FACT_METERING_DAILY")
    contract_runway_bar(contract_runway(_rw.df.iloc[0]) if _rw.usable() else None)
    st.caption("Whole-account contract commitment — not narrowed by the company filter.")

    section_header("Company economics", "info", "spend", badge=f"{company} · {days}d")
    section_filter_contract(
        f,
        applies=("company", "days"),
        note="Warehouse-metering lens; detailed Database/Schema filters do not reshape these headlines.",
    )
    panel_help(
        "Company-scoped warehouse economics for the selected window. Serverless, AI, storage, "
        "transfer, and the cloud-services rebate remain on Cost & Contract so this additive "
        "warehouse lens continues to reconcile."
    )
    kpi_row(company_kpis)

    section_header("Account risk & contract", "warn" if critical_alerts else "info", "contract")
    section_filter_contract(
        f,
        applies=(),
        partial=("company",),
        note="MTD, forecast, freshness, and owner queue are account-wide; alerts and the score "
             "use Company plus account-level events where applicable.",
    )
    panel_help(
        "Account-wide billing pace and operating risk. When the platform score is red (<70) or "
        "Incomplete, open its deductions below and work the highest-penalty driver first."
    )
    kpi_row(account_kpis)
    # CoCo Overview #10: the open-crit/high KPI is a dead-end count — give it a path
    # to the actual events, but only when there's something open to work.
    if (alerts_res.ok and (critical_alerts or high_alerts)
            and st.button("Open the alert queue →", key=f"ov_open_alerts_{company}")):
        request_navigation("Alerts", "Open events")
    # N7: MTD & Projected are credit-billed services (compute + serverless + AI).
    # Storage and data-transfer are separate invoice lines the app reads on Cost &
    # Contract (org rate-card) — disclosed here so these figures aren't mistaken for
    # the whole bill. Not folded in: that would break the credits x rate contract.
    st.caption("MTD & Projected are configured-rate credit-spend models for compute, serverless, "
               "and AI. Storage, transfer, and organization currency adjustments are separate — "
               "Cost & Contract → org rate card is billing truth.")

    # ---- The work + the drivers (rec4: above the charts, not buried below) ----
    # An executive landing page leads with what needs an owner, not two charts.
    action_lines: list[str] = []
    left, right = st.columns([1.15, 1.0])
    with left:
        section_header("Top actions")
        section_filter_contract(
            f,
            applies=("company",),
            note="Owner queue ranked by operational priority; Company narrows to that "
                 "company's actions plus account-level ones.",
        )
        panel_help(
            "The real ACTION_QUEUE — the work waiting on an owner — ranked by severity, then "
            "overdue, then estimated dollars, then age (never template rows). When rows appear "
            "here, click one to open it in the Control Room queue and assign or resolve it."
        )
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
                # rec10: a clickable surface, not a dead read-only wall — a row click
                # jumps to the Control Room where the queue is triaged (matching CR).
                # rec29: the sticky-selection guard (st.dataframe re-emits its
                # selection every rerun) now lives inside selectable_nav_table —
                # it fires on_select ONLY on a changed row, was hand-rolled here.
                def _open_action(_i: int) -> None:
                    try:
                        _action_id = str(ranked.iloc[int(_i)]["ACTION_ID"])
                    except (KeyError, IndexError, ValueError, TypeError):
                        _action_id = ""
                    request_navigation(
                        "Control Room", "Action Center",
                        context={"action_id": _action_id} if _action_id else {},
                    )

                _ov_action_cols = [c for c in ("SEVERITY", "TITLE", "OWNER", "DUE_DATE",
                                               "ESTIMATED_USD", "PERIOD")
                                   if c in ranked.columns]
                selectable_nav_table(
                    ranked[_ov_action_cols],
                    key="ov_actions_sel", slug="top-actions",
                    on_select=_open_action,
                    column_config={"ESTIMATED_USD": st.column_config.NumberColumn("Est. $", format="$%.0f"),
                                   "PERIOD": st.column_config.TextColumn("Basis")})
                # D1: say what "top" means. The ranking is severity, then overdue,
                # then estimated dollars, then age — an executive reading a top-5
                # otherwise assumes it is sorted by money, which it is not (money
                # only breaks ties inside a severity band).
                st.caption("Ranked by severity, then overdue, then estimated $, then age. "
                           "Click a row to open it in the Control Room queue.")
                result_caption(actions_res)
                action_lines = [
                    f"[{a['SEVERITY']}] {a['TITLE']} — owner {a.get('OWNER') or 'unassigned'}"
                    for _, a in ranked.iterrows()
                ]
    with right:
        section_header("Top cost drivers")
        section_filter_contract(
            f,
            applies=("company", "days"),
            note="Warehouse-compute drivers; serverless and AI are reported separately.",
        )
        if not driver_view.empty:
            view = driver_view
            # rec40: bars are click-through — clicking a warehouse jumps to Operations >
            # Warehouses pre-filtered to it. clickable_bar_usd degrades to a plain bar on
            # runtimes without altair on_select, and its return is guarded to fire once
            # per NEW click (so returning to this page with a sticky selection won't bounce).
            # Gate the affordance to viewers whose profile HAS Operations: an EXECUTIVE
            # can't open Operations, so request_navigation would clamp the jump back to
            # Overview yet still apply warehouse_contains — a dead click that silently
            # leaks a cross-page scope filter. They get a plain (non-clickable) bar instead.
            from app.config import PAGES_BY_PROFILE, resolve_role_profile
            from app.core.session import current_role
            _can_ops = "Operations" in PAGES_BY_PROFILE.get(
                resolve_role_profile(current_role()), ())
            if _can_ops:
                _picked_wh = charts.clickable_bar_usd(
                    view, "DIMENSION", "VALUE_USD", key="ov_drivers_bar", title="Spend (USD)")
                if _picked_wh:
                    request_navigation("Operations", "Warehouses",
                                       {"warehouse_contains": _picked_wh})
            else:
                charts.bar_usd(view, "DIMENSION", "VALUE_USD", title="Spend (USD)")
            # rec15: lead with the conclusion — which driver dominates, and by how much.
            _dtot = float(view["VALUE_USD"].map(safe_float).sum())
            if _dtot > 0 and len(view):
                _d0 = view.iloc[0]
                # C5: this panel is warehouse compute ONLY — operational credits at the
                # compute rate, from FACT_WAREHOUSE_DAILY — so it matches the headline
                # KPIs (also warehouse-only) and the drivers reconcile to the KPI total.
                # Serverless (tasks, Snowpipe, MV refresh) and AI/Cortex bill on separate
                # meters and appear in their own panel below (V069 COST_DRIVER_SVC), never
                # mixed into this "% of warehouse compute spend" denominator.
                st.caption(f"Top driver: **{_d0['DIMENSION']}** — {format_usd(safe_float(_d0['VALUE_USD']))} "
                           f"({safe_float(_d0['VALUE_USD']) / _dtot * 100:.0f}% of warehouse "
                           "compute spend — serverless & AI shown separately below).")
        elif not using_mart and not daily.empty:
            st.caption("Driver ranking appears once the exec board mart is installed.")
        else:
            st.info("No cost-driver rows for this scope/window.")

        # V069 (audit C5): serverless & AI/Cortex drivers on a DISTINCT board panel
        # (COST_DRIVER_SVC), rendered as their own small table beneath the warehouse
        # drivers. Kept separate so the warehouse KPIs/denominator above stay
        # warehouse-only and keep reconciling; this panel's basis is BILLED $ (AI/Cortex
        # at the AI rate, the rest at the compute rate), not the warehouse panel's
        # operational credits — the two bases are never mixed. Account-level metering
        # carries no company dimension, so the mart emits these on the ALL scope only;
        # absent under a company pill, which is expected.
        svc = _board_panel(board, "COST_DRIVER_SVC")
        if not svc.empty:
            svc_view = (svc.groupby("DIMENSION", as_index=False)["VALUE_USD"].sum()
                        .sort_values("VALUE_USD", ascending=False)
                        .rename(columns={"DIMENSION": "DRIVER", "VALUE_USD": "BILLED_USD"}))
            st.caption("Serverless & AI billed spend — separate from the warehouse-compute "
                       "drivers above (billed $: AI/Cortex at the AI rate, the rest at the "
                       "compute rate).")
            styled_table(svc_view, height=TABLE_H_MD, slug="serverless-ai-drivers", column_config={
                "BILLED_USD": st.column_config.NumberColumn("Billed $", format="$%.0f"),
            })

    # ---- Monthly spend by warehouse (owner ask 2026-07-11: the boss chart) --
    section_header("Monthly spend by warehouse")
    section_filter_contract(
        f,
        applies=("company",),
        note="Fixed trailing 12-month view; the global Window does not shorten this chart.",
    )
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
            # $-escape via the house helper (was an ad-hoc chr(92) patch, 2026-07-11):
            # two bare $ in one st.caption pair into a LaTeX math span.
            st.caption(md_dollars(f"Last full month {_full.index[-1]}: "
                       f"{format_usd(_full.iloc[-1])} "
                       f"({_mom:+.1f}% vs prior). "
                       "Current month is dimmed — partial, not a drop. "
                       f"Dollars at today's ${rate:.2f}/credit."))
            # rec16: 'who moved' beats eyeballing stacked segments — the warehouses
            # with the largest absolute MoM change (last full month vs the prior).
            _last_m, _prev_m = _full.index[-1], _full.index[-2]
            _piv = (_md[_md["MONTH"].isin([_last_m, _prev_m])]
                    .pivot_table(index="WAREHOUSE_NAME", columns="MONTH", values="USD",
                                 aggfunc="sum").fillna(0.0))
            if _prev_m in _piv.columns and _last_m in _piv.columns:
                _mv = pd.DataFrame({"WAREHOUSE": _piv.index,
                                    "PRIOR_USD": _piv[_prev_m].to_numpy(),
                                    "LATEST_USD": _piv[_last_m].to_numpy()})
                _mv["DELTA_USD"] = _mv["LATEST_USD"] - _mv["PRIOR_USD"]
                _mv["DELTA_PCT"] = [(d / p * 100.0) if p > 0 else 0.0
                                    for d, p in zip(_mv["DELTA_USD"], _mv["PRIOR_USD"], strict=True)]
                _mv = _mv.iloc[_mv["DELTA_USD"].abs().argsort()[::-1]].head(6)
                st.caption(f"Top movers — {_last_m} vs {_prev_m}")
                entity_nav_table(_mv, key=f"ov_wh_movers_{company}", key_col="WAREHOUSE",
                                 entity_type="WAREHOUSE", height=TABLE_H_MD,
                                 slug="warehouse-movers", column_config={
                    "PRIOR_USD": st.column_config.NumberColumn("Prior $", format="$%.0f"),
                    "LATEST_USD": st.column_config.NumberColumn("Latest $", format="$%.0f"),
                    "DELTA_USD": st.column_config.NumberColumn("Δ $", format="$%+.0f"),
                    "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%+.1f%%"),
                })
        result_caption(_mres)

    # ---- Spend trend ---------------------------------------------------------
    section_header("Spend trend")
    section_filter_contract(
        f,
        applies=("company", "days"),
        note="Warehouse metering at the configured compute-credit rate.",
    )
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
        # Ov5: flag anomalous spend days (pure robust-z, no scan — scale-invariant,
        # so it works whether daily carries USD or raw credits) and mark them.
        from app.logic.anomaly import (
            ANOMALY_MIN_ACTIVE_DAYS,
            anomaly_markers,
            complete_days_only,
            flag_anomalies,
            suppress_expected_spikes,
        )
        _dc = daily.columns[0]
        _uc = next((c for c in daily.columns if "USD" in str(c).upper() or "CREDIT" in str(c).upper()),
                   daily.columns[-1])
        _flag = flag_anomalies(
            complete_days_only(daily.rename(columns={_dc: "DAY", _uc: "USD"}), "DAY"),
            "USD", min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
        # Known-spike calendar: the same suppression the Cost sweep applies, so a
        # month-end day never reads "expected" there but anomalous here.
        _flag = suppress_expected_spikes(
            _flag, str(settings.get("EXPECTED_SPIKE_CALENDAR") or ""))
        charts.spend_trend(daily, daily_budget_usd=daily_budget,
                           markers=anomaly_markers(_flag, "DAY"))
        activity = run(mart_sql.fact_daily_activity(14, company), page=_PAGE,
                       key="spark_activity", tier="hourly",
                       source="FACT_QUERY_HOURLY (daily)")
        adf = activity.df if activity.ok and not activity.empty else None
        # The full spend_trend line chart directly above already shows spend; the
        # sparkline_row keeps only the series that chart does NOT carry (queries,
        # failures) so it stops duplicating the trend chart.
        charts.sparkline_row([
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
                       + f" — '{_best}' most reliable; running '{engine}'. "
                       "Account-wide backtest — metering has no company grain.")
        with st.expander("Forecast accuracy — how the projection performed, last 3 months"):
            if not _bt_hist.usable() or len(_bt_hist.df) < 50:
                st.info("Needs ~2 months of daily facts before a backtest says anything.")
            elif _bt.empty:
                st.info("No complete months in the window yet.")
            else:
                styled_table(_bt, height=TABLE_H_MD, column_config={
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
            # E3: the point values are UNCALIBRATED defaults until someone tunes them
            # against this account's incident history. Saying so here — where the
            # arithmetic is on screen — stops "−6 pts per critical" from reading as a
            # measured fact. Once SETTINGS overrides any weight, the line disappears.
            if scoring.resolve_weights(settings) == scoring.DEFAULT_WEIGHTS:
                st.caption("Point values are the shipped defaults — uncalibrated starting "
                           "points, not measured impact. Tune them per driver via the "
                           "SCORE_PTS_* settings on Admin → Settings. '(capped)' means the "
                           "driver is pinned at its maximum: more of it will not lower the "
                           "score further.")

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
                "alerts, telemetry and owner-queue signals, so its level can differ. "
                "Month-to-date spend restarts at the left edge of this window, so "
                "the budget penalty is understated until the first whole month begins "
                "— read the first few days as unreliable, not as a real improvement."
            )

    # ---- Daily AI digest ------------------------------------------------------
    digest = run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="hourly",
                 source="DAILY_DIGEST (Cortex, grounded in the exec board)")
    if digest.usable():
        row = digest.df.iloc[0]
        with st.expander(f"Morning AI digest — {row.get('DIGEST_DATE')} ({row.get('MODEL')})",
                         expanded=False):
            st.markdown(str(row.get("BODY") or ""))
            st.caption("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts "
                       "only. Account-wide narrative — does not change with the company filter.")

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
    # rec20: hand the export the daily spend series so it renders a trend sparkline
    # (defensive — daily/usd_col may be absent when spend history hasn't loaded).
    _export_spark = None
    if not daily.empty:
        _uc = next((c for c in daily.columns if "USD" in str(c).upper() or "CREDIT" in str(c).upper()),
                   daily.columns[-1] if len(daily.columns) else None)
        if _uc is not None:
            _export_spark = [safe_float(v) for v in daily[_uc].tail(30).tolist()]
    _export_view = ExecutiveSummaryView(
        company=company,
        days=days,
        generated=account_now().strftime("%Y-%m-%d %H:%M") + " (account time)",
        cards=(
            ("Window spend", f"{format_usd(window_spend)} - {company}, metering"),
            ("Month to date", _mtd_export),
            ("Projected month-end", _fc_export),
            ("Open alerts", f"{critical_alerts} critical | {high_alerts} high"),
            ("Platform score", _score_export),
        ),
        drivers=tuple(
            (driver.driver, f"-{driver.penalty:.1f} pts", driver.evidence)
            for driver in score.drivers
        ),
        actions=tuple(action_lines),
        spend_series=tuple(_export_spark or []),
        scope_notes=(
            "Window spend is company-scoped warehouse metering at the configured credit rate; "
            "it excludes the account-level cloud-services adjustment.",
            "MTD and projected spend are account-wide billed credits with the cloud-services "
            "adjustment applied. Daily metering can lag up to 24 hours.",
            "An Incomplete platform score means required health inputs did not load.",
        ),
    )
    html = executive_summary_html(_export_view, presentation=True)
    summary = executive_slide_bullets(_export_view)
    c_html, c_txt, c_csv = st.columns(3)
    with c_html:
        export_button("Presentation summary (HTML)", html,
                      file_name="overwatch_executive_summary.html", mime="text/html",
                      use_container_width=True)
    with c_txt:
        download_text_button("Slide bullets (.txt)", summary,
                             "overwatch_executive_slide_bullets.txt")
    with c_csv:
        export_button("Summary data (CSV)", executive_summary_csv(_export_view),
                      file_name="overwatch_executive_summary.csv", mime="text/csv",
                      use_container_width=True)
