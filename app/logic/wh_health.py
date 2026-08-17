"""Per-warehouse health chip (repo review "What to Borrow", wave 3).

A transparent 0-100 grade per warehouse: base 100 minus itemized, capped penalties
for queueing / remote spill / long p95 runtime / low utilization — and it names
WHICH penalty fired. Mirrors app/logic/scoring.py's platform_score (base-100 minus
capped named drivers) but at warehouse grain.

Fed the size_recommendations() output, whose rates are ALREADY per-day over the
served window (QUEUED_MIN_PER_DAY, SPILL_GB_PER_DAY) — scoring the raw window
totals would make the grade a function of window length (scoring.py C8). Pure
pandas; no Streamlit, no Snowflake. Tested in tests/test_wave3_efficiency.py
(incl. an integration test that pipes real size_recommendations output through,
so a column rename there can't silently disarm a penalty).
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float

# Penalty rates (per unit) and caps (max points any one driver can take). Uncalibrated
# starting points, mirroring the platform-score thresholds where they exist.
W_QUEUE_PER_MIN = 1.5     # per queued min/day over the threshold
QUEUE_THRESHOLD_MIN = 10.0
QUEUE_CAP = 25.0
W_SPILL_PER_GB = 4.0      # per remote-spill GB/day over the threshold (tighter than
SPILL_THRESHOLD_GB = 1.0  # the account-level platform score — this is one warehouse)
SPILL_CAP = 20.0
LONG_P95_SEC = 120.0      # p95 runtime above this is "long" (well above the sizing
W_P95 = 20.0              # module's <=10s size-DOWN band, so the two never contradict)
P95_CAP = 20.0
LOW_UTIL_PCT = 50.0       # utilization below this is low (matches sizing SUSPEND_FIRST)
W_UTIL_PER_PCT = 0.4
UTIL_CAP = 20.0
# The low-utilization penalty needs enough active days to be real — else a
# nightly-batch warehouse grades falsely "At risk".
MIN_ACTIVE_DAYS_FOR_UTIL = 3.0


def _cap(value: float, cap: float) -> float:
    return min(max(value, 0.0), cap)


def _grade(score: float) -> str:
    if score >= 85:
        return "Healthy"
    if score >= 70:
        return "Watch"
    if score >= 50:
        return "Degraded"
    return "At risk"


def warehouse_health(sized: pd.DataFrame | None) -> pd.DataFrame:
    """Per-warehouse SCORE (0-100), GRADE, WHY from the size_recommendations frame.

    Reads QUEUED_MIN_PER_DAY, SPILL_GB_PER_DAY, P95_ELAPSED_SEC, IDLE_PCT, and
    (for the utilization gate) EVIDENCE_SUFFICIENT / ACTIVE_DAYS_PER_30D. Missing
    columns simply contribute no penalty. Empty in -> empty out (fixed columns)."""
    cols = ["WAREHOUSE_NAME", "SCORE", "GRADE", "WHY"]
    if sized is None or sized.empty or "WAREHOUSE_NAME" not in sized.columns:
        return pd.DataFrame(columns=cols)
    rows = []
    for _, r in sized.iterrows():
        score = 100.0
        why: list[str] = []

        q = safe_float(r.get("QUEUED_MIN_PER_DAY"))
        if q > QUEUE_THRESHOLD_MIN:
            pen = _cap((q - QUEUE_THRESHOLD_MIN) * W_QUEUE_PER_MIN, QUEUE_CAP)
            score -= pen
            why.append(f"{q:.0f} min/day queued")

        sp = safe_float(r.get("SPILL_GB_PER_DAY"))
        if sp > SPILL_THRESHOLD_GB:
            pen = _cap((sp - SPILL_THRESHOLD_GB) * W_SPILL_PER_GB, SPILL_CAP)
            score -= pen
            why.append(f"{sp:.1f} GB/day remote spill")

        p95 = safe_float(r.get("P95_ELAPSED_SEC"))
        if p95 > LONG_P95_SEC:
            pen = _cap((p95 - LONG_P95_SEC) / LONG_P95_SEC * W_P95, P95_CAP)
            score -= pen
            why.append(f"p95 runtime {p95:.0f}s")

        # Low utilization — evidence-gated (a sparse nightly-batch WH must not grade
        # "At risk" just for being idle between runs).
        idle_pct = safe_float(r.get("IDLE_PCT"))
        util_pct = 100.0 - idle_pct
        active_days = safe_float(r.get("ACTIVE_DAYS_PER_30D"),
                                 safe_float(r.get("ACTIVE_QUERY_DAYS")))
        evidence_ok = bool(r.get("EVIDENCE_SUFFICIENT")) if "EVIDENCE_SUFFICIENT" in sized.columns \
            else active_days >= MIN_ACTIVE_DAYS_FOR_UTIL
        if util_pct < LOW_UTIL_PCT and evidence_ok and active_days >= MIN_ACTIVE_DAYS_FOR_UTIL:
            pen = _cap((LOW_UTIL_PCT - util_pct) * W_UTIL_PER_PCT, UTIL_CAP)
            score -= pen
            why.append(f"{util_pct:.0f}% utilization")

        final = round(max(0.0, score))
        rows.append({
            "WAREHOUSE_NAME": str(r.get("WAREHOUSE_NAME")),
            "SCORE": final, "GRADE": _grade(final),
            "WHY": "; ".join(why) if why else "no penalties",
        })
    return (pd.DataFrame(rows).sort_values("SCORE").reset_index(drop=True)[cols])
