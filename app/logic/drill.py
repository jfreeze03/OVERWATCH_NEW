"""Alert fire-drill scoring: does the page actually reach a human?

Pure evaluation of OPS_ALERT_DRILL events (inserted monthly by the opt-in
snowflake/alert_drill.sql task): delivered = the notify chain stamped
NOTIFIED_AT; acknowledged = a human pressed ACK. The streak is consecutive
months, newest backward, where both happened.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_float


def drill_report(events: pd.DataFrame | None) -> dict:
    """-> {ran, streak_months, last: {...}} from RAISED_AT/NOTIFIED_AT/ACK_AT."""
    if events is None or events.empty:
        return {"ran": False, "streak_months": 0}
    frame = events.copy()
    # format="mixed": pandas 2.x otherwise infers the format from the FIRST
    # row and silently coerces every differently-shaped value to NaT.
    frame["RAISED_AT"] = pd.to_datetime(frame["RAISED_AT"], errors="coerce", format="mixed")
    frame = frame.dropna(subset=["RAISED_AT"]).sort_values("RAISED_AT", ascending=False)
    if frame.empty:
        return {"ran": False, "streak_months": 0}

    def _passed(row) -> bool:
        return pd.notna(row.get("NOTIFIED_AT")) and pd.notna(row.get("ACK_AT"))

    streak = 0
    for _, row in frame.iterrows():
        if _passed(row):
            streak += 1
        else:
            break
    last = frame.iloc[0]
    mtta_min = None
    if pd.notna(last.get("ACK_AT")):
        mtta_min = round(safe_float(
            (pd.to_datetime(last["ACK_AT"]) - last["RAISED_AT"]).total_seconds()) / 60.0, 1)
    return {
        "ran": True,
        "streak_months": int(streak),
        "last": {
            "raised_at": last["RAISED_AT"],
            "delivered": bool(pd.notna(last.get("NOTIFIED_AT"))),
            "acked": bool(pd.notna(last.get("ACK_AT"))),
            "mtta_min": mtta_min,
        },
    }
