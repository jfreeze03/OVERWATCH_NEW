"""Watched-entity monitoring (owner ask 2026-08-17).

Watching an entity in Entity 360 used to be a passive bookmark — it pinned the
entity in tables and showed an SLO badge only when you opened the Watchlist tab.
Nothing ran; it waited for you. This turns "watch" into "surface this entity's
status where I'll see it first": for each watched WAREHOUSE, evaluate a cost
spike/drop (the same robust anomaly sweep the Cost page uses) and a health-grade
drop (the per-warehouse health chip), so the Brief and the Watchlist tab flag a
watched entity that moved — without the operator hunting for it.

Read-only: this detects and surfaces, it never acts. Cost + health are
warehouse-grain, so non-warehouse watches (query family, product, task, user)
pass through with no proactive signal here — the pin + SLO badge still cover them
elsewhere. Pure pandas; tested in tests/test_watch_monitor.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    anomaly_summary,
    complete_days_only,
    flag_anomalies,
)
from app.logic.formulas import safe_float

_ATTENTION_GRADES = {"WATCH", "DEGRADED", "AT RISK"}
_BAD_GRADES = {"DEGRADED", "AT RISK"}


def watched_status(watchlist: pd.DataFrame | None,
                   wh_daily: pd.DataFrame | None,
                   health: pd.DataFrame | None,
                   rate: float = 3.68) -> pd.DataFrame:
    """Per watched entity: does it need attention, and why.

    ``watchlist``: USER_WATCHLIST rows (ENTITY_TYPE, ENTITY_KEY, LABEL).
    ``wh_daily``: FACT_WAREHOUSE_DAILY (WAREHOUSE_NAME, DAY, CREDITS_TOTAL) — the
      cost-anomaly input; priced with ``rate``.
    ``health``: warehouse_health output (WAREHOUSE_NAME, GRADE).
    Returns ENTITY_TYPE, ENTITY_KEY, LABEL, ATTENTION (bool), STATUS, SEVERITY —
    attention rows first. Empty watchlist -> empty frame."""
    cols = ["ENTITY_TYPE", "ENTITY_KEY", "LABEL", "ATTENTION", "STATUS", "SEVERITY"]
    if watchlist is None or watchlist.empty or "ENTITY_KEY" not in watchlist.columns:
        return pd.DataFrame(columns=cols)

    # Cost spike/drop per warehouse — the SAME robust sweep the Cost page runs.
    cost_hits: dict[str, dict] = {}
    if wh_daily is not None and not wh_daily.empty and "CREDITS_TOTAL" in wh_daily.columns:
        priced = complete_days_only(wh_daily.copy())
        priced["USD"] = pd.to_numeric(priced["CREDITS_TOTAL"], errors="coerce").fillna(0.0) * safe_float(rate, 3.68)
        flagged = flag_anomalies(priced, "USD", group_col="WAREHOUSE_NAME",
                                 min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
        for h in anomaly_summary(flagged, "WAREHOUSE_NAME", "USD"):
            # anomaly_summary is strongest-first; keep the STRONGEST day per warehouse.
            cost_hits.setdefault(str(h.get("label", "")).upper(), h)

    grade_by_wh: dict[str, str] = {}
    if health is not None and not health.empty and "WAREHOUSE_NAME" in health.columns:
        grade_by_wh = {str(r["WAREHOUSE_NAME"]).upper(): str(r.get("GRADE", ""))
                       for _, r in health.iterrows()}

    rows = []
    for _, w in watchlist.iterrows():
        etype = str(w.get("ENTITY_TYPE", "")).upper()
        ekey = str(w.get("ENTITY_KEY", "")).upper()
        parts: list[str] = []
        severity = ""
        is_warehouse = etype == "WAREHOUSE"
        if is_warehouse:
            hit = cost_hits.get(ekey)
            if hit:
                z = safe_float(hit.get("z"))
                parts.append(f"spend {'spike' if z > 0 else 'drop'} (z {z:+.1f})")
                severity = "warn"
            grade = grade_by_wh.get(ekey, "")
            if grade.upper() in _ATTENTION_GRADES:
                parts.append(f"health: {grade}")   # keep canonical casing ("At risk")
                if grade.upper() in _BAD_GRADES:
                    severity = "warn"
                elif not severity:
                    severity = "watch"
        if parts:
            status = "; ".join(parts)
        elif is_warehouse:
            status = "steady"       # evaluated, nothing moved
        else:
            status = ""             # cost/health are warehouse-grain — not evaluated here
        rows.append({
            "ENTITY_TYPE": str(w.get("ENTITY_TYPE", "")),
            "ENTITY_KEY": str(w.get("ENTITY_KEY", "")),
            "LABEL": str(w.get("LABEL", "") or w.get("ENTITY_KEY", "")),
            "ATTENTION": bool(parts),
            "STATUS": status,
            "SEVERITY": severity,
        })
    out = pd.DataFrame(rows)
    return out.sort_values("ATTENTION", ascending=False, kind="stable").reset_index(drop=True)[cols]


def watch_summary(status: pd.DataFrame | None) -> dict:
    """Headline for the Brief callout: how many watched entities need attention."""
    if status is None or status.empty:
        return {"watched": 0, "attention": 0, "warn": 0}
    att = status[status["ATTENTION"]]
    return {"watched": len(status),
            "attention": len(att),
            "warn": int((att["SEVERITY"] == "warn").sum())}
