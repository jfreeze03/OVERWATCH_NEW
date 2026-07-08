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
    "COST_CLOUD_SVC_RATIO": (False, "cloud-services ratio % today"),
    "PERF_QUERY_FAIL_PCT": (False, "query fail % today"),
}


def recheck_sql(rule_id: str, warehouse: str = "") -> str | None:
    """Single-row SQL (CURRENT_VALUE) for the rule's condition today, or None."""
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
        return """
SELECT COALESCE(SUM(CREDITS_USED_CLOUD_SERVICES), 0)
       / NULLIF(SUM(CREDITS_USED_COMPUTE), 0) * 100 AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY
WHERE START_TIME >= CURRENT_DATE()
"""
    if rid == "PERF_QUERY_FAIL_PCT":
        return """
SELECT COUNT_IF(EXECUTION_STATUS = 'FAIL') / NULLIF(COUNT(*), 0) * 100 AS CURRENT_VALUE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= CURRENT_DATE()
  AND WAREHOUSE_NAME IS NOT NULL
"""
    return None


def recheck_label(rule_id: str) -> str:
    return RECHECKABLE.get(str(rule_id or "").strip().upper(), (False, ""))[1]
