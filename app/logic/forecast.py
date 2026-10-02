"""Month-end spend projection with an honest uncertainty band.

Simple, explainable math, because an executive will ask "how did you get this
number" and the answer must fit in one sentence: the linear (default) engine fits
a robust (Theil-Sen) calendar-day trend over the last 14 complete-day rows and
projects it forward from today; the seasonal engine uses day-of-week means over
up to 6 weeks of complete days (it needs >= 28, else it falls back to linear).
Either way the band is the residual std against that fit x sqrt(days projected),
widened for parameter uncertainty and within-week autocorrelation. No fabricated
series: with insufficient history the projection declines to guess
(``ok=False``) instead of inventing a line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

import pandas as pd

from .formulas import month_days, safe_float

_BASELINE_DAYS = 14              # linear engine: recent daily rate + robust trend
_SEASONAL_DAYS = 42              # rec#15: >= 6 weeks so each weekday gets ~6 samples
_MIN_POINTS = 7                  # rec#15: a full week is the floor for any projection
_MIN_SEASONAL_POINTS = 28        # rec#15: >= 4 samples/weekday before a DOW split is trusted
_BAND_AUTOCORR_INFLATION = 1.25  # rec#15: daily spend is not i.i.d. within a week


def _robust_slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Theil-Sen slope/intercept — the median pairwise slope, robust to the
    one-off spikes least squares would chase."""
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    slope = float(median(slopes)) if slopes else 0.0
    intercept = float(median([y - slope * x for x, y in zip(xs, ys, strict=True)]))
    return slope, intercept


def _band(resid_std: float, project_days: int, n: int) -> float:
    """Uncertainty half-width for a ``project_days``-day forward sum (rec#15).

    An i.i.d. sum spreads ``resid_std * sqrt(days)``; real daily spend is
    autocorrelated within the week and the fitted level/slope are themselves
    estimated from ``n`` points, so widen for both. A perfectly flat or
    perfectly periodic history (zero residual) still yields a zero band.
    """
    if resid_std <= 0.0 or project_days <= 0:
        return 0.0
    param_infl = (1.0 + 1.0 / max(n, 1)) ** 0.5
    return resid_std * (project_days ** 0.5) * param_infl * _BAND_AUTOCORR_INFLATION


@dataclass(frozen=True)
class MonthEndForecast:
    ok: bool
    mtd_usd: float = 0.0
    projected_usd: float = 0.0
    low_usd: float = 0.0
    high_usd: float = 0.0
    daily_rate_usd: float = 0.0
    days_remaining: int = 0
    basis: str = ""


def month_end_projection(daily: pd.DataFrame, today: date, engine: str = "linear",
                         complete_before: date | None = None) -> MonthEndForecast:
    """Project month-end spend from a ``DAY``/``USD`` daily frame.

    Linear engine: complete-day MTD + a robust (Theil-Sen) daily trend over the
    recent baseline — the 'Linear' label finally carries a slope. Seasonal
    engine: complete-day MTD + per-weekday means over >= 4 weeks. Band: residual
    std (ddof=1) against the fitted line, scaled by sqrt(remaining days) and
    inflated for parameter uncertainty and within-week autocorrelation (rec#15).

    ``complete_before`` (R2-050): the first day that is not complete (default ``today``). A
    FACT_METERING_DAILY frame passes formulas.metering_complete_before: before the 06:45 Central load its
    newest row (yesterday) is still the partial in-progress UTC day, so it is projected with today and the
    rest of the month instead of being counted as a whole day (on Sep 15 at 03:00 a flat $1,000/day month
    projected $29,450, not $30,000). ``mtd`` (spend so far) still counts every row up to today.
    """
    if daily is None or daily.empty or not {"DAY", "USD"}.issubset(daily.columns):
        return MonthEndForecast(ok=False, basis="No daily spend history loaded.")

    frame = daily.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    frame["USD"] = frame["USD"].map(safe_float)
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")

    month_start = today.replace(day=1)
    cut = today if complete_before is None else min(complete_before, today)   # first incomplete day
    # The projected window starts at the first incomplete day of THIS month: a still-loading last day of the
    # prior month (the 1st, before the load) is left out of the complete baseline but is no October spend.
    start = max(cut, month_start)
    mtd = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] <= today)]["USD"].sum())
    # codex#16: project from COMPLETE-day actuals and count TODAY as a projected (still
    # incomplete) day. `mtd` already includes today's PARTIAL actual, but the projection
    # only added days AFTER today — so today's remaining hours were never estimated and the
    # month-end number ran low all day. `mtd` stays the displayed spend-so-far.
    mtd_complete = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] < start)]["USD"].sum())

    # N1: today is a PARTIAL day — averaging it into the daily rate biases every
    # projection low (same class as the pace/anomaly partial-day fixes). MTD above
    # keeps today's actual; the forward rate is built only from completed days.
    complete = frame[frame["DAY"] < cut]
    baseline = complete.tail(_BASELINE_DAYS)
    if len(baseline) < _MIN_POINTS:
        return MonthEndForecast(
            ok=False,
            mtd_usd=round(mtd, 2),
            basis=f"Needs at least {_MIN_POINTS} days of history; have {len(baseline)}.",
        )

    _, _, remaining = month_days(today)
    # codex#16: today (incomplete) + every day after it; R2-050: plus any still-loading day before today
    project_days = remaining + 1 + (today - start).days

    # r33: a completed day MISSING from the frame is counted in NEITHER mtd_complete NOR
    # `add` (which starts at today), so the month-end number reads low by that day's spend.
    # fact_daily_spend GROUPs BY DAY with no date spine, so an ingest-lagged (or genuinely
    # idle) day simply has no row. The projection already models every FUTURE calendar day
    # at ~the recent mean rate; a past GAP day is the same under that model, so fill the
    # missing COMPLETE days at the baseline mean instead of counting them as zero. Only days
    # inside the LOADED coverage (>= the earliest row) count as gaps — days before the
    # frame's first row are un-loaded history, not gaps, and must never be fabricated. Guard
    # to a DENSE window (majority of covered days present) so a genuinely sparse/idle account
    # is not handed a fabricated month.
    cover_start = max(month_start, frame["DAY"].min())
    covered_days = max(0, (start - cover_start).days)
    present_days = int(((frame["DAY"] >= cover_start) & (frame["DAY"] < start)).sum())
    missing_days = max(0, covered_days - present_days)
    # Judge density over at least the trailing baseline window, not month-to-date alone: on the
    # 2nd with the 1st lagging, MTD is 0 of 1 days present and the guard refused to fill the one
    # lagged day, so month-end read a day low on the first days of every month. Only the MTD
    # missing days are filled; the wider window only decides whether the account is dense.
    dens_start = max(frame["DAY"].min(), start - timedelta(days=max(covered_days, _BASELINE_DAYS)))
    dens_days = max(1, (start - dens_start).days)
    dens_present = int(((frame["DAY"] >= dens_start) & (frame["DAY"] < start)).sum())
    gap_fill = (missing_days * float(baseline["USD"].mean())
                if missing_days and dens_present >= dens_days / 2 else 0.0)

    # rec#15: a 14-day window gives day-of-week means only 2 samples each, so the
    # seasonal band came out over-narrow and over-confident. Widen the seasonal
    # baseline to 6 weeks and refuse the DOW split below 4 weeks (falls through to
    # the linear engine), so each weekday mean rests on >= 4 observations.
    seasonal_baseline = complete.tail(_SEASONAL_DAYS)
    if engine == "seasonal" and len(seasonal_baseline) >= _MIN_SEASONAL_POINTS:
        frame_b = seasonal_baseline.copy()
        frame_b["DOW"] = pd.to_datetime(frame_b["DAY"]).map(lambda d: d.weekday())
        dow_mean = frame_b.groupby("DOW")["USD"].mean()
        resid = frame_b["USD"] - frame_b["DOW"].map(dow_mean)
        resid_std = float(resid.std(ddof=1)) if len(frame_b) > 1 else 0.0
        fallback_rate = float(seasonal_baseline["USD"].mean())
        future = [start + timedelta(days=i) for i in range(project_days)]  # codex#16: incl TODAY
        add = sum(float(dow_mean.get(d.weekday(), fallback_rate)) for d in future)
        # month-end is monotonic: it can never fall below spend-to-date (mtd, which
        # already includes today's partial). Flooring here keeps the point estimate
        # and its band self-consistent (low <= projected <= high) under any trend.
        projected = max(mtd_complete + add + gap_fill, mtd)
        spread = _band(resid_std, project_days, len(frame_b))
        return MonthEndForecast(
            ok=True,
            mtd_usd=round(mtd, 2),
            projected_usd=round(projected, 2),
            low_usd=round(max(mtd, projected - spread), 2),
            high_usd=round(projected + spread, 2),
            daily_rate_usd=round(add / project_days, 2) if project_days else 0.0,
            days_remaining=remaining,
            basis=f"Seasonal engine: day-of-week means over {len(frame_b)}d "
                  f"(>= 4 samples/weekday), {_projected_span(start, today, remaining, project_days)} per weekday."
                  + _loading_note(cut, start, today),
        )

    # Linear engine (rec#15): a robust Theil-Sen daily trend, not a flat mean —
    # future days ride the fitted line from TODAY (clamped at 0), and the band uses residuals
    # against THAT line, not the raw scatter.
    # Fit on CALENDAR-DAY offsets, not row indices (like capacity._theil_sen): the baseline
    # has no date spine (an ingest-lagged or idle day simply has no row, r33), so a row-index
    # slope is per-ROW and a gap inflates/deflates the forward extrapolation — a per-DAY slope
    # stays correct regardless of gaps (bug-hunt we2ahd4d0).
    if len(baseline):
        _origin = baseline["DAY"].iloc[0]
        xs = [float((_d - _origin).days) for _d in baseline["DAY"]]
        start_x = float((start - _origin).days)
    else:
        xs = []
        start_x = 0.0
    ys = [safe_float(value) for value in baseline["USD"]]
    slope, intercept = _robust_slope(xs, ys)
    # Anchor the forward window on TODAY (k=0 is today, the incomplete day), NOT the last PRESENT
    # complete day: metering lags ~1-2d so the last present day is routinely today-2, and a last_x
    # anchor would project days that gap_fill ALREADY estimates (double-count) and drop the month
    # tail — gap_fill's own r33 comment says `add` "starts at today" (bug-hunt wdmz68vd4). Matches
    # the seasonal engine's today-anchored future window; with no trailing gap start_x == last_x+1.
    # R2-050: k=0 is the first incomplete day (today, or the still-loading day before it).
    fitted_future = [max(0.0, intercept + slope * (start_x + k)) for k in range(project_days)]
    add = sum(fitted_future)
    # rec#15 guard: a steep downward trend can extrapolate below spend-to-date (and
    # every clamped-to-0 future day drives `add` toward 0). Month-end is monotonic —
    # it can never be below `mtd` (which counts today's partial) — so floor the point
    # estimate there. This keeps low <= projected <= high even on a clean decline
    # (zero residual -> zero band) instead of inverting the interval.
    projected = max(mtd_complete + add + gap_fill, mtd)
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(len(ys))]
    resid_std = float(pd.Series(resid).std(ddof=1)) if len(resid) > 1 else 0.0
    spread = _band(resid_std, project_days, len(baseline))
    return MonthEndForecast(
        ok=True,
        mtd_usd=round(mtd, 2),
        projected_usd=round(projected, 2),
        low_usd=round(max(mtd, projected - spread), 2),
        high_usd=round(projected + spread, 2),
        daily_rate_usd=round(add / project_days, 2) if project_days else 0.0,
        days_remaining=remaining,
        basis=f"Linear engine: complete-day MTD + robust {_BASELINE_DAYS}d trend x "
              f"{_projected_span(start, today, remaining, project_days)}." + _loading_note(cut, start, today),
    )


def _projected_span(start: date, today: date, remaining: int, project_days: int) -> str:
    """The projected days in the basis: 'today + N remaining days', or -- when not-yet-loaded days before today
    are projected too (R2-050) -- every projected day from the first one, so the count matches the projection."""
    if start >= today:
        return f"today + {remaining} remaining days"
    return f"{project_days} days ({start.isoformat()} through month end)"


def _loading_note(cut: date, start: date, today: date) -> str:
    """R2-050: name the newest loaded metering day and the days the projection estimated instead of counting.

    ``cut`` is the first incomplete day: for a FACT_METERING_DAILY frame (formulas.metering_complete_before) the
    newest loaded DAY, which was still in progress at its 06:45 Central load. ``start`` is the first projected day
    of this month. After a failed or late load the newest row can be days old, or in the prior month, so the note
    names it and the whole projected span rather than calling ``start`` the newest metering day."""
    if start >= today:
        return ""
    last = today - timedelta(days=1)
    span = (f"{start.isoformat()} is" if start == last
            else f"{start.isoformat()} to {last.isoformat()} are")
    if cut < start:
        return (f" No day of this month has loaded yet (metering is loaded through {cut.isoformat()}, still in "
                f"progress at its 06:45 Central load), so {span} projected with today.")
    return (f" Metering is loaded through {cut.isoformat()}, which was still in progress at its 06:45 Central "
            f"load, so {span} projected with today rather than counted as complete.")


def contract_pace(
    consumed_credits: float,
    contract_credits: float,
    contract_start: date,
    contract_end: date,
    today: date,
    trailing_daily_credits: float | None = None,
) -> dict:
    """Contract burn pacing: consumed share vs elapsed-time share.

    pace_ratio > 1.0 means burning faster than the contract clock.

    N11: when ``trailing_daily_credits`` is supplied, the forward projection uses
    that recent daily burn (the same trailing-30d basis the renewal planner and
    year strip use) so the prominent KPI can't contradict the planner below it —
    booked ``consumed`` plus the remaining days at recent burn. Without it, the
    lifetime-average fallback is kept for backward compatibility. The share/pace
    fields stay lifetime ACTUALS (are-you-ahead-of-the-clock), which don't conflict.
    """
    total = safe_float(contract_credits)
    term_days = (contract_end - contract_start).days
    if total <= 0 or term_days <= 0 or today < contract_start:
        return {"ok": False, "reason": "Contract not configured or not started."}
    elapsed_days = min((today - contract_start).days + 1, term_days)
    time_share = elapsed_days / term_days
    consumed_share = safe_float(consumed_credits) / total
    pace_ratio = consumed_share / time_share if time_share > 0 else 0.0
    if trailing_daily_credits is not None and safe_float(trailing_daily_credits) >= 0:
        run_rate_daily = safe_float(trailing_daily_credits)
        projected_total = safe_float(consumed_credits) + run_rate_daily * (term_days - elapsed_days)
        basis = "trailing-30d burn"
    else:  # backward-compatible lifetime fallback (no daily frame available)
        run_rate_daily = safe_float(consumed_credits) / max(elapsed_days, 1)
        projected_total = run_rate_daily * term_days
        basis = "lifetime average"
    return {
        "ok": True,
        "consumed_share": round(consumed_share * 100, 1),
        "time_share": round(time_share * 100, 1),
        "pace_ratio": round(pace_ratio, 2),
        "projected_term_credits": round(projected_total, 1),
        "projected_overage_credits": round(max(0.0, projected_total - total), 1),
        "days_remaining": term_days - elapsed_days,
        "basis": basis,
    }


def backtest_forecasts(daily: pd.DataFrame, months: int = 3,
                       checkpoints: tuple[int, ...] = (7, 14, 21)) -> pd.DataFrame:
    """How accurate would the projection have been? Retro-runs the engines.

    For each of the last ``months`` COMPLETE months and each checkpoint day,
    projects month-end using only the history available on that day, then
    compares with the month's actual total. Pure — the page supplies the
    daily USD frame. Columns: MONTH, CHECKPOINT_DAY, ENGINE, PROJECTED_USD,
    LOW_USD, HIGH_USD, ACTUAL_USD, ERROR_PCT, COVERED (actual within the band —
    for interval-coverage and directional-bias reporting, not just point error).
    """
    if daily is None or daily.empty or not {"DAY", "USD"}.issubset(daily.columns):
        return pd.DataFrame()
    frame = daily.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")
    if frame.empty:
        return pd.DataFrame()
    last_day = frame["DAY"].max()
    first_of_current = last_day.replace(day=1)
    rows: list[dict] = []
    month_end = first_of_current
    for _ in range(max(1, int(months))):
        month_start = (date(month_end.year - 1, 12, 1) if month_end.month == 1
                       else date(month_end.year, month_end.month - 1, 1))
        month_block = frame[(frame["DAY"] >= month_start) & (frame["DAY"] < month_end)]
        if len(month_block) < 20:  # partial history: stop walking back
            break
        actual = float(month_block["USD"].sum())
        for checkpoint in checkpoints:
            as_of = month_start + timedelta(days=checkpoint - 1)
            history = frame[frame["DAY"] <= as_of]
            for engine in ("linear", "seasonal"):
                cast = month_end_projection(history, as_of, engine=engine)
                if not cast.ok:
                    continue
                err = (cast.projected_usd - actual) / actual * 100 if actual else 0.0
                # R27: score the INTERVAL, not just the point. The band [low,high] is already
                # computed per checkpoint but was discarded; keep it and flag whether the actual
                # landed inside, so the panel can report interval coverage (are the bands
                # trustworthy?) and directional bias (signed ERROR_PCT), not just point error.
                rows.append({
                    "MONTH": month_start.strftime("%Y-%m"),
                    "CHECKPOINT_DAY": checkpoint,
                    "ENGINE": engine,
                    "PROJECTED_USD": round(cast.projected_usd, 0),
                    "LOW_USD": round(cast.low_usd, 0),
                    "HIGH_USD": round(cast.high_usd, 0),
                    "ACTUAL_USD": round(actual, 0),
                    "ERROR_PCT": round(err, 1),
                    "COVERED": bool(cast.low_usd <= actual <= cast.high_usd),
                })
        month_end = month_start
    return pd.DataFrame(rows)
