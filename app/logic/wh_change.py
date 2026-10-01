"""Warehouse change-scorecard display math. Pure module.

Verdicts are computed and stored by SP_WAREHOUSE_CHANGE_SCAN (single source
of truth so the alert and the page can never disagree); this module only
derives display artifacts from registry rows — per-metric deltas, the
KPI counts, and the VERDICT_DETAIL text with its durations humanized.
"""

from __future__ import annotations

import re

import pandas as pd

from .formulas import humanize_duration, safe_float

_VD_P95_RE = re.compile(r"p95 (\?|-?[0-9.]+)s->(\?|-?[0-9.]+)s")
_VD_QUEUE_RE = re.compile(r"queue (\?|-?[0-9.]+)->(\?|-?[0-9.]+) min/d")


def humanize_verdict_detail(text: str) -> str:
    """A change scan's VERDICT_DETAIL string with its durations in Hr/Min/Sec.

    SP_WAREHOUSE_CHANGE_SCAN (V109) and the object-change scan (V140) build the string in SQL with
    raw seconds ('p95 1800.0s->2400.0s', 'queue 145.00->200.00 min/d'), so the drill caption read
    '1800.0s' right above a KPI showing the same p95 as '30m' (PR-1 R1-124). The numbers are left
    as they are; only the duration tokens are re-rendered. The ALERT_EVENTS.DETAIL copy is SQL-side.

    Shared by the three app surfaces that show the string (v4.606 holistic review: it was private
    to Operations, so the others still read '1800.0s'): the Operations warehouse/object change
    drills, Control Room's ranked-cause Magnitude (rca.candidates_from_changes) and Workbench Entity
    360's Recent changes DETAIL column. Spend's anomaly drill renders only the change title, not this
    string. V172 humanizes VERDICT_DETAIL in SQL (both scans, the formulas.humanize_duration twin) with a
    spaced ASCII ' -> ' arrow, so the regexes find nothing on the new text and it passes through unchanged;
    the shim stays for registry rows whose tracking closed before V172 and for as-raised ALERT_EVENTS, and
    writes the scans' own ' -> ' so an old row and a V172 row read alike side by side in the 90-day drills."""
    def _h(tok: str, unit_sec: float) -> str:
        return "?" if tok == "?" else humanize_duration(safe_float(tok) * unit_sec, "s")

    out = _VD_P95_RE.sub(lambda m: f"p95 {_h(m.group(1), 1)} -> {_h(m.group(2), 1)}", text)
    return _VD_QUEUE_RE.sub(lambda m: f"queue {_h(m.group(1), 60)} -> {_h(m.group(2), 60)}/day", out)

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
            "col": col,          # source column, so the UI can format by native unit (P95_S = seconds)
            "base": round(base_v, nd),
            "after": round(after_v, nd),
            "delta_pct": delta_pct,
            "direction": direction,
        })
    return out


# KPI key -> the untruncated window total change_impact_sql.warehouse_change_registry carries.
_WINDOW_TOTALS = (
    ("changes", "TOTAL_CHANGES"),
    ("regressed", "TOTAL_REGRESSED"),
    ("improved", "TOTAL_IMPROVED"),
    ("pending", "TOTAL_PENDING"),
)


def registry_kpis(df: pd.DataFrame) -> dict:
    """Counts by verdict for the KPI row; safe on empty/missing frames.

    The registry read is capped (newest 200 rows), so counting the frame undercounted any
    window with more changes: the tile read 200 and Regressed/Improved covered only the newest
    rows. The builder now carries untruncated window totals (window functions run before the
    LIMIT); read those, and count the frame only when they are absent (an older builder, tests).

    "pending" is PENDING only. NO_BASELINE (the warehouse was idle before the change) is a FINAL
    verdict the scan never revisits, like INSUFFICIENT_AFTER, so it is not "still accumulating";
    the object-change panel already counts PENDING only.
    """
    if df is None or df.empty or "VERDICT" not in getattr(df, "columns", ()):
        return {"changes": 0, "regressed": 0, "improved": 0, "pending": 0}
    v = df["VERDICT"].astype(str).str.upper()
    out = {
        "changes": len(df),
        "regressed": int((v == "REGRESSED").sum()),
        "improved": int((v == "IMPROVED").sum()),
        "pending": int((v == "PENDING").sum()),
    }
    first = df.iloc[0]
    for key, col in _WINDOW_TOTALS:
        if col in df.columns and pd.notna(first[col]):
            out[key] = int(safe_float(first[col]))
    return out
