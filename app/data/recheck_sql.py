"""Live re-checks for alert conditions ("is this still true, right now?").

Each builder answers ONE rule's condition for a specific target on the SAME
basis the alert uses (window + source), so the drawer can show
current-vs-threshold before someone resolves. Coverage is deliberately the
warehouse-lever rules the drawer already special-cases; model-based rules
(anomaly sweep) have no point-in-time recheck. Pure module; identifiers
validated via sqlsafe.
"""

from __future__ import annotations

import re

from app.core.sqlsafe import safe_identifier, sql_literal

# rule id -> (needs_warehouse, value label)
RECHECKABLE: dict[str, tuple[bool, str]] = {
    "COST_WH_DAILY_CREDITS": (True, "credits today"),
    # review R1-040: the alert (SP_ALERT_SCAN [04]/[05]) and its V091 auto-clear sweep sum a TRAILING 24h of
    # FACT_QUERY_HOURLY; the re-check now reads that same basis, so the label says 24h, not "today".
    "PERF_QUEUED_MINUTES": (True, "queued time (24h)"),
    "PERF_SPILL_GB": (True, "remote spill GB (24h)"),
    "COST_CLOUD_SVC_RATIO": (True, "cloud-services ratio % today"),
    "PERF_QUERY_FAIL_PCT": (False, "query fail % (24h)"),
}


def recheck_sql(rule_id: str, warehouse: str = "", company: str = "") -> str | None:
    """Single-row SQL (CURRENT_VALUE) for the rule's condition today, or None.

    ``company`` is the event's COMPANY — per-company rules (query-fail %) must
    re-check the SAME company the alert fired on, not an account-wide blend.
    """
    rid = str(rule_id or "").strip().upper()
    if rid not in RECHECKABLE:
        return None
    needs_wh, _label = RECHECKABLE[rid]
    wh_clause = ""
    if needs_wh:
        if not str(warehouse or "").strip():
            return None
        try:
            wh = safe_identifier(str(warehouse).strip())
        except ValueError:
            return None  # garbage target extracted from event text: no recheck
        wh_clause = f"AND UPPER(WAREHOUSE_NAME) = {sql_literal(wh.upper())}"
    if rid == "COST_WH_DAILY_CREDITS":
        return f"""
SELECT COALESCE(SUM(CREDITS_USED), 0) AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= CURRENT_DATE() {wh_clause}
"""
    if rid in ("PERF_QUEUED_MINUTES", "PERF_SPILL_GB"):
        # Review R1-040: match the alert exactly -- per warehouse, TRAILING 24h, from FACT_QUERY_HOURLY, the basis
        # SP_ALERT_SCAN arms [04]/[05] raise on and the V091 auto-clear sweep keeps an event firing on. The old
        # re-check summed raw QUERY_HISTORY since account-midnight: a strict subset of that window, so in the
        # morning it read "Condition clear" (and offered the ACTIONED resolve) while the scan still saw the
        # event over its threshold. Warehouse-only filter, no company: the sweep groups by warehouse.
        value = ("COALESCE(SUM(QUEUED_SEC_SUM), 0) / 60" if rid == "PERF_QUEUED_MINUTES"
                 else "COALESCE(SUM(SPILL_REMOTE_GB), 0)")
        return f"""
SELECT {value} AS CURRENT_VALUE
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME IS NOT NULL {wh_clause}
"""
    if rid == "COST_CLOUD_SVC_RATIO":
        # Warehouse-scoped to match the alert exactly (WAREHOUSE_METERING_HISTORY,
        # CS / total credits) — the old recheck read the account-wide ratio off
        # METERING_HISTORY, so the drawer showed a different number than the
        # per-warehouse alert it was re-checking.
        return f"""
SELECT COALESCE(SUM(CREDITS_USED_CLOUD_SERVICES), 0)
       / NULLIF(SUM(CREDITS_USED), 0) * 100 AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= CURRENT_DATE() {wh_clause}
"""
    if rid == "PERF_QUERY_FAIL_PCT":
        # Match the alert exactly: per-COMPANY, trailing 24h, from FACT_QUERY_HOURLY.
        # The old recheck read an account-wide, since-midnight rate off raw
        # QUERY_HISTORY, so it blended companies and could report "clear" while the
        # company the alert fired on was still failing.
        comp = ""
        if str(company or "").strip() and str(company).strip().upper() != "ALL":
            comp = f"AND COMPANY = {sql_literal(str(company).strip())}"
        # < 20 queries reads as 0% (clear): the alert has HAVING SUM(QUERY_COUNT)
        # >= 20, so below that volume it would not fire and the re-check must agree.
        return f"""
SELECT IFF(SUM(QUERY_COUNT) < 20, 0,
           SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS CURRENT_VALUE
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP()) {comp}
"""
    return None


def recheck_label(rule_id: str) -> str:
    return RECHECKABLE.get(str(rule_id or "").strip().upper(), (False, ""))[1]


# Rules whose re-check evaluates a rolling trailing-24h window (to match the alert's
# own basis) rather than since account-midnight. Keep this next to the SQL so the
# drawer help can never drift from what the builder actually filters on.
_TRAILING_24H_RULES = frozenset({"PERF_QUERY_FAIL_PCT", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB"})


def recheck_window_phrase(rule_id: str) -> str:
    """How to describe the window a rule's re-check evaluates, for the drawer button help.

    COST_WH_DAILY_CREDITS and COST_CLOUD_SVC_RATIO filter ``START_TIME >= CURRENT_DATE()``
    (since account-midnight = today); PERF_QUERY_FAIL_PCT, PERF_QUEUED_MINUTES and
    PERF_SPILL_GB filter a rolling ``HOUR_TS >= DATEADD('hour', -24, ...)`` window to match
    the alert definition. The button help is one shared string, so it must ask the builder
    which window this rule actually uses instead of hard-coding "today".
    """
    rid = str(rule_id or "").strip().upper()
    return "the last 24h of data" if rid in _TRAILING_24H_RULES else "today's data"


# Review R1-040: rules whose condition is ONE DAY's total, raised for that day (SP_ALERT_SCAN arm [02] titles
# "<WH> used N credits on YYYY-MM-DD", DEDUPE_KEY RULE|WH|DAY, DAY = yesterday or today). The re-check sums
# today since account-midnight, so for an event raised for an EARLIER day it measures a different, partial day:
# it can still show today is over the threshold too, but it can never show that event's condition cleared.
_FULL_DAY_RULES = frozenset({"COST_WH_DAILY_CREDITS"})
_ON_DAY_RE = re.compile(r"\bon (\d{4}-\d{2}-\d{2})\b")


def recheck_closed_day(rule_id: str, title: str, today: str) -> str:
    """The event's measured day (``YYYY-MM-DD``) when ``rule_id`` is a full-day-total rule and the event's title
    names a day BEFORE ``today`` (an ISO date, account timezone); '' otherwise -- including an event for today,
    whose since-midnight re-check is the same partial day the alert measured. Pure; never raises."""
    rid = str(rule_id or "").strip().upper()
    if rid not in _FULL_DAY_RULES:
        return ""
    match = _ON_DAY_RE.search(str(title or ""))
    day = match.group(1) if match else ""
    return day if day and day < str(today or "")[:10] else ""
