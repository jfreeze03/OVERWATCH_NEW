"""Warehouse change-scorecard display math. Pure module.

Verdicts are computed and stored by SP_WAREHOUSE_CHANGE_SCAN (single source
of truth so the alert and the page can never disagree); this module only
derives display artifacts from registry rows — per-metric deltas and the
KPI counts.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_float

_METRICS = (
    ("CREDITS_PER_DAY", "credits/day", 1),
    ("P95_S", "p95 s", 1),
    ("QUEUED_MIN_PER_DAY", "queue min/d", 1),
    ("SPILL_GB_PER_DAY", "spill GB/d", 2),
    ("FAIL_PCT", "fail %", 1),
)


def change_deltas(row: dict) -> list[dict]:
    """Per-metric before/after deltas for one registry row.

    Returns [{metric, base, after, delta_pct, direction}]. direction is
    'worse' when the metric moved up (all five are lower-is-better),
    'better' when down, 'flat' inside ±5% or when either side is missing.
    """
    out: list[dict] = []
    for col, label, nd in _METRICS:
        base = row.get(f"BASELINE_{col}")
        after = row.get(f"AFTER_{col}")
        if base is None or after is None or (isinstance(base, float) and pd.isna(base)) \
                or (isinstance(after, float) and pd.isna(after)):
            continue
        base_v, after_v = safe_float(base), safe_float(after)
        if base_v == 0.0 and after_v == 0.0:
            direction, delta_pct = "flat", 0.0
        elif base_v == 0.0:
            direction, delta_pct = "worse", None  # something from nothing
        else:
            delta_pct = round((after_v - base_v) / base_v * 100.0, 1)
            direction = ("worse" if delta_pct > 5.0
                         else "better" if delta_pct < -5.0 else "flat")
        out.append({
            "metric": label,
            "base": round(base_v, nd),
            "after": round(after_v, nd),
            "delta_pct": delta_pct,
            "direction": direction,
        })
    return out


def registry_kpis(df: pd.DataFrame) -> dict:
    """Counts by verdict for the KPI row; safe on empty/missing frames."""
    if df is None or df.empty or "VERDICT" not in getattr(df, "columns", ()):
        return {"changes": 0, "regressed": 0, "improved": 0, "pending": 0}
    v = df["VERDICT"].astype(str).str.upper()
    return {
        "changes": len(df),
        "regressed": int((v == "REGRESSED").sum()),
        "improved": int((v == "IMPROVED").sum()),
        "pending": int(v.isin(["PENDING", "NO_BASELINE"]).sum()),
    }
