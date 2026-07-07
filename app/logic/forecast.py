"""Month-end spend projection with an honest uncertainty band.

Simple, explainable math (recent daily average + variability band), because an
executive will ask "how did you get this number" and the answer must fit in
one sentence. No fabricated series: with insufficient history the projection
declines to guess (``ok=False``) instead of inventing a line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .formulas import month_days, safe_float

_BASELINE_DAYS = 14
_MIN_POINTS = 3


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

    projected = MTD actual + recent-daily-average * remaining days.
    Band: +/- 1 std of the recent daily values * sqrt(remaining days).
    """
    if daily is None or daily.empty or not {"DAY", "USD"}.issubset(daily.columns):
        return MonthEndForecast(ok=False, basis="No daily spend history loaded.")

    frame = daily.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    frame["USD"] = frame["USD"].map(safe_float)
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")

    month_start = today.replace(day=1)
    mtd = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] <= today)]["USD"].sum())

    baseline = frame[frame["DAY"] <= today].tail(_BASELINE_DAYS)
    if len(baseline) < _MIN_POINTS:
        return MonthEndForecast(
            ok=False,
            mtd_usd=round(mtd, 2),
            basis=f"Needs at least {_MIN_POINTS} days of history; have {len(baseline)}.",
        )

    daily_rate = float(baseline["USD"].mean())
    daily_std = float(baseline["USD"].std(ddof=0))
    _, _, remaining = month_days(today)

    if engine == "seasonal" and len(baseline) >= 14:
        # Day-of-week means over the baseline; each remaining calendar day is
        # projected with its own weekday mean. Band = per-day residual std
        # against the weekday means, scaled by sqrt(remaining).
        from datetime import timedelta

        frame_b = baseline.copy()
        frame_b["DOW"] = pd.to_datetime(frame_b["DAY"]).map(lambda d: d.weekday())
        dow_mean = frame_b.groupby("DOW")["USD"].mean()
        resid = frame_b["USD"] - frame_b["DOW"].map(dow_mean)
        resid_std = float(resid.std(ddof=0))
        future = [today + timedelta(days=i) for i in range(1, remaining + 1)]
        add = sum(float(dow_mean.get(d.weekday(), daily_rate)) for d in future)
        projected = mtd + add
        spread = resid_std * (remaining**0.5)
        return MonthEndForecast(
            ok=True,
            mtd_usd=round(mtd, 2),
            projected_usd=round(projected, 2),
            low_usd=round(max(mtd, projected - spread), 2),
            high_usd=round(projected + spread, 2),
            daily_rate_usd=round(add / remaining, 2) if remaining else 0.0,
            days_remaining=remaining,
            basis=f"Seasonal engine: day-of-week means over {len(baseline)}d, "
                  f"{remaining} remaining days projected per weekday.",
        )

    projected = mtd + daily_rate * remaining
    spread = daily_std * (remaining**0.5)
    return MonthEndForecast(
        ok=True,
        mtd_usd=round(mtd, 2),
        projected_usd=round(projected, 2),
        low_usd=round(max(mtd, projected - spread), 2),
        high_usd=round(projected + spread, 2),
        daily_rate_usd=round(daily_rate, 2),
        days_remaining=remaining,
        basis=f"Linear engine: MTD actual + {_BASELINE_DAYS}d avg daily rate x {remaining} remaining days.",
    )


def contract_pace(
    consumed_credits: float,
    contract_credits: float,
    contract_start: date,
    contract_end: date,
    today: date,
) -> dict:
    """Contract burn pacing: consumed share vs elapsed-time share.

    pace_ratio > 1.0 means burning faster than the contract clock.
    """
    total = safe_float(contract_credits)
    term_days = (contract_end - contract_start).days
    if total <= 0 or term_days <= 0 or today < contract_start:
        return {"ok": False, "reason": "Contract not configured or not started."}
    elapsed_days = min((today - contract_start).days + 1, term_days)
    time_share = elapsed_days / term_days
    consumed_share = safe_float(consumed_credits) / total
    pace_ratio = consumed_share / time_share if time_share > 0 else 0.0
    run_rate_daily = safe_float(consumed_credits) / max(elapsed_days, 1)
    projected_total = run_rate_daily * term_days
    return {
        "ok": True,
        "consumed_share": round(consumed_share * 100, 1),
        "time_share": round(time_share * 100, 1),
        "pace_ratio": round(pace_ratio, 2),
        "projected_term_credits": round(projected_total, 1),
        "projected_overage_credits": round(max(0.0, projected_total - total), 1),
        "days_remaining": term_days - elapsed_days,
    }
