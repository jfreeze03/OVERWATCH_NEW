"""Contract steering: the gap to commit, and how far the named levers go.

Pure math over numbers the pages already have (contract pace, idle advisor,
recurring patterns, sizing candidates). Snowsight budgets are calendar-month
and contract-blind; this is the renewal-landing plan.
"""

from __future__ import annotations

from .formulas import safe_float


def steering_plan(
    *,
    projected_term_credits: float,
    contract_credits: float,
    days_remaining: int,
    rate_usd: float,
    levers_monthly_usd: dict[str, float],
) -> dict:
    """-> {ok, gap_usd, needed_per_day_usd, rows, covered_per_day_usd,
    coverage_pct, verdict}. gap<=0 means on track (rows still shown)."""
    total = safe_float(contract_credits)
    projected = safe_float(projected_term_credits)
    days = max(int(days_remaining or 0), 0)
    rate = safe_float(rate_usd, 3.68)
    if total <= 0:
        return {"ok": False, "verdict": "Contract not configured (CONTRACT_CREDITS on Admin)."}
    if days <= 0:
        return {"ok": False, "verdict": "Contract term has ended — see the renewal planner."}
    gap_usd = max(0.0, (projected - total)) * rate
    needed_per_day = gap_usd / days
    rows = []
    covered = 0.0
    for lever, monthly in sorted(levers_monthly_usd.items(), key=lambda kv: -safe_float(kv[1])):
        per_day = max(0.0, safe_float(monthly)) / 30.0
        covered += per_day
        rows.append({
            "LEVER": lever,
            "EST_MONTHLY_USD": round(safe_float(monthly), 0),
            "EST_PER_DAY_USD": round(per_day, 2),
        })
    coverage_pct = (covered / needed_per_day * 100.0) if needed_per_day > 0 else 100.0
    if gap_usd <= 0:
        verdict = "On track to land within commit at the current burn."
    elif coverage_pct >= 100:
        verdict = (f"Overage projected, but the levers below cover the required "
                   f"{needed_per_day:,.0f} $/day cut with room to spare.")
    else:
        verdict = (f"Overage projected: cutting {needed_per_day:,.0f} $/day lands on commit; "
                   f"the known levers reach {coverage_pct:,.0f}% of that.")
    return {
        "ok": True, "gap_usd": round(gap_usd, 0),
        "needed_per_day_usd": round(needed_per_day, 2),
        "covered_per_day_usd": round(covered, 2),
        "coverage_pct": round(min(coverage_pct, 999.0), 1),
        "rows": rows, "verdict": verdict,
    }
