"""Warehouse right-sizing advisor (pure, tested).

Transparent scenario model, not a promise: Snowflake size steps double/halve
the credit rate, so the table shows current monthly spend next to the
mechanical x0.5 / x2.0 scenarios and a rules-based recommendation. Runtime
effects are workload-dependent; the rationale says why, the DBA decides.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_div, safe_float

QUEUE_UP_MIN_PER_DAY = 30.0    # sustained queueing -> size up / add cluster
SPILL_UP_GB = 5.0              # remote spill in window -> size up
DOWN_P95_SEC = 10.0            # fast p95 and calm queue -> down candidate
DOWN_IDLE_PCT = 30.0           # meaningful idle share strengthens down case
SUSPEND_FIRST_IDLE_PCT = 50.0  # mostly idle -> fix auto-suspend before resizing

RECOMMEND_UP = "Size up / add cluster"
RECOMMEND_DOWN = "Size down candidate"
RECOMMEND_SUSPEND = "Tune auto-suspend first"
RECOMMEND_KEEP = "Keep"


def size_recommendations(df: pd.DataFrame, credit_rate_usd: float, window_days: int) -> pd.DataFrame:
    """Score each warehouse row and attach scenario dollars + recommendation.

    Expects columns: WAREHOUSE_NAME, CREDITS_TOTAL, QUERY_COUNT, P95_ELAPSED_SEC,
    QUEUED_SEC, SPILL_REMOTE_GB, IDLE_PCT (optional).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    rate = safe_float(credit_rate_usd, 3.68)
    days = max(int(window_days or 1), 1)
    out = df.copy()
    for col in ("CREDITS_TOTAL", "QUERY_COUNT", "P95_ELAPSED_SEC", "QUEUED_SEC",
                "SPILL_REMOTE_GB", "IDLE_PCT"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    if "IDLE_PCT" not in out.columns:
        out["IDLE_PCT"] = 0.0

    out["QUEUED_MIN_PER_DAY"] = (out["QUEUED_SEC"] / 60.0 / days).round(1)
    out["MONTHLY_USD_NOW"] = (out["CREDITS_TOTAL"] * rate / days * 30).round(0)
    out["SCENARIO_DOWN_USD"] = (out["MONTHLY_USD_NOW"] * 0.5).round(0)
    out["SCENARIO_UP_USD"] = (out["MONTHLY_USD_NOW"] * 2.0).round(0)

    def _recommend(row) -> tuple[str, str]:
        queued = row["QUEUED_MIN_PER_DAY"]
        spill = row["SPILL_REMOTE_GB"]
        idle = row["IDLE_PCT"]
        p95 = row["P95_ELAPSED_SEC"]
        if queued >= QUEUE_UP_MIN_PER_DAY or spill >= SPILL_UP_GB:
            why = []
            if queued >= QUEUE_UP_MIN_PER_DAY:
                why.append(f"{queued:.0f} queued min/day")
            if spill >= SPILL_UP_GB:
                why.append(f"{spill:.1f} GB remote spill")
            return RECOMMEND_UP, "Concurrency/memory pressure: " + ", ".join(why) + "."
        if idle >= SUSPEND_FIRST_IDLE_PCT:
            return RECOMMEND_SUSPEND, (
                f"{idle:.0f}% of credits are idle-hours - shorten AUTO_SUSPEND before resizing.")
        if queued < 1 and spill < 0.5 and p95 <= DOWN_P95_SEC and idle >= DOWN_IDLE_PCT:
            return RECOMMEND_DOWN, (
                f"No queueing, no spill, p95 {p95:.1f}s, {idle:.0f}% idle - "
                "one size down likely holds SLAs at half the rate.")
        return RECOMMEND_KEEP, "Load and capacity look matched for this window."

    verdicts = out.apply(_recommend, axis=1, result_type="expand")
    out["RECOMMENDATION"] = verdicts[0]
    out["RATIONALE"] = verdicts[1]
    out["POTENTIAL_MONTHLY_SAVING_USD"] = out.apply(
        lambda r: round(r["MONTHLY_USD_NOW"] - r["SCENARIO_DOWN_USD"], 0)
        if r["RECOMMENDATION"] == RECOMMEND_DOWN else 0.0,
        axis=1,
    )
    order = {RECOMMEND_UP: 0, RECOMMEND_DOWN: 1, RECOMMEND_SUSPEND: 2, RECOMMEND_KEEP: 3}
    out["_O"] = out["RECOMMENDATION"].map(order).fillna(9)
    return (out.sort_values(["_O", "MONTHLY_USD_NOW"], ascending=[True, False])
            .drop(columns="_O").reset_index(drop=True))


def sizing_summary(out: pd.DataFrame) -> dict:
    if out is None or out.empty:
        return {"up": 0, "down": 0, "suspend": 0, "potential_saving_usd": 0.0}
    rec = out["RECOMMENDATION"]
    return {
        "up": int((rec == RECOMMEND_UP).sum()),
        "down": int((rec == RECOMMEND_DOWN).sum()),
        "suspend": int((rec == RECOMMEND_SUSPEND).sum()),
        "potential_saving_usd": round(float(out["POTENTIAL_MONTHLY_SAVING_USD"].sum()), 0),
    }


def _unused_guard() -> float:  # pragma: no cover - keeps safe_div imported for future ratios
    return safe_div(1, 1)


# ---------------------------------------------------------------------------
# Interactive what-if simulator (pure; the UI supplies observed inputs)
# ---------------------------------------------------------------------------

SIZE_ORDER = ("XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE",
              "2XLARGE", "3XLARGE", "4XLARGE")
_SIZE_ALIASES = {"X-SMALL": "XSMALL", "XS": "XSMALL", "S": "SMALL", "M": "MEDIUM",
                 "L": "LARGE", "X-LARGE": "XLARGE", "XL": "XLARGE",
                 "2X-LARGE": "2XLARGE", "3X-LARGE": "3XLARGE", "4X-LARGE": "4XLARGE"}


def normalize_size(size: object) -> str:
    text = str(size or "").strip().upper().replace("_", "-")
    text = _SIZE_ALIASES.get(text, text.replace("-", ""))
    return text if text in SIZE_ORDER else ""


def shifted_size(size: str, delta: int) -> str:
    """Size N steps up/down the ladder, clamped at the ends ('' if unknown)."""
    current = normalize_size(size)
    if not current:
        return ""
    idx = max(0, min(SIZE_ORDER.index(current) + int(delta), len(SIZE_ORDER) - 1))
    return SIZE_ORDER[idx]


def simulate_scenario(
    *,
    size: str,
    credits_window: float,
    idle_credits_window: float,
    window_days: int,
    rate_usd: float,
    size_delta: int = 0,
    autosuspend_now_s: int = 600,
    autosuspend_new_s: int = 60,
) -> dict:
    """What-if for one warehouse: size step and/or auto-suspend change.

    Transparent replay of the observed window, not a promise:
    - Busy credits scale between two stated bounds. Sizing up (rate x2 per
      step): worst case queries run the SAME wall time (cost x2), best case
      they halve (cost-neutral). Sizing down mirrors that.
    - Idle credits scale with the new rate AND shrink/grow with the
      auto-suspend ratio (capped at 2x — longer suspends can't burn more
      than always-on).
    Returns monthly dollars: {ok, size_now, size_new, monthly_now_usd,
    monthly_low_usd, monthly_high_usd, assumptions: [...]}
    """
    from .formulas import safe_float as _sf

    size_now = normalize_size(size)
    size_new = shifted_size(size_now, size_delta)
    if not size_now:
        return {"ok": False, "reason": f"Unknown warehouse size {size!r}."}
    days = max(int(window_days or 1), 1)
    rate = _sf(rate_usd, 3.68)
    total = max(0.0, _sf(credits_window))
    idle = min(max(0.0, _sf(idle_credits_window)), total)
    busy = total - idle
    # Effective applied delta after clamping at the ladder ends.
    applied_delta = SIZE_ORDER.index(size_new) - SIZE_ORDER.index(size_now)
    factor = 2.0 ** applied_delta
    busy_bounds = sorted((busy * factor, busy * 1.0))
    suspend_ratio = min(max(_sf(autosuspend_new_s, 60), 0.0)
                        / max(_sf(autosuspend_now_s, 600), 1.0), 2.0)
    idle_new = idle * factor * suspend_ratio
    to_month = 30.0 / days

    def _usd(credits: float) -> float:
        return round(credits * to_month * rate, 0)

    low = _usd(busy_bounds[0] + idle_new)
    high = _usd(busy_bounds[1] + idle_new)
    assumptions = [
        f"Observed window: {days}d, {total:,.1f} credits ({idle:,.1f} idle).",
        (f"Size {size_now} -> {size_new}: busy credits bounded between rate-scaled "
         f"(x{factor:g}) and cost-neutral (perfect runtime scaling)."
         if applied_delta else "Size unchanged: busy credits unchanged."),
        (f"Auto-suspend {int(autosuspend_now_s)}s -> {int(autosuspend_new_s)}s: idle credits "
         f"scaled x{suspend_ratio:.2f} (linear with suspend window, capped at 2x)."),
        "Idle burns at the NEW size's rate. Concurrency, caching, and queueing shifts are not modeled.",
    ]
    return {
        "ok": True,
        "size_now": size_now,
        "size_new": size_new,
        "monthly_now_usd": _usd(total),
        "monthly_low_usd": min(low, high),
        "monthly_high_usd": max(low, high),
        "assumptions": assumptions,
    }


def price_per_run_bounds(allocated_credits: float, runs: int, rate_usd: float,
                         size_delta: int = 0) -> dict:
    """$/run for a query pattern, now and at a size step, as honest bounds.

    Same assumption pair as simulate_scenario: a size step multiplies the
    rate by 2^delta; runtime lands between unchanged (rate-scaled cost) and
    perfectly scaled (cost-neutral).
    """
    from .formulas import safe_float as _sf

    runs_n = max(int(runs or 0), 1)
    per_run_now = _sf(allocated_credits) / runs_n * _sf(rate_usd, 3.68)
    factor = 2.0 ** int(size_delta)
    bounds = sorted((per_run_now * factor, per_run_now))
    return {
        "per_run_now_usd": round(per_run_now, 4),
        "per_run_low_usd": round(bounds[0], 4),
        "per_run_high_usd": round(bounds[1], 4),
    }
