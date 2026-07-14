"""Canonical money and delta math.

Every credit->dollar conversion in the app goes through this module so the
formula, rounding, and edge-case behavior live in exactly one tested place.
"""

from __future__ import annotations

import html as _html
import math
from collections.abc import Sequence
from datetime import date, datetime

DEFAULT_CREDIT_PRICE_USD = 3.68
DEFAULT_AI_CREDIT_PRICE_USD = 2.20
DEFAULT_STORAGE_USD_PER_TB_MONTH = 23.00
# Storage TB base (F3, 2026-07-14): every storage divisor in the app is binary
# TiB (bytes / 1024**4, GB / 1024). Snowflake prices "per TB" and its own
# ACCOUNT_USAGE storage views (DATABASE_STORAGE_USAGE_HISTORY, STORAGE_USAGE)
# are explicitly an estimate that "won't match your invoice exactly"
# (docs.snowflake.com/en/sql-reference/account-usage/storage_usage). Billing
# truth is ORGANIZATION_USAGE.USAGE_IN_CURRENCY, surfaced on the Cost rate-card
# reconciliation panel. Keep this divisor consistent with the $/TB SETTING.

# The Snowflake account runs in Central time; SETTINGS and the marts store
# account time. components.py imports this so the app has ONE spelling of it.
ACCOUNT_TIMEZONE = "America/Chicago"


def account_today() -> date:
    """Today in the ACCOUNT's timezone, not the server's.

    Under SiS/containers the process clock is UTC while account time is
    America/Chicago: from 18:00 to midnight Chicago the two disagree about
    what 'today' is, which shifted MTD boundaries and month-end forecasts
    for a quarter of every day. Falls back to the local date only where
    tzdata is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(tz=ZoneInfo(ACCOUNT_TIMEZONE)).date()
    except (ImportError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return date.today()


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
    # Largest-remainder in cents: naive per-part rounding drifted (100 across
    # [1,1,1] -> 33.33*3 = 99.99) and chargeback tables leaked pennies vs the
    # exact warehouse total they must reconcile to by construction.
    total_cents = round(total_v * 100)
    raw = [total_v * 100 * w / weight_sum for w in clean]
    floors = [int(x) for x in raw]
    shortfall = int(total_cents - sum(floors))
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    step = 1 if shortfall >= 0 else -1
    for i in order[: abs(shortfall)]:
        floors[i] += step
    return [cents / 100.0 for cents in floors]


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


def exec_summary_html(*, company: str, days: int, generated: str, window_spend: str,
                      mtd_line: str, forecast_line: str, alerts_line: str,
                      score_line: str, drivers: list[tuple[str, str, str]],
                      actions: list[str]) -> str:
    """Styled, self-contained HTML executive summary (the .txt looked amateur).

    Pure string builder — inputs arrive pre-formatted so this stays testable
    and the page keeps owning data honesty. Every interpolated field is
    HTML-escaped HERE, in the one tested place, so an object name carrying
    '<', '&', or a stray tag can never break (or script) the exported file.
    """
    esc = _html.escape
    company = esc(str(company))
    generated = esc(str(generated))
    window_spend = esc(str(window_spend))
    mtd_line = esc(str(mtd_line))
    forecast_line = esc(str(forecast_line))
    alerts_line = esc(str(alerts_line))
    score_line = esc(str(score_line))
    driver_rows = "".join(
        f"<tr><td>{esc(str(d))}</td><td style='text-align:right'>-{esc(str(p))}</td><td>{esc(str(e))}</td></tr>"
        for d, p, e in drivers
    ) or "<tr><td colspan='3'>No deductions — clean window.</td></tr>"
    action_items = "".join(f"<li>{esc(str(a))}</li>" for a in actions) or "<li>No open actions.</li>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OVERWATCH executive summary</title>
<style>
 body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #0f172a; margin: 32px; }}
 .kicker {{ letter-spacing: .18em; font-size: 11px; color: #64748b; text-transform: uppercase; }}
 h1 {{ margin: 4px 0 2px 0; font-size: 22px; }}
 .meta {{ color: #64748b; font-size: 12px; margin-bottom: 18px; }}
 .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
 .card {{ border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px 16px; min-width: 170px; }}
 .card .label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: .06em; }}
 .card .value {{ font-size: 17px; font-weight: 600; margin-top: 3px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ border-bottom: 1px solid #e2e8f0; padding: 6px 8px; text-align: left; }}
 th {{ color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }}
 h2 {{ font-size: 14px; margin: 22px 0 8px 0; }}
 .foot {{ margin-top: 26px; color: #94a3b8; font-size: 11px; }}
</style></head><body>
<div class="kicker">OVERWATCH</div>
<h1>Executive summary — {company}</h1>
<div class="meta">Last {days} days · generated {generated}</div>
<div class="cards">
 <div class="card"><div class="label">Window spend</div><div class="value">{window_spend}</div></div>
 <div class="card"><div class="label">Month to date</div><div class="value">{mtd_line}</div></div>
 <div class="card"><div class="label">Projected month-end</div><div class="value">{forecast_line}</div></div>
 <div class="card"><div class="label">Open alerts</div><div class="value">{alerts_line}</div></div>
 <div class="card"><div class="label">Platform score</div><div class="value">{score_line}</div></div>
</div>
<h2>Score deductions</h2>
<table><tr><th>Driver</th><th>Points</th><th>Evidence</th></tr>{driver_rows}</table>
<h2>Top actions</h2>
<ul>{action_items}</ul>
<div class="foot">Numbers come from ACCOUNT_USAGE-derived facts with the cloud-services
adjustment applied; telemetry lags up to ~45 min (metering daily up to 24h).</div>
</body></html>"""

def mtd_pace_vs_prior_month(daily, today):
    """MTD spend paced against the SAME first-N-days of the prior month —
    the budget-free pace signal (owner 2026-07-13: the Monthly-budget KPI
    read 'Not configured' forever; a pace needs no configuration).

    ``daily``: frame with DAY (date-like) and USD columns covering both
    months. Returns (mtd_usd, prior_usd, pct_delta); pct_delta is None when
    the prior month has no rows in the span — never a fabricated 0%.
    """
    from datetime import timedelta

    import pandas as pd

    if daily is None or len(daily) == 0 or "USD" not in getattr(daily, "columns", ()):
        return 0.0, 0.0, None
    frame = daily.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce").dt.date
    frame = frame.dropna(subset=["DAY"])
    month_start = today.replace(day=1)
    prior_end = month_start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    n_days = min(today.day, prior_end.day)   # capped at the prior month's length
    prior_cut = prior_start + timedelta(days=n_days)
    mtd = float(frame[frame["DAY"] >= month_start]["USD"].map(safe_float).sum())
    prior_rows = frame[(frame["DAY"] >= prior_start) & (frame["DAY"] < prior_cut)]
    prior = float(prior_rows["USD"].map(safe_float).sum())
    if prior_rows.empty or prior <= 0:
        return mtd, prior, None
    return mtd, prior, (mtd - prior) / prior * 100.0

