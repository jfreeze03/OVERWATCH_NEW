"""Canonical money and delta math.

Every credit->dollar conversion in the app goes through this module so the
formula, rounding, and edge-case behavior live in exactly one tested place.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

DEFAULT_CREDIT_PRICE_USD = 3.68
DEFAULT_AI_CREDIT_PRICE_USD = 2.20
DEFAULT_STORAGE_USD_PER_TB_MONTH = 23.00


def safe_float(value: object, default: float = 0.0) -> float:
    """Coerce to float; NaN/None/garbage become ``default``."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that treats a zero/invalid denominator as ``default``."""
    den = safe_float(denominator)
    if den == 0.0:
        return default
    return safe_float(numerator) / den


def credits_to_usd(credits: float, rate_usd: float = DEFAULT_CREDIT_PRICE_USD) -> float:
    """Convert credits to dollars at the configured rate, rounded to cents."""
    return round(safe_float(credits) * safe_float(rate_usd, DEFAULT_CREDIT_PRICE_USD), 2)


def billed_credits(credits_used: float, cloud_services_adjustment: float = 0.0) -> float:
    """Billed credits = used + adjustment (adjustment is negative or zero).

    Snowflake's daily cloud-services adjustment rebates cloud-services credits
    up to 10% of daily compute. Ignoring it overstates spend — the old app
    hardcoded it to zero; this function exists so no caller can.
    """
    used = safe_float(credits_used)
    adj = safe_float(cloud_services_adjustment)
    if adj > 0:  # defensive: source column is <= 0
        adj = -adj
    return max(0.0, used + adj)


def pct_delta(current: float, prior: float) -> float | None:
    """Percent change vs prior. ``None`` when there is no meaningful prior."""
    prior_v = safe_float(prior)
    if prior_v == 0.0:
        return None
    return round((safe_float(current) - prior_v) / abs(prior_v) * 100.0, 1)


def allocate_by_share(total: float, weights: Sequence[float]) -> list[float]:
    """Allocate ``total`` proportionally to non-negative weights.

    Used for labeled *allocated* attribution (user/database cost) where
    Snowflake bills only at warehouse grain. Zero-weight sets allocate zeros.
    """
    total_v = safe_float(total)
    clean = [max(0.0, safe_float(w)) for w in weights]
    weight_sum = sum(clean)
    if weight_sum <= 0.0:
        return [0.0 for _ in clean]
    return [round(total_v * w / weight_sum, 2) for w in clean]


def month_days(day: date) -> tuple[int, int, int]:
    """Return (days_in_month, days_elapsed_inclusive, days_remaining)."""
    if day.month == 12:
        next_month = date(day.year + 1, 1, 1)
    else:
        next_month = date(day.year, day.month + 1, 1)
    days_in_month = (next_month - date(day.year, day.month, 1)).days
    elapsed = day.day
    return days_in_month, elapsed, days_in_month - elapsed


def format_usd(value: float) -> str:
    """Compact dollar formatting for KPI surfaces."""
    amount = safe_float(value)
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:,.2f}M"
    if abs(amount) >= 10_000:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def format_credits(credits: float) -> str:
    """Credit formatting consistent with old-app conventions."""
    value = safe_float(credits)
    if abs(value) >= 100:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:,.4f}"
