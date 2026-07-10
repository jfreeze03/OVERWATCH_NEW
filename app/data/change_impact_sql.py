"""SQL builders for object change-impact tracking (V010).

Answers "did that stored procedure or task get slower or more expensive
after it was changed?" Reads OBJECT_CHANGE_REGISTRY — populated by the
daily SP_CHANGE_IMPACT_SCAN task, which freezes a 14-day pre-change
baseline and compares the 14 days after — plus a live drill-through into
the runs behind a verdict. DATABASE_NAME / SCHEMA_NAME are first-class
columns so changes are always attributable to their schema.
"""

from __future__ import annotations

import re

from app import companies
from app.config import core_object
from app.core.sqlsafe import contains_filter, sql_literal
from app.data.common import and_where, bounded_days

_TYPES = ("PROCEDURE", "TASK")
_NAME_RE = re.compile(r"^[A-Za-z0-9_$.]{1,600}$")


def change_registry(days: int, company: str = "ALL", database: str = "",
                    schema_contains: str = "") -> str:
    """Tracked object changes: frozen baseline vs post-change stats + verdict."""
    days = bounded_days(days, 120)
    where = and_where(
        f"CHANGE_SEEN_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "" if company == "ALL" else f"COMPANY = {sql_literal(company)}",
        companies.database_equals_clause(database, "DATABASE_NAME"),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, COMPANY,
    CHANGE_SEEN_AT, CHANGED_BY, VERDICT, VERDICT_DETAIL,
    BASELINE_CALLS, AFTER_CALLS,
    ROUND(BASELINE_P95_MS / 1000, 1) AS BASELINE_P95_S,
    ROUND(AFTER_P95_MS / 1000, 1) AS AFTER_P95_S,
    BASELINE_CREDITS_PER_CALL, AFTER_CREDITS_PER_CALL,
    BASELINE_FAILS, AFTER_FAILS,
    TRACKING_UNTIL, ALERTED, CHANGE_DDL
FROM {core_object("OBJECT_CHANGE_REGISTRY")}
WHERE {where}
ORDER BY CHANGE_SEEN_AT DESC
LIMIT 200
"""


def object_run_history(object_type: str, object_name: str, days: int = 28) -> str:
    """Daily run/latency/failure series for one tracked object (live)."""
    days = bounded_days(days, 60)
    otype = str(object_type or "").strip().upper()
    if otype not in _TYPES:
        raise ValueError(f"object_type must be one of {_TYPES}, got {object_type!r}")
    name = str(object_name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"Invalid object name: {object_name!r}")
    if otype == "PROCEDURE":
        short = name.split(".")[-1].upper()
        return f"""
SELECT
    DATE_TRUNC('day', START_TIME) AS DAY,
    COUNT(*) AS RUNS,
    COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
    ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 1) AS MEDIAN_S,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND QUERY_TYPE = 'CALL'
  AND POSITION({sql_literal(short + "(")} IN
               REPLACE(REPLACE(UPPER(QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
GROUP BY 1
ORDER BY 1
"""
    return f"""
SELECT
    DATE_TRUNC('day', QUERY_START_TIME) AS DAY,
    COUNT(*) AS RUNS,
    COUNT_IF(STATE = 'FAILED') AS FAILS,
    ROUND(MEDIAN(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 1) AS MEDIAN_S,
    ROUND(APPROX_PERCENTILE(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME), 0.95) / 1000, 1) AS P95_S
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE SCHEDULED_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND STATE IN ('SUCCEEDED', 'FAILED')
  AND DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME = {sql_literal(name.upper())}
GROUP BY 1
ORDER BY 1
"""


def run_scan_call() -> str:
    """Operator statement: run the change-impact scan on demand."""
    return f"CALL {core_object('SP_CHANGE_IMPACT_SCAN')}()"


_WH_NAME_RE = re.compile(r"^[A-Za-z0-9_$]{1,200}$")


def warehouse_change_registry(days: int, company: str = "ALL",
                              warehouse_contains: str = "") -> str:
    """Detected warehouse setting changes: frozen baseline vs after + verdict."""
    days = bounded_days(days, 180)
    where = and_where(
        f"CHANGE_SEEN_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "" if company == "ALL" else f"COMPANY = {sql_literal(company)}",
        contains_filter("WAREHOUSE_NAME", warehouse_contains),
    )
    return f"""
SELECT
    WAREHOUSE_NAME, COMPANY, SETTING, OLD_VALUE, NEW_VALUE, CHANGE_SEEN_AT,
    CHANGED_BY,
    IFF(CHANGED_BY IS NULL, 'UNKNOWN',
        IFF((SELECT POSITION(UPPER(w.CHANGED_BY) IN UPPER(COALESCE(MAX(s.VALUE), '')))
               FROM DBA_MAINT_DB.OVERWATCH.SETTINGS s
              WHERE s.KEY = 'DEPLOY_ACTORS') > 0,
            'MANAGED', 'MANUAL')) AS CHANGE_SOURCE,
    VERDICT, VERDICT_DETAIL,
    BASELINE_QUERIES, AFTER_QUERIES, AFTER_DAYS,
    BASELINE_CREDITS_PER_DAY, AFTER_CREDITS_PER_DAY,
    BASELINE_P95_S, AFTER_P95_S,
    BASELINE_QUEUED_MIN_PER_DAY, AFTER_QUEUED_MIN_PER_DAY,
    BASELINE_SPILL_GB_PER_DAY, AFTER_SPILL_GB_PER_DAY,
    BASELINE_FAIL_PCT, AFTER_FAIL_PCT,
    TRACKING_UNTIL, ALERTED
FROM {core_object("WAREHOUSE_CHANGE_REGISTRY")} w
WHERE {where}
ORDER BY CHANGE_SEEN_AT DESC
LIMIT 200
"""


def warehouse_daily_series(warehouse: str, days: int = 28) -> str:
    """Daily credits + p95/fails for ONE warehouse (the change-rule chart)."""
    days = bounded_days(days, 60)
    wh = str(warehouse or "").strip().upper()
    if not _WH_NAME_RE.match(wh):
        raise ValueError(f"Invalid warehouse name: {warehouse!r}")
    return f"""
SELECT
    m.DAY, m.CREDITS, q.P95_S, q.QUERIES, q.FAILS
FROM (
    SELECT DATE_TRUNC('day', START_TIME)::DATE AS DAY,
           ROUND(SUM(CREDITS_USED), 4) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND WAREHOUSE_NAME = {sql_literal(wh)}
    GROUP BY 1
) m
LEFT JOIN (
    SELECT DATE_TRUNC('day', START_TIME)::DATE AS DAY,
           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S,
           COUNT(*) AS QUERIES,
           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND WAREHOUSE_NAME = {sql_literal(wh)}
    GROUP BY 1
) q ON q.DAY = m.DAY
ORDER BY m.DAY
"""


def run_wh_scan_call() -> str:
    """Operator statement: run the warehouse change scan on demand."""
    return f"CALL {core_object('SP_WAREHOUSE_CHANGE_SCAN')}()"

