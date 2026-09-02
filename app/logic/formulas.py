"""Canonical money and delta math.

Every credit->dollar conversion in the app goes through this module so the
formula, rounding, and edge-case behavior live in exactly one tested place.
"""

from __future__ import annotations

import csv
import html as _html
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO

import pandas as pd

DEFAULT_CREDIT_PRICE_USD = 3.68
DEFAULT_AI_CREDIT_PRICE_USD = 2.20
DEFAULT_STORAGE_USD_PER_TB_MONTH = 23.00
# Storage TB base (F3, 2026-07-14): every storage divisor in the app is binary
# TiB (bytes / 1024**4, GB / 1024). Snowflake prices "per TB" and its own
# ACCOUNT_USAGE storage views (DATABASE_STORAGE_USAGE_HISTORY, STORAGE_USAGE)
# are explicitly an estimate that "won't match your invoice exactly"
# (docs.snowflake.com/en/sql-reference/account-usage/storage_usage). Billing
# truth is ORGANIZATION_USAGE.USAGE_IN_CURRENCY, surfaced on Cost & Contract →
# Contract & Forecast's rate-card reconciliation panel (on Admin before v4.48).
# Keep this divisor consistent with the $/TB SETTING.

# The Snowflake account runs in Central time; SETTINGS and the marts store
# account time. components.py imports this so the app has ONE spelling of it.
ACCOUNT_TIMEZONE = "America/Chicago"


def account_now() -> datetime:
    """Now in the ACCOUNT's timezone, not the server's. Naive, on purpose.

    The marts store naive account time, so anything compared against a mart
    timestamp — overdue checks, row ages — has to be read here or it compares
    account time against a UTC clock and runs 5-6 hours fast. Naive because the
    values it is measured against carry no tzinfo. Falls back to the local clock
    only where tzdata is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(tz=ZoneInfo(ACCOUNT_TIMEZONE)).replace(tzinfo=None)
    except (ImportError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return datetime.now()


def account_today() -> date:
    """Today in the ACCOUNT's timezone, not the server's.

    Under SiS/containers the process clock is UTC while account time is
    America/Chicago: from 18:00 to midnight Chicago the two disagree about
    what 'today' is, which shifted MTD boundaries and month-end forecasts
    for a quarter of every day.
    """
    return account_now().date()


def daily_spend_last_n(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """PERF #46: last-N-days suffix of a wide daily-grain FACT_METERING_DAILY frame.

    FACT_METERING_DAILY is daily grain (~1 row/day), so a smaller window is a strict
    suffix of a larger one — this slice is row-for-row equal to fetching N days. Uses
    account_today() (America/Chicago), equal-or-more-correct than the old SQL
    CURRENT_DATE()-N session-tz window. Safe on empty/failed/missing-DAY frames (returns
    the frame unchanged) so callers keep their .usable()/.ok/guard() checks.
    """
    if df is None or df.empty or "DAY" not in df.columns:
        return df
    out = df.copy()
    out["DAY"] = pd.to_datetime(out["DAY"], errors="coerce").dt.date
    return out[out["DAY"] >= (account_today() - timedelta(days=n))]


def safe_float(value: object, default: float = 0.0) -> float:
    """Coerce to float; NaN/None/garbage become ``default``."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _coerce_date(value: object) -> date | None:
    """Best-effort date from a mart cell (date, datetime/Timestamp, or ISO string)."""
    # pd.NaT is a datetime SUBCLASS (isinstance(pd.NaT, datetime) is True), so a NULL
    # date cell would slip past the isinstance branch below and return pd.NaT.date()==NaT
    # instead of None — surfacing the literal 'NaT' on the contract-runway bar. Guard it
    # first, mirroring rca._to_dt / anomaly_explain._to_date.
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):   # pandas Timestamp is a datetime subclass
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def contract_runway(row: pd.Series | None, *, lead_days: int = 30) -> dict | None:
    """Contract-runway summary for the persistent countdown bar (CoCo Overview #20 /
    Cost #4).

    From a contract_exhaustion row (TOTAL, CONSUMED, DAILY_BURN, DAYS_LEFT,
    EXHAUST_DATE): the percent of the commitment consumed, days left, the exhaust
    date, and a 'decide-by' date (exhaust minus a procurement lead time, so renewal
    talks can start before the runway ends). Colour by runway: <=30 days left = bad,
    <=90 = warn, else ok. Returns None when no contract is configured (TOTAL <= 0)
    so callers render nothing. Pure — the dates come from the row, not a clock.
    """
    if row is None:
        return None
    total = safe_float(row.get("TOTAL"))
    if total <= 0:
        return None
    consumed = max(0.0, safe_float(row.get("CONSUMED")))
    days_left = safe_float(row.get("DAYS_LEFT"), -1.0)
    pct = round(min(consumed / total * 100.0, 100.0), 1)
    exhaust = _coerce_date(row.get("EXHAUST_DATE"))
    decide_by = exhaust - timedelta(days=max(0, int(lead_days))) if exhaust else None
    if days_left < 0:
        # exhausted/overrun (pct at the cap), or burn couldn't be computed — surface it
        severity = "bad" if pct >= 100 else "warn"
    elif days_left <= 30:
        severity = "bad"
    elif days_left <= 90:
        severity = "warn"
    else:
        severity = "ok"
    return {
        "pct_consumed": pct,
        "days_left": days_left,
        "exhaust_date": exhaust.isoformat() if exhaust else None,
        "decide_by": decide_by.isoformat() if decide_by else None,
        "severity": severity,
        "lead_days": int(lead_days),
    }


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that treats a zero/invalid denominator as ``default``."""
    den = safe_float(denominator)
    if den == 0.0:
        return default
    return safe_float(numerator) / den


def credits_to_usd(credits: float, rate_usd: float = DEFAULT_CREDIT_PRICE_USD,
                   round_cents: bool = True) -> float:
    """Convert credits to dollars at the configured rate. Rounds to cents by
    default (display). Pass ``round_cents=False`` for sub-cent unit costs
    ($/call, $/run) and — critically — BEFORE summing a day-grain series:
    rounding each day to cents first zeroes small per-day spend (a pipeline at
    ~$0.003/day reads $0.00 across a 180-day window instead of ~$0.54)."""
    usd = safe_float(credits) * safe_float(rate_usd, DEFAULT_CREDIT_PRICE_USD)
    return round(usd, 2) if round_cents else usd


def blended_billed_usd(credits_other: float, credits_ai: float,
                       rate_usd: float = DEFAULT_CREDIT_PRICE_USD,
                       ai_rate_usd: float = DEFAULT_AI_CREDIT_PRICE_USD) -> float:
    """Dollarize a billed-credit total that mixes compute and AI/Cortex credits.

    AI/Cortex credits bill at AI_CREDIT_PRICE_USD ($2.20 default), not the
    compute rate ($3.68); pricing an all-service credit total at the single
    compute rate overstates the bill by (rate - ai_rate) x AI credits (cost
    review C1). Every all-service rollup must price the two partitions
    separately. The shared readers emit CREDITS_BILLED_OTHER + CREDITS_BILLED_AI
    (they sum to CREDITS_BILLED by construction); pass them here.
    """
    other = safe_float(credits_other) * safe_float(rate_usd, DEFAULT_CREDIT_PRICE_USD)
    ai = safe_float(credits_ai) * safe_float(ai_rate_usd, DEFAULT_AI_CREDIT_PRICE_USD)
    return round(other + ai, 2)


def blended_credit_rate(credits_other: float, credits_ai: float,
                        rate_usd: float = DEFAULT_CREDIT_PRICE_USD,
                        ai_rate_usd: float = DEFAULT_AI_CREDIT_PRICE_USD) -> float:
    """Effective $/credit for an observed compute+AI credit mix.

    Lets a forward projection that only carries a total-credit number (renewal
    planner, remaining-commitment valuation) stay consistent with the AI-aware
    dollar burn instead of falling back to the flat compute rate. Empty mix
    falls back to the compute rate.
    """
    total = safe_float(credits_other) + safe_float(credits_ai)
    if total <= 0.0:
        return safe_float(rate_usd, DEFAULT_CREDIT_PRICE_USD)
    return blended_billed_usd(credits_other, credits_ai, rate_usd, ai_rate_usd) / total


def egress_effective_rate_per_tb(org_transfer_usd: float, billable_tb: float,
                                 fallback_rate_per_tb: float,
                                 sanity_multiple: float = 10.0) -> tuple[float, bool]:
    """The $/TB to price egress with (rec#11).

    Prefer the org rate-card IMPLIED rate — billed TRANSFER_USD divided by the
    observed billable TB — so the estimate reconciles to billing truth. Fall back
    to the configured DATA_TRANSFER_USD_PER_TB setting when org currency is not
    visible, no billable TB was observed, OR the implied rate is implausibly high
    (> ``sanity_multiple`` x the configured rate): the app's cross-boundary
    BILLABLE flag can under-count what Snowflake actually bills, and a real org
    bill over a tiny billable-TB estimate would otherwise present an absurd
    five-figure $/TB as billing truth. Never an inlined constant (house rule d).
    Returns (rate_per_tb, used_org_truth).
    """
    org = safe_float(org_transfer_usd)
    tb = safe_float(billable_tb)
    fallback = safe_float(fallback_rate_per_tb)
    if org > 0.0 and tb > 0.0:
        implied = org / tb
        if fallback > 0.0 and implied > sanity_multiple * fallback:
            return fallback, False   # BILLABLE under-counts — don't trust the implied rate
        return implied, True
    return fallback, False


def pct_delta(current: float, prior: float) -> float | None:
    """Percent change vs prior. ``None`` when there is no meaningful prior."""
    prior_v = safe_float(prior)
    if prior_v == 0.0:
        return None
    r = round((safe_float(current) - prior_v) / abs(prior_v) * 100.0, 1)
    return r + 0.0 if r == 0 else r   # collapse -0.0 -> 0.0 so a flat metric isn't colored/signed as a decrease


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


def md_dollars(text: object) -> str:
    """Escape '$' for Streamlit MARKDOWN sinks (st.caption/markdown/info/...).

    Streamlit's markdown treats a bare '$'...'$' pair as a LaTeX math span, so any
    caption holding two dollar amounts — or one amount plus a literal like USER$ —
    renders everything between them in serif-italic math (live render bug,
    screenshots 2026-07-11 and 2026-07-31). Wrap the WHOLE rendered string at the
    sink. Do NOT bake this into format_usd: its output also feeds non-markdown
    surfaces (metric cards, chart data, CSV/ZIP exports) where a literal backslash
    would display.
    """
    return str(text).replace("$", "\\$")


def format_credits(credits: float) -> str:
    """Credit formatting consistent with old-app conventions."""
    value = safe_float(credits)
    if abs(value) >= 100:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


_DURATION_FACTOR_SEC = {"ms": 0.001, "s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0}


def humanize_duration(value: object, unit: str = "s") -> str:
    """Render a raw duration as compact H/M/S for reading — "1h 30m", "5m 12s",
    "45s", "850ms". DISPLAY ONLY: callers format the display cell while the
    underlying numeric column is untouched, so tables still sort by the real
    value and the CSV keeps the raw number. ``unit`` is the column's native unit
    ('ms', 's'/'sec', 'min', 'h'). NaN/garbage renders the em-dash no-value glyph."""
    v = safe_float(value, default=float("nan"))
    if v != v:                       # NaN / unparseable
        return "—"
    sign = "-" if v < 0 else ""
    secs = abs(v) * _DURATION_FACTOR_SEC.get(unit.lower(), 1.0)
    if secs == 0:
        return "0s"
    if secs < 1:
        ms = secs * 1000
        if round(ms) == 0:            # sub-0.5ms rounds to nothing -> one zero glyph
            return "0s"
        return f"{sign}{ms:.0f}ms"
    if secs < 10:                    # keep sub-10s legible with one decimal
        return f"{sign}{secs:.1f}s"
    total = round(secs)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{sign}{hours}h {minutes}m" if minutes else f"{sign}{hours}h"
    if minutes:
        return f"{sign}{minutes}m {seconds}s" if seconds else f"{sign}{minutes}m"
    return f"{sign}{seconds}s"


def humanize_bytes(value: object) -> str:
    """Render a raw byte count as a compact binary unit — "3.6 TB", "891.3 MB",
    "512 B" — so sub-TB transfer (hundreds of MB) reads as itself instead of
    rounding to "0.000 TB". Binary (1024) TiB/GiB/MiB, the unit Snowflake bills
    on; Snowsight's Cost Management shows the same scale. NaN/garbage -> em-dash."""
    v = safe_float(value, default=float("nan"))
    if v != v:
        return "—"
    sign = "-" if v < 0 else ""
    n = abs(float(v))
    _units = (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024))
    for i, (unit, factor) in enumerate(_units):
        if n >= factor:
            # promote at the boundary: a value that rounds to 1024 of this unit (e.g.
            # 1023.97 MB) reads as "1.0 GB", not "1,024.0 MB", when a larger unit exists.
            if round(n / factor, 1) >= 1024 and i > 0:
                unit, factor = _units[i - 1]
            return f"{sign}{n / factor:,.1f} {unit}"   # 1 decimal, Snowsight style
    return f"{sign}{n:,.0f} B"


def humanize_gb(value: object) -> str:
    """Humanize a value already in binary GiB (the unit most OVERWATCH byte
    columns carry) to auto MB/GB/TB via humanize_bytes — so a 0.03 GB spill
    reads "30.7 MB", not the "0.0 GB" that fixed-decimal formatting produced."""
    v = safe_float(value, default=float("nan"))
    if v != v:
        return "—"
    return humanize_bytes(v * 1024 ** 3)


def humanize_age(value: object, now: object = None) -> str:
    """rec27: compact 'how stale' for a past timestamp — "just now" / "5m ago" /
    "3h ago" / "2d ago". A DISPLAY-ONLY companion to a real timestamp column (which
    stays for sort + tz-conversion); the raw timestamp is never mutated. ``now`` is
    the reference time the caller supplies (account-naive, e.g. account_now()); an
    unparseable value or a missing ``now`` renders the em-dash no-value glyph."""
    import pandas as pd

    ts = pd.to_datetime(value, errors="coerce")
    ref = pd.to_datetime(now, errors="coerce")
    if pd.isna(ts) or pd.isna(ref):
        return "—"
    secs = (ref - ts).total_seconds()
    if secs < 45:                     # includes small clock skew / future stamps
        return "just now"
    if secs < 3600:
        return f"{round(secs / 60)}m ago"
    if secs < 86_400:
        return f"{round(secs / 3600)}h ago"
    return f"{round(secs / 86_400)}d ago"


def humanize_minutes_ago(minutes: object) -> str:
    """'just now' / 'Nm ago' / 'Nh ago' / 'Nd ago' from a minutes-since count.

    Day-aware (unlike humanize_duration, which caps at hours), and safe to feed a
    minutes value computed in SQL (DATEDIFF) so the relative age stays in one
    timezone basis instead of mixing a SQL stamp with a Python clock."""
    m = safe_float(minutes)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{round(m)}m ago"
    if m < 1440:
        return f"{round(m / 60)}h ago"
    return f"{round(m / 1440)}d ago"


def _spark_polyline(values, width: int = 240, height: int = 40, color: str = "#0891b2") -> str:
    """rec20: a self-contained inline-SVG sparkline for the HTML export — pure, no
    Streamlit dependency, so `exec_summary_html` stays in the logic layer and the
    downloaded file needs no external assets."""
    pts = [safe_float(v) for v in (values or []) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    n = len(pts)
    coords = " ".join(
        f"{i / (n - 1) * (width - 4) + 2:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}"
        for i, v in enumerate(pts))
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'style="display:block;margin-top:8px" aria-label="spend trend">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{coords}"/></svg>')


@dataclass(frozen=True)
class ExecutiveSummaryView:
    """Source-neutral executive readout shared by HTML, text, and CSV exports."""

    company: str
    days: int
    generated: str
    cards: tuple[tuple[str, str], ...]
    drivers: tuple[tuple[str, str, str], ...] = ()
    actions: tuple[str, ...] = ()
    spend_series: tuple[float, ...] = ()
    scope_notes: tuple[str, ...] = ()
    title: str = "Executive summary"


def executive_summary_html(view: ExecutiveSummaryView, *, presentation: bool = False) -> str:
    """Render a self-contained, projector- and print-friendly executive summary."""
    esc = _html.escape
    spark = _spark_polyline(view.spend_series, width=360 if presentation else 240,
                            height=64 if presentation else 40)
    cards = "".join(
        "<div class='card'><div class='label'>"
        f"{esc(str(label))}</div><div class='value'>{esc(str(value))}</div></div>"
        for label, value in view.cards
    ) or "<div class='card'><div class='value'>No headline metrics available.</div></div>"
    driver_rows = "".join(
        "<tr><td>"
        f"{esc(str(driver))}</td><td class='num'>{esc(str(value))}</td>"
        f"<td>{esc(str(evidence))}</td></tr>"
        for driver, value, evidence in view.drivers
    ) or "<tr><td colspan='3'>No deductions - clean window.</td></tr>"
    action_items = "".join(f"<li>{esc(str(action))}</li>" for action in view.actions)
    if not action_items:
        action_items = "<li>No open actions.</li>"
    trend = (
        f"<section class='trend'><div class='label'>Spend trend (last {int(view.days)}d)"
        f"</div>{spark}</section>"
        if spark else ""
    )
    notes = "<br>".join(esc(str(note)) for note in view.scope_notes)
    body_class = "presentation" if presentation else "standard"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OVERWATCH {esc(view.title)}</title>
<style>
 :root {{ color-scheme: light; --ink:#0f172a; --muted:#475569; --line:#cbd5e1; --accent:#0369a1; }}
 * {{ box-sizing:border-box; }}
 body {{ font-family:'Segoe UI',Arial,sans-serif; color:var(--ink); background:#fff; margin:0; }}
 main {{ max-width:1280px; margin:0 auto; padding:32px; }}
 .kicker {{ font-size:12px; color:var(--muted); text-transform:uppercase; font-weight:700; }}
 h1 {{ margin:5px 0 2px; font-size:28px; }}
 .meta {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
 .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
 .card {{ border:1px solid var(--line); border-top:4px solid var(--accent); border-radius:6px; padding:14px 16px; }}
 .label {{ font-size:12px; color:var(--muted); text-transform:uppercase; font-weight:700; }}
 .value {{ font-size:19px; font-weight:700; margin-top:4px; overflow-wrap:anywhere; }}
 h2 {{ font-size:17px; margin:24px 0 8px; }}
 table {{ border-collapse:collapse; width:100%; font-size:14px; }}
 th,td {{ border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }}
 th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
 .num {{ text-align:right; white-space:nowrap; }}
 li {{ margin:6px 0; }}
 .trend {{ margin:18px 0 4px; }}
 .scope {{ margin-top:26px; color:var(--muted); font-size:12px; line-height:1.5; }}
 body.presentation main {{ max-width:1600px; padding:4vh 4vw; }}
 body.presentation h1 {{ font-size:clamp(32px,4vh,54px); }}
 body.presentation .cards {{ grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:18px; }}
 body.presentation .card {{ padding:20px; }}
 body.presentation .value {{ font-size:clamp(24px,3vh,40px); }}
 body.presentation table, body.presentation li {{ font-size:clamp(16px,1.7vh,24px); }}
 @media (max-width:700px) {{ main {{ padding:18px; }} .cards {{ grid-template-columns:1fr; }} }}
 @media print {{
   @page {{ margin:14mm; }}
   main {{ max-width:none; padding:0; }}
   .card,tr,ul,table,svg,.trend {{ break-inside:avoid; }}
 }}
</style></head><body class="{body_class}"><main>
<div class="kicker">OVERWATCH</div>
<h1>{esc(view.title)} - {esc(str(view.company))}</h1>
<div class="meta">Last {int(view.days)} days | generated {esc(str(view.generated))}</div>
<div class="cards">{cards}</div>
{trend}
<h2>Score deductions</h2>
<table><tr><th>Driver</th><th class="num">Impact</th><th>Evidence</th></tr>{driver_rows}</table>
<h2>Top actions</h2><ul>{action_items}</ul>
<div class="scope">{notes}</div>
</main></body></html>"""


def executive_slide_bullets(view: ExecutiveSummaryView) -> str:
    """Produce paste-ready slide bullets from the same readout."""
    bullets = [f"{view.title}: {view.company} ({view.days}d)"]
    bullets.extend(f"- {label}: {value}" for label, value in view.cards)
    if view.drivers:
        driver, value, evidence = view.drivers[0]
        bullets.append(f"- Largest deduction: {driver} {value} - {evidence}")
    if view.actions:
        bullets.append(f"- Immediate action: {view.actions[0]}")
    return "\n".join(bullets)


def executive_summary_csv(view: ExecutiveSummaryView) -> str:
    """Render a tabular companion that keeps every headline and decision row."""
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["SECTION", "METRIC", "VALUE", "DETAIL"])
    for label, value in view.cards:
        writer.writerow(["Headline", label, value, ""])
    for driver, value, evidence in view.drivers:
        writer.writerow(["Score deduction", driver, value, evidence])
    for action in view.actions:
        writer.writerow(["Action", action, "", ""])
    for note in view.scope_notes:
        writer.writerow(["Scope", "Method note", "", note])
    return output.getvalue()


def exec_summary_html(*, company: str, days: int, generated: str, window_spend: str,
                      mtd_line: str, forecast_line: str, alerts_line: str,
                      score_line: str, drivers: list[tuple[str, str, str]],
                      actions: list[str], spend_series: list | None = None) -> str:
    """Styled, self-contained HTML executive summary (the .txt looked amateur).

    Pure string builder — inputs arrive pre-formatted so this stays testable
    and the page keeps owning data honesty. Every interpolated field is
    HTML-escaped HERE, in the one tested place, so an object name carrying
    '<', '&', or a stray tag can never break (or script) the exported file.
    ``spend_series`` (optional daily USD) adds a trend sparkline and the export
    carries a print stylesheet so it prints as a clean one-pager (rec20).
    """
    return executive_summary_html(ExecutiveSummaryView(
        company=company,
        days=days,
        generated=generated,
        cards=(
            ("Window spend", window_spend),
            ("Month to date", mtd_line),
            ("Projected month-end", forecast_line),
            ("Open alerts", alerts_line),
            ("Platform score", score_line),
        ),
        drivers=tuple((driver, f"-{points}", evidence) for driver, points, evidence in drivers),
        actions=tuple(actions),
        spend_series=tuple(safe_float(value) for value in (spend_series or [])),
        scope_notes=(
            "MTD and projected are billed credits with the cloud-services adjustment applied "
            "and are account-wide; window spend is warehouse metering and company-scoped.",
            "All figures use account time; warehouse telemetry lags about 45 minutes and daily "
            "metering can lag up to 24 hours. An Incomplete score means inputs did not load.",
        ),
    ))

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
    # R3-7: compare only COMPLETED days on both sides — today is still growing, so
    # counting it on the current side (while the prior side has full days) biases the
    # pace low all day. eff_day = today.day - 1 drops today; still capped at the prior
    # month's length. The displayed full-month `mtd` below keeps today (unchanged).
    n_days = min(max(today.day - 1, 0), prior_end.day)   # equal-length window of completed days
    prior_cut = prior_start + timedelta(days=n_days)
    mtd_cut = month_start + timedelta(days=n_days)
    # Displayed MTD stays the TRUE full month-to-date (never understated at
    # month-end). The pace DELTA compares EQUAL day counts — the "(same days)"
    # contract. The old delta compared the full MTD against the day-capped prior,
    # so at month-end after a shorter prior month (e.g. Mar 30 = 30 days vs
    # Feb = 28) it over-counted the current side and read falsely high.
    mtd = float(frame[frame["DAY"] >= month_start]["USD"].map(safe_float).sum())
    mtd_same = float(frame[(frame["DAY"] >= month_start) & (frame["DAY"] < mtd_cut)]["USD"].map(safe_float).sum())
    prior_rows = frame[(frame["DAY"] >= prior_start) & (frame["DAY"] < prior_cut)]
    prior = float(prior_rows["USD"].map(safe_float).sum())
    if prior_rows.empty or prior <= 0:
        return mtd, prior, None
    return mtd, prior, (mtd_same - prior) / prior * 100.0


def budget_pace_variance(mtd_actual, budget_usd, today) -> tuple[float, float]:
    """Signed variance of MTD spend vs the budget's OWN straight-line expected-to-date
    (repo wave-2 #11): ``mtd_actual - budget * day_of_month / days_in_month``.

    Isolates 'are we ahead of the budget calendar right now' (pace) from the
    structural 'will we end over' (projection). Positive = ahead of the flat daily
    budget target (burning fast); negative = behind. Returns
    (variance, expected_to_date); (0.0, 0.0) when no budget is configured (the KPI
    must not invent a denominator)."""
    budget = safe_float(budget_usd)
    if budget <= 0:
        return 0.0, 0.0
    days_in_month, elapsed, _remaining = month_days(today)
    # The MTD actual excludes today (daily metering lags ~24h), so measure the target
    # over COMPLETED days too — else an on-pace account reads ~one day's budget "behind"
    # every day. Matches the today-excluded convention (mtd_pace_vs_prior_month uses day-1).
    completed = max(elapsed - 1, 0)
    expected = budget * completed / days_in_month if days_in_month else 0.0
    return safe_float(mtd_actual) - expected, expected


def budget_burndown(daily, budget_usd, today, *, day_col="DAY", usd_col="USD") -> pd.DataFrame:
    """Cumulative actual spend THIS month vs the straight-line budget target per
    day-of-month — the canonical 'are we pacing over budget' burndown (P1 #57).
    Returns DAY, CUM_ACTUAL_USD, BUDGET_LINE_USD for the current calendar month up
    to the latest DAY present. No budget / empty / missing columns -> empty frame;
    never raises."""
    cols = ["DAY", "CUM_ACTUAL_USD", "BUDGET_LINE_USD"]
    budget = safe_float(budget_usd)
    if (daily is None or getattr(daily, "empty", True) or budget <= 0
            or day_col not in daily.columns or usd_col not in daily.columns):
        return pd.DataFrame(columns=cols)
    days_in_month, _elapsed, _rem = month_days(today)
    month_start = pd.Timestamp(today).normalize().replace(day=1)
    out = daily.copy()
    out["DAY"] = pd.to_datetime(out[day_col], errors="coerce")
    out = out.dropna(subset=["DAY"])
    out = out[out["DAY"] >= month_start].sort_values("DAY")
    if out.empty:
        return pd.DataFrame(columns=cols)
    out["CUM_ACTUAL_USD"] = pd.to_numeric(out[usd_col], errors="coerce").fillna(0.0).cumsum().round(2)
    out["BUDGET_LINE_USD"] = (budget * out["DAY"].dt.day / (days_in_month or 1)).round(2)
    return out[cols].reset_index(drop=True)
