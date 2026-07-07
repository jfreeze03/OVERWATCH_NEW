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


def fact_metering_by_service(days: int) -> str:
    """Spend-tab hot path: same output shape as the live metering reader,
    served from the hourly-loaded fact instead of ACCOUNT_USAGE."""
    days = bounded_days(days)
    return f"""
SELECT DAY, SERVICE_TYPE, CREDITS_USED, CREDITS_BILLED, CREDITS_ADJUSTMENT
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY DAY, SERVICE_TYPE
"""


def fact_query_window_summary(days: int, company: str = "ALL", warehouse_contains: str = "",
                              user_contains: str = "", database: str = "") -> str:
    """Ops Queries-tab hot path from FACT_QUERY_HOURLY.

    Counts, failures, queued time and spill are exact sums of the hourly
    fact. P95 is the PEAK hourly-group p95 (a true p95 needs raw rows) —
    the UI labels it as such. No schema dimension in the fact, so callers
    fall back to live when a schema filter is active.
    """
    from app import companies
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    where = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    where.append(contains_filter("WAREHOUSE_NAME", warehouse_contains))
    where.append(contains_filter("USER_NAME", user_contains))
    where.append(companies.database_equals_clause(database, "DATABASE_NAME"))
    return f"""
SELECT
    SUM(QUERY_COUNT) AS QUERY_COUNT,
    SUM(FAILED_COUNT) AS FAILED_COUNT,
    MAX(P95_ELAPSED_SEC) AS P95_ELAPSED_SEC,
    SUM(QUEUED_SEC_SUM) AS QUEUED_SEC,
    SUM(SPILL_REMOTE_GB) AS SPILL_REMOTE_GB
FROM {mart_object("FACT_QUERY_HOURLY")}
WHERE {and_where(*where)}
"""


def app_statement_stats(days: int = 7) -> str:
    """The app's own slowest statement families on the dedicated warehouse.

    Groups by QUERY_PARAMETERIZED_HASH so each app query pattern (all pages,
    all filter values) collapses to one row — the honest way to find which
    builder to optimize next.
    """
    from app.config import APP_WAREHOUSE

    days = bounded_days(days, 30)
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT,
    COUNT(*) AS RUNS,
    COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
    ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
    ROUND(AVG(BYTES_SCANNED) / POWER(1024, 3), 3) AS AVG_GB_SCANNED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY P95_S DESC
LIMIT 30
"""


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


def alert_mttr(days: int = 90) -> str:
    """Weekly MTTA/MTTR from alert lifecycle timestamps."""
    days = bounded_days(days)
    return f"""
SELECT
    DATE_TRUNC('week', RAISED_AT)::DATE AS WEEK,
    COUNT(*) AS EVENTS,
    SUM(IFF(ACK_AT IS NOT NULL, 1, 0)) AS ACKED,
    SUM(IFF(RESOLVED_AT IS NOT NULL, 1, 0)) AS RESOLVED,
    ROUND(AVG(DATEDIFF('minute', RAISED_AT, ACK_AT)), 1) AS MTTA_MIN,
    ROUND(AVG(DATEDIFF('minute', RAISED_AT, RESOLVED_AT)), 1) AS MTTR_MIN
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1
ORDER BY WEEK
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


def latest_digest() -> str:
    return f"""
SELECT DIGEST_DATE, MODEL, BODY, CREATED_AT
FROM {core_object("DAILY_DIGEST")}
ORDER BY DIGEST_DATE DESC
LIMIT 1
"""


def savings_verification_runs() -> str:
    return f"""
SELECT V.RUN_AT, V.ITEM_ID, V.WAREHOUSE_NAME, V.BASELINE_EST_USD,
       V.MEASURED_IDLE_USD_30D, V.PROPOSED_VERIFIED_USD, L.STATE
FROM {core_object("SAVINGS_VERIFICATION_RUNS")} V
LEFT JOIN {core_object("SAVINGS_LEDGER")} L ON L.ITEM_ID = V.ITEM_ID
QUALIFY ROW_NUMBER() OVER (PARTITION BY V.ITEM_ID ORDER BY V.RUN_AT DESC) = 1
ORDER BY V.PROPOSED_VERIFIED_USD DESC
LIMIT 200
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


def alert_routes() -> str:
    return f"""
SELECT ROUTE_ID, FAMILY, MIN_SEVERITY, INTEGRATION_NAME, ENABLED, CREATED_BY, CREATED_AT
FROM {core_object("ALERT_ROUTES")}
ORDER BY FAMILY, MIN_SEVERITY
"""


def remediation_log(limit: int = 100) -> str:
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD,
       EXECUTED_BY, EXECUTED_AT, STATUS, RESULT_NOTE
FROM {core_object("REMEDIATION_LOG")}
ORDER BY EXECUTED_AT DESC
LIMIT {limit}
"""


def health_strip() -> str:
    """Three always-on sidebar badges in one cached statement: open
    criticals, stalest telemetry source, month-to-date billed credits."""
    return f"""
SELECT 'OPEN_CRITICAL' AS METRIC, TO_VARCHAR(COUNT(*)) AS VALUE,
       IFF(COUNT(*) > 0, 'BAD', 'OK') AS STATE
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS = 'OPEN' AND SEVERITY = 'CRITICAL'
UNION ALL
SELECT 'STALEST_SOURCE_H', TO_VARCHAR(COALESCE(ROUND(MAX(HOURS_SINCE_LOAD), 1), -1)),
       CASE WHEN MAX(HOURS_SINCE_LOAD) IS NULL THEN 'MUTED'
            WHEN MAX(HOURS_SINCE_LOAD) > 26 THEN 'BAD'
            WHEN MAX(HOURS_SINCE_LOAD) > 3 THEN 'WARN'
            ELSE 'OK' END
FROM {mart_object("MART_SOURCE_FRESHNESS")}
UNION ALL
SELECT 'MTD_CREDITS', TO_VARCHAR(ROUND(COALESCE(SUM(CREDITS_BILLED), 0), 0)), 'INFO'
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
"""
