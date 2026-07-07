"""Guarded remediation: exact fix statements + honest savings estimates.

Pure module — builds statements and numbers only. The page owns the
confirmation gate, execution, REMEDIATION_LOG audit row, and the
savings-ledger insert (always ESTIMATED until the verifier proves it).
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SIZES = ("XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE", "XXLARGE")


def _ident(name: str, what: str = "identifier") -> str:
    text = str(name or "").strip().upper()
    if not _IDENT_RE.match(text):
        raise ValueError(f"Invalid {what}: {name!r}")
    return text


def auto_suspend_fix(warehouse: str, seconds: int = 60) -> str:
    """The single highest-ROI knob for an idle-heavy warehouse."""
    seconds = max(30, min(int(seconds), 3600))
    return f"ALTER WAREHOUSE {_ident(warehouse, 'warehouse')} SET AUTO_SUSPEND = {seconds};"


def resize_fix(warehouse: str, to_size: str) -> str:
    size = str(to_size or "").strip().upper()
    if size not in _SIZES:
        raise ValueError(f"Size must be one of {_SIZES}, got {to_size!r}")
    return f"ALTER WAREHOUSE {_ident(warehouse, 'warehouse')} SET WAREHOUSE_SIZE = '{size}';"


def retention_fix(database: str, schema: str, table: str, days: int) -> str:
    days = max(0, min(int(days), 90))
    fqn = ".".join(_ident(p, "name part") for p in (database, schema, table))
    return f"ALTER TABLE {fqn} SET DATA_RETENTION_TIME_IN_DAYS = {days};"


def suspend_schedule(warehouse: str, quiet_start_hour: int, quiet_end_hour: int,
                     tz: str = "America/Chicago", weekdays_only: bool = True) -> str:
    """Suspend/resume task pair for a warehouse's quiet window.

    Requires OPERATE on the warehouse for the task owner; generated as a
    script so it can also be reviewed/run outside the app.
    """
    wh = _ident(warehouse, "warehouse")
    qs, qe = int(quiet_start_hour) % 24, int(quiet_end_hour) % 24
    if qs == qe:
        raise ValueError("Quiet window must span at least one hour")
    dow = "1-5" if weekdays_only else "*"
    return f"""-- Off-hours schedule for {wh}: suspend {qs:02d}:00, resume {qe:02d}:00 ({tz})
CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.OVERWATCH_SUSPEND_{wh}
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 0 {qs} * * {dow} {tz}'
AS
    ALTER WAREHOUSE {wh} SUSPEND;

CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.OVERWATCH_RESUME_{wh}
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 0 {qe} * * {dow} {tz}'
AS
    ALTER WAREHOUSE {wh} RESUME IF SUSPENDED;

ALTER TASK DBA_MAINT_DB.OVERWATCH.OVERWATCH_SUSPEND_{wh} RESUME;
ALTER TASK DBA_MAINT_DB.OVERWATCH.OVERWATCH_RESUME_{wh} RESUME;"""


def monthly_savings_estimate(idle_credits_window: float, window_days: int, rate: float) -> float:
    """Monthly-ized idle burn. Labeled ESTIMATED in the ledger until the
    savings verifier compares actual before/after spend."""
    if window_days <= 0:
        return 0.0
    return round(max(0.0, float(idle_credits_window)) / window_days * 30 * max(0.0, float(rate)), 2)


def propose_quiet_window(hours: list[dict], min_len: int = 4,
                         max_avg_queries: float = 1.0,
                         min_avg_credits: float = 0.05) -> dict | None:
    """Longest contiguous hour-of-day window (wrap-aware) where a warehouse
    burns credits but runs (almost) nothing.

    hours: [{"HOUR_OF_DAY": 0..23, "AVG_CREDITS": x, "AVG_QUERIES": y}, ...]
    Returns {"start", "end", "hours", "avg_credits_per_day"} or None.
    """
    by_hour = {int(h["HOUR_OF_DAY"]): h for h in hours}
    quiet = [hr for hr in range(24)
             if hr in by_hour
             and float(by_hour[hr].get("AVG_QUERIES") or 0) <= max_avg_queries
             and float(by_hour[hr].get("AVG_CREDITS") or 0) >= min_avg_credits]
    if not quiet:
        return None
    qset = set(quiet)
    best_start, best_len = None, 0
    for start in quiet:
        length = 0
        while (start + length) % 24 in qset and length < 24:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    if best_len < min_len or best_start is None:
        return None
    window = [(best_start + i) % 24 for i in range(best_len)]
    credits = sum(float(by_hour[h].get("AVG_CREDITS") or 0) for h in window)
    return {"start": best_start, "end": (best_start + best_len) % 24,
            "hours": best_len, "avg_credits_per_day": round(credits, 2)}
