"""Day-replay narrative: turn the day's cross-domain frames into headlines.

Pure module — the Control Room page fetches six day-scoped frames (spend
movers, activity vs baseline, DDL, grants, task failures, alerts) and this
distills them into the ordered story Snowsight's siloed views can't tell.
"""

from __future__ import annotations

import pandas as pd

from .formulas import format_usd, safe_float


def replay_headlines(
    movers: pd.DataFrame | None,
    activity: pd.DataFrame | None,
    ddl_count: int,
    grants_count: int,
    task_failures: int,
    critical_alerts: int,
    rate_usd: float,
) -> list[dict]:
    """Ordered [{severity, text}] — worst first; empty list = quiet day."""
    heads: list[dict] = []
    if movers is not None and not movers.empty:
        top = movers.iloc[0]
        delta_cr = safe_float(top.get("DELTA_CREDITS"))
        if abs(delta_cr) >= 1.0:
            direction = "up" if delta_cr > 0 else "down"
            heads.append({
                "severity": "bad" if delta_cr > 0 else "ok",
                "text": (f"{top.get('WAREHOUSE_NAME')} spend {direction} "
                         f"{format_usd(abs(delta_cr) * safe_float(rate_usd, 3.68))} vs its 14d norm "
                         f"({safe_float(top.get('CREDITS_TOTAL')):,.1f} cr vs "
                         f"{safe_float(top.get('BASELINE_CREDITS')):,.1f} baseline)."),
            })
    if activity is not None and not activity.empty:
        row = activity.iloc[0]
        fails = safe_float(row.get("FAILED_COUNT"))
        base_fails = safe_float(row.get("BASELINE_FAILED"))
        if fails > max(3.0, base_fails * 2):
            heads.append({
                "severity": "bad",
                "text": f"Query failures {fails:,.0f} vs {base_fails:,.1f}/day baseline.",
            })
        queued_min = safe_float(row.get("QUEUED_SEC")) / 60
        if queued_min >= 30:
            heads.append({
                "severity": "warn",
                "text": f"{queued_min:,.0f} queued minutes across the day.",
            })
    if critical_alerts:
        heads.append({"severity": "bad",
                      "text": f"{critical_alerts} CRITICAL alert(s) raised."})
    if task_failures:
        heads.append({"severity": "warn",
                      "text": f"{task_failures} task failure(s)."})
    if ddl_count:
        heads.append({"severity": "info",
                      "text": f"{ddl_count} DDL change(s) landed — correlate below."})
    if grants_count:
        heads.append({"severity": "info",
                      "text": f"{grants_count} role grant(s) changed."})
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    heads.sort(key=lambda h: order.get(h["severity"], 9))
    return heads
