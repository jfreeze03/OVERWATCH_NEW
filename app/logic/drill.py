"""Alert fire-drill scoring: does the page actually reach a human?

Pure evaluation of OPS_ALERT_DRILL events (inserted monthly by the opt-in
snowflake/alert_drill.sql task): delivered = the notify chain stamped
NOTIFIED_AT; acknowledged = a human pressed ACK. The streak is consecutive
CALENDAR months, counted back from the newest month whose drill is DUE on the
account clock, where both happened: one outcome per month (passed if any drill
that month passed), broken by a failed month OR a missing one (a suspended or
torn-down drill task writes no row, and that is exactly the reach failure the
scoreboard exists to catch -- including when the gap is still going on now).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .formulas import ACCOUNT_TIMEZONE, account_now, safe_float

# TASK_ALERT_DRILL fires on SCHEDULE 'USING CRON 0 9 1 * * America/Chicago' (09:00 account time
# on the 1st); tests/test_logic_misc_hunt_r1.py locks these to the task's schedule.
DRILL_DAY = 1
DRILL_HOUR = 9
# A month's drill is due one hour after the cron fires: room for the task to start on a busy
# WH_ALFA_ADMIN and for Admin's 5-minute 'recent' read cache to pick the new row up.
DRILL_DUE_GRACE = timedelta(hours=1)


def _account_naive(value: datetime) -> datetime:
    """``value`` as naive account time (an aware value is converted, a naive one is taken as-is)."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(ACCOUNT_TIMEZONE).tz_localize(None)
    return ts.to_pydatetime()


def due_month(now: datetime | None = None) -> pd.Period:
    """The newest calendar month whose drill must already exist, on the account clock: this month
    once its 1st-of-month 09:00 CT run (plus the grace) has passed, else the previous month."""
    current = account_now() if now is None else _account_naive(now)
    month = pd.Period(current, freq="M")
    fires = datetime(current.year, current.month, DRILL_DAY, DRILL_HOUR) + DRILL_DUE_GRACE
    return month if current >= fires else month - 1


def drill_report(events: pd.DataFrame | None, now: datetime | None = None) -> dict:
    """-> {ran, streak_months, last: {...}} from RAISED_AT/NOTIFIED_AT/ACK_AT.

    ``now`` (default: the account clock) anchors the streak; tests pin it."""
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

    # One outcome per calendar month (account time), then walk back month by month. Counting
    # passing EVENTS instead read Sep + Jul passes with no August drill as a 2-month streak.
    raised = frame["RAISED_AT"]
    if getattr(raised.dt, "tz", None) is not None:
        raised = raised.dt.tz_convert(ACCOUNT_TIMEZONE).dt.tz_localize(None)
    passed = pd.Series([_passed(row) for _, row in frame.iterrows()], index=frame.index)
    by_month = passed.groupby(raised.dt.to_period("M")).any().sort_index(ascending=False)
    # The walk starts at the month that is DUE now, not at the newest drill: a task suspended since
    # June writes no row, and walking back from June's pass showed a green 3-month streak in
    # September. A drill already in a not-yet-due month (a manual EXECUTE TASK before 09:00 on the
    # 1st) is the newest outcome, so the walk starts there instead.
    expected = max(due_month(now), by_month.index[0])
    streak = 0
    for month, ok in by_month.items():
        if month != expected or not ok:
            break           # a missing month or a failed month ends the streak
        streak += 1
        expected = month - 1
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
