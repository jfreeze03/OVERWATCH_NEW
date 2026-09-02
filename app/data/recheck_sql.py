"""Live re-checks for alert conditions ("is this still true, right now?").

Each builder answers ONE rule's condition for a specific target with today's
data, so the drawer can show current-vs-threshold before someone resolves.
Coverage is deliberately the warehouse-lever rules the drawer already
special-cases; model-based rules (anomaly sweep) have no point-in-time
recheck. Pure module; identifiers validated via sqlsafe.
"""

from __future__ import annotations

from app.core.sqlsafe import safe_identifier, sql_literal

# rule id -> (needs_warehouse, value label)
RECHECKABLE: dict[str, tuple[bool, str]] = {
    "COST_WH_DAILY_CREDITS": (True, "credits today"),
    "PERF_QUEUED_MINUTES": (True, "queued minutes today"),
    "PERF_SPILL_GB": (True, "remote spill GB today"),
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
    if rid == "PERF_QUEUED_MINUTES":
        return f"""
SELECT COALESCE(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)), 0)
       / 60000.0 AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= CURRENT_DATE() {wh_clause}
"""
    if rid == "PERF_SPILL_GB":
        return f"""
SELECT COALESCE(SUM(BYTES_SPILLED_TO_REMOTE_STORAGE), 0) / POWER(1024, 3) AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= CURRENT_DATE() {wh_clause}
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
_TRAILING_24H_RULES = frozenset({"PERF_QUERY_FAIL_PCT"})


def recheck_window_phrase(rule_id: str) -> str:
    """How to describe the window a rule's re-check evaluates, for the drawer button help.

    Four rules filter ``START_TIME >= CURRENT_DATE()`` (since account-midnight = today);
    PERF_QUERY_FAIL_PCT filters a rolling ``HOUR_TS >= DATEADD('hour', -24, ...)`` window
    to match the alert definition. The button help is one shared string, so it must ask
    the builder which window this rule actually uses instead of hard-coding "today".
    """
    rid = str(rule_id or "").strip().upper()
    return "the last 24h of data" if rid in _TRAILING_24H_RULES else "today's data"
