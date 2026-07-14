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
import pytest

from app.logic.formulas import (
    DEFAULT_AI_CREDIT_PRICE_USD,
    DEFAULT_CREDIT_PRICE_USD,
    billed_credits,
    credits_to_usd,
)

# ---------------------------------------------------------------------------
# The tolerance policy — see the request in the session notes.
# ---------------------------------------------------------------------------


def reconciles(computed_usd: float, billed_usd: float) -> bool:
    """Does the app's computed spend reconcile with what Snowflake billed?

    This is the definition of "close enough" for CI, and it is a judgment call
    about YOUR billing reality, not a mechanical one. The band has to sit
    between two hard walls:

      FLOOR — it must be tight enough to still catch a dropped cloud-services
              adjustment. That rebate runs up to 10% of daily compute, so a
              tolerance of >=10% would make test_a_dropped_cs_adjustment_breaks
              _reconciliation() pass while the app overstates every dollar.

      CEILING — it must be loose enough to absorb (a) cent-rounding across the
              window, which is pennies, and (b) genuine rate-card drift: the app
              prices at a flat DEFAULT_CREDIT_PRICE_USD while the invoice applies
              real contract rates per RATING_TYPE. That gap is not a bug and must
              not redden CI.

    Shapes worth weighing:
      - relative only (abs(c-b) <= pct * b) — scales, but on a near-zero window a
        tiny absolute gap reads as a huge percentage and flaps.
      - absolute only (abs(c-b) <= usd)     — stable when small, meaningless at
        six figures.
      - hybrid: abs(c-b) <= max(floor_usd, pct * b) — absolute slack for small
        windows, proportional slack for large ones. Usually the right shape.

    TODO(jfreeze03): implement. Pick the shape and the numbers; the numbers are
    the part only you can supply, because they encode how far your effective
    contract rate really sits from DEFAULT_CREDIT_PRICE_USD.
    """
    raise NotImplementedError("define the reconciliation tolerance policy")


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


@pytest.mark.xfail(raises=NotImplementedError, reason="tolerance policy is yours to define")
def test_a_clean_month_reconciles():
    df = _usage_frame()
    assert reconciles(_computed_usd(df), float(df["BILLED_USD"].sum()))


@pytest.mark.xfail(raises=NotImplementedError, reason="tolerance policy is yours to define")
def test_a_dropped_cs_adjustment_breaks_reconciliation():
    """The regression that matters. If the tolerance swallows this, it is too loose.

    Here the app forgets the rebate and prices raw CREDITS_USED. On this frame the
    rebate is 8% of compute — inside a naive 10% band, which is exactly the trap.
    """
    df = _usage_frame()
    forgot_the_rebate = credits_to_usd(float(df["CREDITS_USED"].sum()), _RATE)
    assert not reconciles(forgot_the_rebate, float(df["BILLED_USD"].sum()))


@pytest.mark.xfail(raises=NotImplementedError, reason="tolerance policy is yours to define")
def test_cent_rounding_across_a_window_still_reconciles():
    """Rounding is not a break. Per-day rounding to cents drifts by up to half a
    cent a day; over a month that is small change and must not redden CI."""
    df = _usage_frame(days=30, credits_per_day=33.333, cs_adjustment=-2.777)
    per_day = sum(credits_to_usd(billed_credits(c, a), _RATE)
                  for c, a in zip(df["CREDITS_USED"], df["CS_ADJUSTMENT"], strict=True))
    assert reconciles(per_day, _computed_usd(df))


@pytest.mark.xfail(raises=NotImplementedError, reason="tolerance policy is yours to define")
def test_a_near_zero_window_does_not_flap():
    """A quiet day is where a percentage-only tolerance falls apart: a few cents
    of drift against a couple of dollars of spend is a huge percentage and nothing
    at all in reality."""
    df = _usage_frame(days=1, credits_per_day=0.5, cs_adjustment=-0.04)
    computed = _computed_usd(df) + 0.02          # two cents of honest drift
    assert reconciles(computed, float(df["BILLED_USD"].sum()))
