"""Cost-layer bug-hunt #2 locks (2026-08-30, v4.355.0). App-side only, no migration.

Second, deeper adversarial pass over the cost layer (7 finders; credit-arms, spend/storage and
contract/forecast swept clean). Three confirmed findings:
  - [MED] unit_costs 'Trend one procedure': the window Total / Avg $/call summed per-day USD that
    was ALREADY cents-rounded (round-then-sum), zeroing sub-cent daily spend and contradicting the
    sum-then-round leaderboard beside it.
  - [MED] ai_chargeback queue: the '(all users)' aggregate row (= SUM of the per-user projections)
    and its per-user constituents were both queued with independent ESTIMATED_USD, double-counting
    those dollars in mart_sql.action_acceptance DONE_USD / ESTIMATED_OPEN_USD (SUM with no de-overlap).
  - [LOW] unit_costs measured-$ / call / call-tree columns render $%.4f but dollarized with the
    cents-rounding default, so real sub-cent values quantized to $0.0000.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.cortex import aggregate_budget_row, rollup_summary

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- F1 + F3: day-grain / sub-cent columns dollarize with round_cents=False --------
def test_unit_costs_subcent_and_daygrain_columns_use_round_cents_false():
    body = _src("app/ui/pages/cost_parts/unit_costs.py")
    for var in ("qdf", "tdf", "cdf", "kdf"):
        assert (f'{var}["USD"] = {var}["CREDITS"].map(lambda c: '
                f'credits_to_usd(c, rate, round_cents=False))') in body, var
    # the single-conversion window totals (rendered $%.2f) correctly KEEP the cents default --
    # they sum-then-round one window total, so cents-rounding there is right.
    assert 'pdf["USD_TOTAL"] = pdf["TOTAL_CREDITS"].map(lambda c: credits_to_usd(c, rate))' in body
    # the trend Total is now built from the exact (unrounded) per-day series
    assert '_tot = float(tdf["USD"].sum())' in body


# ---- F2 precondition: the aggregate row IS the sum of the per-user projections ------
def test_aggregate_budget_row_is_the_sum_of_its_constituents():
    # 3 fully-observable users; guarded scope projection == sum of the per-user projected column,
    # which is exactly why queuing the aggregate beside the per-user rows double-counts.
    enriched = pd.DataFrame({
        "USER_NAME": ["A", "B", "C"],
        "TOTAL_CREDITS": [100.0, 50.0, 50.0],
        "OBSERVABLE_DAYS": [30, 30, 30],
        "SPEND_USD": [220.0, 110.0, 110.0],
        "TOTAL_REQUESTS": [1000, 500, 500],
        "PROJECTED_30D_USD": [220.0, 110.0, 110.0],
    })
    summary = rollup_summary(enriched, 30)
    assert summary["projected_30d_usd_guarded"] == 440.0
    agg = aggregate_budget_row(summary, ai_budget_usd=100.0)
    assert agg is not None
    assert agg["PROJECTED_30D_USD"] == 440.0
    assert agg["PROJECTED_30D_USD"] == float(enriched["PROJECTED_30D_USD"].sum())  # full overlap


# ---- F2 fix: the queued aggregate ESTIMATED_USD is the incremental exposure ---------
def test_ai_queue_stamps_aggregate_with_incremental_estimate():
    body = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert "_est = max(0.0, _proj - _other_proj) if _is_agg else _proj" in body
    assert "sql_number(_est)" in body
    # the raw full-value insert (which caused the overlap) is gone
    assert "sql_number(r['PROJECTED_30D_USD'])" not in body


def test_ai_queue_incremental_formula_zeroes_the_overlap():
    # replicate the inline invariant: with the aggregate + all its constituents queued, the queued
    # ESTIMATED_USD set sums to the scope total exactly once.
    scope_total = 440.0
    per_user = [220.0, 110.0, 110.0]
    other = sum(per_user)
    agg_est = max(0.0, scope_total - other)
    assert agg_est == 0.0
    assert agg_est + other == scope_total
    # many-tiny case: scope breaches but no single user is separately queued -> aggregate keeps all
    assert max(0.0, scope_total - 0.0) == scope_total
    # partial: only the whale is separately queued -> aggregate carries the rest, no double-count
    whale = 300.0
    assert max(0.0, scope_total - whale) + whale == scope_total
