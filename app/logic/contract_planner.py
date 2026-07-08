"""Contract renewal scenarios from observed burn. Pure module.

Straight-line projections deliberately labeled as such — no seasonality is
invented. If the 365-day facts backfill lands, a monthly index can replace
the flat daily rate.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def remaining_balance_summary(df: pd.DataFrame, burn_window_days: int = 14) -> dict:
    """Summarize ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY rows.

    Expects DAY + TOTAL_REMAINING (multiple contracts per day are summed).
    Burn/day averages only the day-over-day DROPS in the trailing window —
    a renewal top-up is a rise, and treating it as negative burn would poison
    the runway. Returns ok=False (with a reason) whenever the frame cannot
    support the math; the UI degrades instead of inventing a number.
    """
    if df is None or len(df) == 0 or "TOTAL_REMAINING" not in getattr(df, "columns", ()):
        return {"ok": False, "reason": "No balance rows visible."}
    daily = (df.assign(_v=pd.to_numeric(df["TOTAL_REMAINING"], errors="coerce"))
               .groupby("DAY")["_v"].sum().dropna().sort_index())
    if len(daily) == 0:
        return {"ok": False, "reason": "No balance rows visible."}
    remaining = float(daily.iloc[-1])
    as_of = str(pd.Timestamp(daily.index[-1]).date()) if daily.index[-1] is not None else "n/a"
    on_demand = 0.0
    if "ON_DEMAND_CONSUMPTION_BALANCE" in df.columns:
        last_day = daily.index[-1]
        od = pd.to_numeric(df.loc[df["DAY"] == last_day, "ON_DEMAND_CONSUMPTION_BALANCE"],
                           errors="coerce").fillna(0)
        on_demand = float(od.sum())
    deltas = daily.diff().dropna().tail(max(1, int(burn_window_days)))
    drops = -deltas[deltas < 0]
    burn = float(drops.mean()) if len(drops) else 0.0
    runway = (remaining / burn) if burn > 0 and remaining > 0 else None
    return {
        "ok": True,
        "as_of": as_of,
        "remaining_usd": remaining,
        "on_demand_usd": on_demand,
        "burn_per_day_usd": burn,
        "runway_days": round(runway, 0) if runway is not None else None,
        "burn_days_observed": len(drops),
    }


def plan_scenarios(daily_burn_usd: float, term_months: int, buffer_pct: float,
                   remaining_usd: float, growth_pcts: tuple = (-10, 0, 10, 25)) -> list[dict]:
    """One row per growth scenario: term consumption, exhaustion of the
    CURRENT contract, and a recommended next commit with buffer."""
    daily = max(0.0, float(daily_burn_usd))
    months = max(1, min(int(term_months), 60))
    buffer = max(0.0, min(float(buffer_pct), 100.0)) / 100
    remaining = max(0.0, float(remaining_usd))
    rows = []
    for g in growth_pcts:
        rate = daily * (1 + g / 100)
        term_usd = rate * 30.44 * months
        if rate > 0 and remaining > 0:
            days_left = remaining / rate
            exhaustion = date.today() + timedelta(days=int(days_left))
            exhaustion_s = exhaustion.isoformat()
        else:
            exhaustion_s = "n/a"
        rows.append({
            "GROWTH": f"{g:+d}%",
            "DAILY_BURN_USD": round(rate, 2),
            "TERM_CONSUMPTION_USD": round(term_usd, 0),
            "CURRENT_CONTRACT_EXHAUSTED": exhaustion_s,
            "RECOMMENDED_COMMIT_USD": round(term_usd * (1 + buffer), 0),
        })
    return rows
