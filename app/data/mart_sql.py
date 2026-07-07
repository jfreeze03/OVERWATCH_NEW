"""Readers for OVERWATCH's own Snowflake objects (marts, facts, ops tables).

Object shapes are defined by snowflake/migrations/. These builders read only;
lifecycle INSERT/UPDATE statements are built in the pages that own them.
"""

from __future__ import annotations

from app.config import core_object, mart_object
from app.core.sqlsafe import sql_literal
from app.data.common import and_where, bounded_days


def _company_filter(company: str) -> str:
    value = str(company or "ALL")
    if value.upper() == "ALL":
        return f"COMPANY = {sql_literal('ALL')}"
    return f"COMPANY = {sql_literal(value)}"


def exec_board(company: str, days: int) -> str:
    """First-paint executive board rows for one company scope and window."""
    days = bounded_days(days)
    where = and_where(_company_filter(company), f"WINDOW_DAYS = {days}")
    return f"""
SELECT PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER, REFRESHED_AT
FROM {mart_object("MART_EXEC_BOARD")}
WHERE {where}
ORDER BY PANEL, SORT_ORDER, PERIOD_START
"""


def source_freshness() -> str:
    return f"SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, HOURS_SINCE_LOAD FROM {mart_object('MART_SOURCE_FRESHNESS')} ORDER BY SOURCE_NAME"


def fact_daily_spend(days: int) -> str:
    """Account billed credits per day from the daily fact (adjustment applied)."""
    days = bounded_days(days)
    return f"""
SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY DAY
ORDER BY DAY
"""


def fact_warehouse_daily(days: int, company: str = "ALL") -> str:
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
ORDER BY DAY
"""


def fact_task_daily(days: int, company: str = "ALL", database: str = "") -> str:
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    if str(database or "").strip():
        where.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    return f"""
SELECT DAY, DATABASE_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR
FROM {mart_object("FACT_TASK_DAILY")}
WHERE {and_where(*where)}
ORDER BY FAILED DESC, DAY DESC
"""


def open_alert_events(limit: int = 200) -> str:
    limit = max(1, min(int(limit), 1000))
    return f"""
SELECT EVENT_ID, RULE_ID, RAISED_AT, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, STATUS, ACK_BY, ACK_AT
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS IN ('OPEN', 'ACK')
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, RAISED_AT DESC
LIMIT {limit}
"""


def alert_event_history(days: int) -> str:
    days = bounded_days(days)
    return f"""
SELECT DATE(RAISED_AT) AS DAY, SEVERITY, COUNT(*) AS EVENTS
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2
ORDER BY DAY
"""


def alert_rules() -> str:
    return f"""
SELECT RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, OWNER, CHANNEL, UPDATED_AT
FROM {core_object("ALERT_CONFIG")}
ORDER BY FAMILY, RULE_ID
"""


def action_queue(limit: int = 200) -> str:
    limit = max(1, min(int(limit), 1000))
    return f"""
SELECT ACTION_ID, CREATED_AT, COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, DUE_DATE,
       SOURCE, PROOF_SQL, ESTIMATED_USD, UPDATED_AT
FROM {core_object("ACTION_QUEUE")}
ORDER BY CREATED_AT DESC
LIMIT {limit}
"""


def savings_ledger() -> str:
    return f"""
SELECT ITEM_ID, ACTION_ID, CREATED_AT, DESCRIPTION, STATE, ESTIMATED_USD, VERIFIED_USD,
       VERIFIED_AT, VERIFIED_BY, PROOF_SQL, NOTES
FROM {core_object("SAVINGS_LEDGER")}
ORDER BY CREATED_AT DESC
LIMIT 500
"""


def settings() -> str:
    return f"SELECT KEY, VALUE, UPDATED_AT, UPDATED_BY FROM {core_object('SETTINGS')} ORDER BY KEY"


def schema_version() -> str:
    return f"SELECT VERSION, DESCRIPTION, APPLIED_AT FROM {core_object('SCHEMA_VERSION')} ORDER BY VERSION"


def app_error_log(limit: int = 100) -> str:
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT LOGGED_AT, PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME
FROM {core_object("APP_ERROR_LOG")}
ORDER BY LOGGED_AT DESC
LIMIT {limit}
"""


def app_self_cost(days: int) -> str:
    """What OVERWATCH itself spends: tagged queries + the app warehouse."""
    days = bounded_days(days, maximum=30)
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    COUNT(*) AS APP_QUERIES,
    SUM(TOTAL_ELAPSED_TIME) / 1000.0 AS ELAPSED_SEC,
    SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)) AS FAILED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
  AND (QUERY_TAG LIKE 'OVERWATCH%' OR WAREHOUSE_NAME = 'WH_ALFA_OVERWATCH')
GROUP BY 1
ORDER BY DAY
"""
