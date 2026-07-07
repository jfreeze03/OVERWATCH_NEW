"""Contract renewal scenarios from observed burn. Pure module.

Straight-line projections deliberately labeled as such — no seasonality is
invented. If the 365-day facts backfill lands, a monthly index can replace
the flat daily rate.
"""

from __future__ import annotations

from datetime import date, timedelta


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
