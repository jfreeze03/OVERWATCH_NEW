"""Formula fact-check (2026-07-07): every number-producing function verified
against hand-computed expectations. Three discrepancies were found and fixed;
these tests pin the corrected behavior forever.

Findings fixed:
1. allocate_by_share leaked pennies (naive rounding: 100 over [1,1,1] summed
   to 99.99) -> largest-remainder, parts now sum exactly.
2. day_activity's baseline divided by a fixed 14 -> loader gaps deflated the
   baseline and over-flagged replay days -> divide by days PRESENT.
3. Cortex per-user 30d projection used an active-day basis (x15 overshoot
   for a 2-days-active user) while the rollup used the calendar window ->
   both now use the calendar basis.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.data import mart_sql
from app.logic.cortex import enrich_user_rollup
from app.logic.forecast import contract_pace, month_end_projection
from app.logic.formulas import (
    allocate_by_share,
    billed_credits,
    credits_to_usd,
    month_days,
    pct_delta,
)
from app.logic.scoring import platform_score, resolve_weights

# ---------------------------------------------------------------------------
# Money primitives (hand-computed)
# ---------------------------------------------------------------------------


def test_credits_to_usd_hand():
    assert credits_to_usd(123.456, 3.68) == 454.32  # 454.31808 -> cents


def test_billed_credits_hand():
    assert billed_credits(100, -7.5) == 92.5
    assert billed_credits(100, 7.5) == 92.5   # defensive sign flip
    assert billed_credits(3, -10) == 0.0      # floor


def test_pct_delta_hand():
    assert pct_delta(120, 100) == 20.0
    assert pct_delta(50, -100) == 150.0       # |prior| denominator
    assert pct_delta(5, 0) is None


def test_month_days_hand():
    assert month_days(date(2026, 7, 7)) == (31, 7, 24)
    assert month_days(date(2026, 2, 28)) == (28, 28, 0)
    assert month_days(date(2026, 12, 31)) == (31, 31, 0)


# ---------------------------------------------------------------------------
# Allocation: exact-sum invariant (finding 1)
# ---------------------------------------------------------------------------


def test_allocation_sums_exactly():
    parts = allocate_by_share(100.0, [1, 1, 1])
    assert round(sum(parts), 2) == 100.00      # was 99.99 pre-fix
    assert sorted(parts) == [33.33, 33.33, 33.34]


def test_allocation_exactness_property():
    cases = [(100.0, [1, 1, 1]), (999.97, [3, 7, 11, 13]), (0.05, [1, 1, 1, 1, 1, 1]),
             (12345.67, list(range(1, 40))), (10.0, [0, 5, 0, 5])]
    for total, weights in cases:
        parts = allocate_by_share(total, weights)
        assert round(sum(parts), 2) == round(total, 2), (total, weights, parts)
        assert all(p >= 0 for p in parts)


def test_allocation_proportionality_preserved():
    parts = allocate_by_share(300.0, [1, 2, 3])
    assert parts == [50.0, 100.0, 150.0]


# ---------------------------------------------------------------------------
# Contract pace + forecast (hand-computed)
# ---------------------------------------------------------------------------


def test_contract_pace_hand():
    # term (1/1 -> 4/11) = 100 days; day 50 inclusive; half consumed.
    pace = contract_pace(500, 1000, date(2026, 1, 1), date(2026, 4, 11), date(2026, 2, 19))
    assert pace["time_share"] == 50.0
    assert pace["consumed_share"] == 50.0
    assert pace["pace_ratio"] == 1.0
    assert pace["projected_term_credits"] == 1000.0
    assert pace["projected_overage_credits"] == 0.0


def test_forecast_flat_series_hand():
    daily = pd.DataFrame({"DAY": pd.date_range("2026-06-20", periods=18, freq="D"),
                          "USD": [100.0] * 18})
    cast = month_end_projection(daily, date(2026, 7, 7), engine="linear")
    assert cast.projected_usd == 3100.0        # 700 MTD + 100 x 24 remaining
    assert cast.low_usd == cast.high_usd == 3100.0  # zero variance -> no band


# ---------------------------------------------------------------------------
# Scoring weights (hand-computed) + caps engage
# ---------------------------------------------------------------------------


def test_score_hand():
    score = platform_score({"critical_alerts": 2, "high_alerts": 1}, resolve_weights(None))
    assert score.score == 86                    # 100 - 2*6 - 1*2


def test_score_caps_engage():
    flooded = platform_score({"critical_alerts": 100}, resolve_weights(None))
    assert flooded.score == 76                  # critical penalty capped at 24


# ---------------------------------------------------------------------------
# Baseline + projection bases (findings 2 and 3)
# ---------------------------------------------------------------------------


def test_day_activity_baseline_uses_present_days():
    sql = mart_sql.day_activity("2026-07-01")
    assert "COUNT(DISTINCT DATE(HOUR_TS))" in sql
    assert "/ 14" not in sql


def test_cortex_projection_calendar_basis():
    row = pd.DataFrame([{"USER_NAME": "A", "TOTAL_CREDITS": 10.0,
                         "AVG_DAILY_CREDITS": 5.0, "CREDITS_PER_REQUEST": 0.1,
                         "TOTAL_REQUESTS": 100}])
    out = enrich_user_rollup(row, 2.20, window_days=30)
    # 10 credits over the 30d WINDOW projects 10 -- not AVG_DAILY(5) x 30 = 150.
    assert out.iloc[0]["PROJECTED_30D_CREDITS"] == 10.0
    seven = enrich_user_rollup(row, 2.20, window_days=7)
    assert round(seven.iloc[0]["PROJECTED_30D_CREDITS"], 2) == round(10.0 / 7 * 30, 2)
