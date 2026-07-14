"""Phase 4 — org reconciliation: billing truth vs the app's computed dollars.

Two numbers describe the same spend and they are produced completely differently:

  BILLED   ORGANIZATION_USAGE.USAGE_IN_CURRENCY — what Snowflake actually charged.
  COMPUTED credits x rate, through formulas.billed_credits + credits_to_usd —
           what every dollar the app renders is made of.

They must agree, but they will never be byte-equal: the org rate card carries
contract discounts, per-RATING_TYPE pricing and adjustments, while the app model
applies a configured flat rate. cost_sql.org_rate_card says so outright — the
residual "is rate-card reality, not a bug in either number." So reconciliation is
a TOLERANCE question, and the tolerance is what these locks are really about.

The failure this guards against is the one formulas.billed_credits was written
for: Snowflake rebates cloud-services credits (up to 10% of daily compute) via a
negative adjustment. Drop it and every dollar in the app is overstated. The old
app hardcoded it to zero. A tolerance loose enough to swallow a dropped
adjustment would let that regression back in silently — which is precisely why
the band cannot just be set to "generous and move on."
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import (
    DEFAULT_AI_CREDIT_PRICE_USD,
    DEFAULT_CREDIT_PRICE_USD,
    billed_credits,
    credits_to_usd,
    safe_float,
)

# ---------------------------------------------------------------------------
# The tolerance policy
# ---------------------------------------------------------------------------

# The account's effective contract rate IS DEFAULT_CREDIT_PRICE_USD (3.68), so the
# app model and the invoice price the same credit the same way and there is no
# systematic rate-card gap to absorb. That is what lets this band be tight.
#
# Were the effective rate materially below 3.68, no single band would work: the
# constant overstatement from pricing at 3.68 would be the same order as the
# dropped-rebate error below, and the two would be indistinguishable. The fix then
# is not a looser band but reconciling at the IMPLIED rate
# (USAGE_IN_CURRENCY / credits), which cancels the rate difference and leaves only
# the rebate error visible. Revisit this if the contract is renegotiated.
RECONCILE_PCT = 0.01        # 1% of billed — see the walls below
RECONCILE_FLOOR_USD = 1.00  # absolute slack so a near-zero window cannot flap


def reconciles(computed_usd: float, billed_usd: float) -> bool:
    """Does the app's computed spend reconcile with what Snowflake billed?

    Hybrid band: absolute slack on small windows, proportional slack on large
    ones. A percentage-only rule divides by a near-zero billed total and turns
    two cents of honest rounding into a screaming variance; an absolute-only rule
    is meaningless once the window is six figures.

    The band sits between two hard walls:

      FLOOR — tight enough to still catch a dropped cloud-services rebate, which
              is the regression formulas.billed_credits exists to prevent (the
              old app hardcoded it to zero). That rebate runs up to 10% of daily
              compute, so anything at or above a 10% band would let the bug back
              in silently. At 1% we catch any systematic rebate loss above 1%.

      CEILING — loose enough to absorb cent-rounding across the window, which is
              fractions of a cent per day and cannot approach 1% of a real month.

    1% is therefore comfortably clear of both walls rather than split between
    them, which is the whole reason a matching effective rate is worth having.
    """
    gap = abs(safe_float(computed_usd) - safe_float(billed_usd))
    return gap <= max(RECONCILE_FLOOR_USD, RECONCILE_PCT * abs(safe_float(billed_usd)))


# ---------------------------------------------------------------------------
# 1. The cloud-services adjustment is not optional
# ---------------------------------------------------------------------------

def test_billed_credits_applies_the_rebate():
    assert billed_credits(1000.0, -50.0) == 950.0


def test_ignoring_the_adjustment_overstates_by_exactly_the_rebate():
    used, adj = 1000.0, -50.0
    naive = credits_to_usd(used)                       # what the old app did
    honest = credits_to_usd(billed_credits(used, adj))
    assert naive > honest
    assert round(naive - honest, 2) == credits_to_usd(50.0)


def test_a_positive_adjustment_is_still_a_rebate():
    # The source column is <= 0; a sign flip upstream must not INFLATE the bill.
    assert billed_credits(1000.0, 50.0) == 950.0


def test_billed_credits_never_goes_negative():
    assert billed_credits(10.0, -999.0) == 0.0


# ---------------------------------------------------------------------------
# 2. Rate discipline — AI credits are not standard credits
# ---------------------------------------------------------------------------

def test_ai_credits_price_at_the_ai_rate():
    assert DEFAULT_AI_CREDIT_PRICE_USD != DEFAULT_CREDIT_PRICE_USD
    assert credits_to_usd(100.0, DEFAULT_AI_CREDIT_PRICE_USD) == 220.0


def test_pricing_ai_at_the_standard_rate_overstates():
    ai = 100.0
    correct = credits_to_usd(ai, DEFAULT_AI_CREDIT_PRICE_USD)
    wrong = credits_to_usd(ai)                          # standard rate by default
    assert wrong > correct


# ---------------------------------------------------------------------------
# 3. Reconciliation against a synthetic month of billing truth
# ---------------------------------------------------------------------------

_RATE = DEFAULT_CREDIT_PRICE_USD


def _usage_frame(days: int = 30, credits_per_day: float = 100.0,
                 cs_adjustment: float = -8.0) -> pd.DataFrame:
    """A month of metering rows plus the matching billed dollars.

    BILLED_USD is what the invoice would show: the REBATED credits at the rate.
    Reconciliation must recover it from CREDITS_USED + CS_ADJUSTMENT.
    """
    day = pd.date_range("2026-06-01", periods=days, freq="D")
    billed = [round((credits_per_day + cs_adjustment) * _RATE, 2)] * days
    return pd.DataFrame({
        "DAY": day,
        "CREDITS_USED": [credits_per_day] * days,
        "CS_ADJUSTMENT": [cs_adjustment] * days,
        "BILLED_USD": billed,
    })


def _computed_usd(df: pd.DataFrame) -> float:
    """The app's path: rebate the credits, then price them. Rounds ONCE, at the
    end — rounding each day to cents first would drift by up to half a cent a day."""
    total = sum(billed_credits(c, a)
                for c, a in zip(df["CREDITS_USED"], df["CS_ADJUSTMENT"], strict=True))
    return credits_to_usd(total, _RATE)


def test_the_synthetic_frame_is_self_consistent():
    # Guards the fixture: if this drifts, every reconciliation test below is noise.
    df = _usage_frame()
    assert len(df) == 30
    assert df["BILLED_USD"].iloc[0] == round(92.0 * _RATE, 2)


def test_a_clean_month_reconciles():
    df = _usage_frame()
    assert reconciles(_computed_usd(df), float(df["BILLED_USD"].sum()))


def test_a_dropped_cs_adjustment_breaks_reconciliation():
    """The regression that matters. If the tolerance swallows this, it is too loose.

    Here the app forgets the rebate and prices raw CREDITS_USED. On this frame the
    rebate is 8% of compute — inside a naive 10% band, which is exactly the trap.
    """
    df = _usage_frame()
    forgot_the_rebate = credits_to_usd(float(df["CREDITS_USED"].sum()), _RATE)
    assert not reconciles(forgot_the_rebate, float(df["BILLED_USD"].sum()))


def test_cent_rounding_across_a_window_still_reconciles():
    """Rounding is not a break. Per-day rounding to cents drifts by up to half a
    cent a day; over a month that is small change and must not redden CI."""
    df = _usage_frame(days=30, credits_per_day=33.333, cs_adjustment=-2.777)
    per_day = sum(credits_to_usd(billed_credits(c, a), _RATE)
                  for c, a in zip(df["CREDITS_USED"], df["CS_ADJUSTMENT"], strict=True))
    assert reconciles(per_day, _computed_usd(df))


def test_a_near_zero_window_does_not_flap():
    """A quiet day is where a percentage-only tolerance falls apart: a few cents
    of drift against a couple of dollars of spend is a huge percentage and nothing
    at all in reality."""
    df = _usage_frame(days=1, credits_per_day=0.5, cs_adjustment=-0.04)
    computed = _computed_usd(df) + 0.02          # two cents of honest drift
    assert reconciles(computed, float(df["BILLED_USD"].sum()))
