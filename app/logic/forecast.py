"""Month-end spend projection with an honest uncertainty band.

Simple, explainable math (recent daily average + variability band), because an
executive will ask "how did you get this number" and the answer must fit in
one sentence. No fabricated series: with insufficient history the projection
declines to guess (``ok=False``) instead of inventing a line.
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


def month_end_projection(daily: pd.DataFrame, today: date, engine: str = "linear") -> MonthEndForecast:
    """Project month-end spend from a ``DAY``/``USD`` daily frame.

    Linear engine: complete-day MTD + a robust (Theil-Sen) daily trend over the
    recent baseline — the 'Linear' label finally carries a slope. Seasonal
    engine: complete-day MTD + per-weekday means over >= 4 weeks. Band: residual
    std (ddof=1) against the fitted line, scaled by sqrt(remaining days) and
    inflated for parameter uncertainty and within-week autocorrelation (rec#15).
    """
    if daily is None or daily.empty or not {"DAY", "USD"}.issubset(daily.columns):
        return MonthEndForecast(ok=False, basis="No daily spend history loaded.")

    frame = daily.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    frame["USD"] = frame["USD"].map(safe_float)
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")

    month_start = today.replace(day=1)
    mtd = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] <= today)]["USD"].sum())
    # codex#16: project from COMPLETE-day actuals and count TODAY as a projected (still
    # incomplete) day. `mtd` already includes today's PARTIAL actual, but the projection
    # only added days AFTER today — so today's remaining hours were never estimated and the
    # month-end number ran low all day. `mtd` stays the displayed spend-so-far.
    mtd_complete = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] < today)]["USD"].sum())

    # N1: today is a PARTIAL day — averaging it into the daily rate biases every
    # projection low (same class as the pace/anomaly partial-day fixes). MTD above
    # keeps today's actual; the forward rate is built only from completed days.
    complete = frame[frame["DAY"] < today]
    baseline = complete.tail(_BASELINE_DAYS)
    if len(baseline) < _MIN_POINTS:
        return MonthEndForecast(
            ok=False,
            mtd_usd=round(mtd, 2),
            basis=f"Needs at least {_MIN_POINTS} days of history; have {len(baseline)}.",
        )

    _, _, remaining = month_days(today)
    project_days = remaining + 1   # codex#16: today (incomplete) + every day after it

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
        future = [today + timedelta(days=i) for i in range(remaining + 1)]  # codex#16: incl TODAY
        add = sum(float(dow_mean.get(d.weekday(), fallback_rate)) for d in future)
        # month-end is monotonic: it can never fall below spend-to-date (mtd, which
        # already includes today's partial). Flooring here keeps the point estimate
        # and its band self-consistent (low <= projected <= high) under any trend.
        projected = max(mtd_complete + add, mtd)
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
                  f"(>= 4 samples/weekday), today + {remaining} remaining days per weekday.",
        )

    # Linear engine (rec#15): a robust Theil-Sen daily trend, not a flat mean —
    # future days ride the fitted line from the last complete day (clamped at 0),
    # and the band uses residuals against THAT line, not the raw scatter.
    xs = [float(i) for i in range(len(baseline))]
    ys = [safe_float(value) for value in baseline["USD"]]
    slope, intercept = _robust_slope(xs, ys)
    last_x = len(baseline) - 1
    fitted_future = [max(0.0, intercept + slope * (last_x + k)) for k in range(1, project_days + 1)]
    add = sum(fitted_future)
    # rec#15 guard: a steep downward trend can extrapolate below spend-to-date (and
    # every clamped-to-0 future day drives `add` toward 0). Month-end is monotonic —
    # it can never be below `mtd` (which counts today's partial) — so floor the point
    # estimate there. This keeps low <= projected <= high even on a clean decline
    # (zero residual -> zero band) instead of inverting the interval.
    projected = max(mtd_complete + add, mtd)
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
              f"(today + {remaining} remaining) days.",
    )


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
    ACTUAL_USD, ERROR_PCT.
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
                rows.append({
                    "MONTH": month_start.strftime("%Y-%m"),
                    "CHECKPOINT_DAY": checkpoint,
                    "ENGINE": engine,
                    "PROJECTED_USD": round(cast.projected_usd, 0),
                    "ACTUAL_USD": round(actual, 0),
                    "ERROR_PCT": round(err, 1),
                })
        month_end = month_start
    return pd.DataFrame(rows)
