"""Adaptive-compute candidacy (repo review "What to Borrow", wave 3).

Which warehouses would benefit from adaptive / auto-scaling compute? A warehouse
whose load is BURSTY (tall peaks, quiet troughs) with MATERIAL volume gains from
adding clusters at the peak and dropping them off-peak; a flat, always-on (or
always-idle) warehouse does not — a fixed size (or auto-suspend) is the right
lever there. We score each warehouse 0-100 from the hour-of-day load profile it
already reports, and name the better lever when adaptive compute is not it.

Pure pandas over frames the Operations page already fetches (warehouse_hourly_
activity + idle_warehouse_analysis); no Streamlit, no Snowflake. The score is an
ordering heuristic, not a guarantee — the rationale column shows the inputs so a
DBA can judge. Tested in tests/test_adaptive.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float

# Burstiness is peak-to-mean of the hour-of-day credit profile. 1.0 = perfectly
# flat; a warehouse only benefits from adaptive scaling once its peak clearly
# exceeds its average. Below ANCHOR it scores 0; at/above CEIL it saturates.
BURST_ANCHOR = 1.6
BURST_CEIL = 5.0
# Volume gate: below this daily-credit floor a warehouse is too small to bother
# auto-scaling (the score is scaled down proportionally, not hard-zeroed). Raised
# from 2 -> 10 (review #3): ~2 cr/day is a right-size/suspend case, not a
# multi-cluster one, and a single-hour blip there should not saturate to 100.
MIN_DAILY_CREDITS = 10.0
# Heavy idle means the primary lever is auto-suspend, not adaptive compute, so
# idle discounts the adaptive fit (never to zero — a bursty+idle WH still counts).
IDLE_MAX_DISCOUNT = 0.5
# ...and above this idle share, auto-suspend is unambiguously the better lever, so
# it OVERRIDES the verdict regardless of burstiness (review #2 — the discount alone
# floored at 0.5 and could never route a bursty+idle WH to the right advice).
HIGH_IDLE_PCT = 50.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def adaptive_compute_candidacy(hourly: pd.DataFrame | None,
                               idle: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-warehouse adaptive-compute candidacy.

    ``hourly`` (warehouse_hourly_activity): WAREHOUSE_NAME, HOUR_OF_DAY, AVG_CREDITS.
    ``idle`` (idle_warehouse_analysis, optional): WAREHOUSE_NAME, TOTAL_CREDITS,
    IDLE_CREDITS — folds in an idle discount + surfaces IDLE_PCT.

    Returns one row per warehouse: SCORE (0-100, desc), VERDICT, PEAK_TO_MEAN,
    DAILY_CREDITS, IDLE_PCT, RATIONALE. Empty in, empty out.
    """
    cols = ["WAREHOUSE_NAME", "SCORE", "VERDICT", "PEAK_TO_MEAN",
            "DAILY_CREDITS", "IDLE_PCT", "RATIONALE"]
    if hourly is None or hourly.empty or "WAREHOUSE_NAME" not in hourly.columns:
        return pd.DataFrame(columns=cols)
    h = hourly.copy()
    h["AVG_CREDITS"] = pd.to_numeric(h.get("AVG_CREDITS"), errors="coerce").fillna(0.0)

    idle_pct_by_wh: dict[str, float] = {}
    if idle is not None and not idle.empty and "WAREHOUSE_NAME" in idle.columns:
        i = idle.copy()
        tot = pd.to_numeric(i.get("TOTAL_CREDITS"), errors="coerce").fillna(0.0)
        idl = pd.to_numeric(i.get("IDLE_CREDITS"), errors="coerce").fillna(0.0)
        i["_PCT"] = (idl / tot.where(tot > 0) * 100).fillna(0.0)
        idle_pct_by_wh = dict(zip(i["WAREHOUSE_NAME"].astype(str), i["_PCT"], strict=False))

    rows = []
    for wh, grp in h.groupby("WAREHOUSE_NAME"):
        credits = grp["AVG_CREDITS"]
        daily = float(credits.sum())            # true credits on an average DAY
        peak = float(credits.max())
        # Review #1 (crux): WAREHOUSE_METERING_HISTORY emits NO row for a suspended
        # hour, so credits.mean() would divide only by the ACTIVE hours and read a
        # nightly-batch (maximally bursty) warehouse as flat. The absent hours are
        # genuinely 0 credits, so the true 24-hour mean is the metered sum / 24 —
        # this makes peak-to-mean the real all-day burstiness the model intends.
        mean = daily / 24.0
        if daily <= 0:
            continue
        peak_to_mean = (peak / mean) if mean > 0 else 1.0
        burst = _clamp01((peak_to_mean - BURST_ANCHOR) / (BURST_CEIL - BURST_ANCHOR))
        volume_gate = _clamp01(daily / MIN_DAILY_CREDITS)
        idle_pct = safe_float(idle_pct_by_wh.get(str(wh), 0.0))
        idle_discount = 1.0 - IDLE_MAX_DISCOUNT * _clamp01(idle_pct / 100.0)
        score = round(100 * burst * volume_gate * idle_discount)
        if idle_pct >= HIGH_IDLE_PCT:
            verdict = "Auto-suspend first"     # idle dominates; multi-cluster won't help it
        elif score >= 60:
            verdict = "Strong candidate"
        elif score >= 35:
            verdict = "Consider"
        else:
            verdict = "Not bursty enough"
        rationale = (f"{peak_to_mean:.1f}x peak-to-mean, {daily:,.1f} cr/day"
                     + (f", {idle_pct:.0f}% idle" if idle_pct >= 1 else ""))
        rows.append({
            "WAREHOUSE_NAME": str(wh), "SCORE": int(score), "VERDICT": verdict,
            "PEAK_TO_MEAN": round(peak_to_mean, 1), "DAILY_CREDITS": round(daily, 1),
            "IDLE_PCT": round(idle_pct, 0), "RATIONALE": rationale,
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    return (pd.DataFrame(rows).sort_values(["SCORE", "DAILY_CREDITS"], ascending=False)
            .reset_index(drop=True)[cols])


def candidacy_summary(scored: pd.DataFrame) -> dict:
    """Headline counts for the KPI row."""
    if scored is None or scored.empty:
        return {"warehouses": 0, "strong": 0, "consider": 0}
    v = scored["VERDICT"].astype(str)
    return {"warehouses": len(scored),
            "strong": int((v == "Strong candidate").sum()),
            "consider": int((v == "Consider").sum())}
