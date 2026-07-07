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
