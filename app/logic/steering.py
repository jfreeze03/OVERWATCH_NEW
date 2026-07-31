"""Contract steering: the gap to commit, and how far the named levers go.

Pure math over numbers the pages already have (contract pace, idle advisor,
recurring patterns). Snowsight budgets are calendar-month and contract-blind;
this is the renewal-landing plan.

C4 (audit 2026-07-31): the docstring used to promise a sizing-candidates lever
as well. No caller ever wired one, so the promise is deleted rather than left
as a claim the page does not keep — add it back here only when a caller
actually passes a sizing lever in ``levers_monthly_usd``.

Lever CONTRACT: ``levers_monthly_usd`` values must already be REALIZABLE
dollars — the caller applies its own per-lever haircut before handing them
over, because only the caller knows how much of an estimate its lever can
actually bank. This module deliberately does not haircut again; it would
double-count. See cost_parts/contract.py for the fractions in use.
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
        # C4: "room to spare" read as a promise. The rows are realizable-adjusted
        # ESTIMATES of unexecuted work, so the honest claim is that the levers are
        # big enough on paper — not that the landing is secured.
        verdict = (f"Overage projected. The levers below are estimated at "
                   f"{coverage_pct:,.0f}% of the required {needed_per_day:,.0f} $/day cut "
                   "— enough on paper, but only once they are executed and verified.")
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
