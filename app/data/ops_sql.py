"""Operations SQL builders: queries, tasks, warehouses, contention."""

from __future__ import annotations

from app import companies
from app.core.sqlsafe import contains_filter
from app.data.common import and_where, bounded_days


def _query_scope(days: int, company: str, warehouse_contains: str = "", user_contains: str = "",
                 database: str = "", schema_contains: str = "") -> str:
    return and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company),
        companies.user_clause(company),
        companies.database_equals_clause(database),
        contains_filter("WAREHOUSE_NAME", warehouse_contains),
        contains_filter("USER_NAME", user_contains),
        contains_filter("SCHEMA_NAME", schema_contains),
    )


def query_window_summary(days: int, company: str = "ALL", warehouse_contains: str = "", user_contains: str = "",
                         database: str = "", schema_contains: str = "") -> str:
    """One-row query health summary for the window."""
    days = bounded_days(days)
    return f"""
SELECT
    COUNT(*) AS QUERY_COUNT,
    SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)) AS FAILED_COUNT,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {_query_scope(days, company, warehouse_contains, user_contains, database, schema_contains)}
"""


def top_queries_by_elapsed(days: int, company: str = "ALL", limit: int = 50,
                           warehouse_contains: str = "", user_contains: str = "",
                           database: str = "", schema_contains: str = "") -> str:
    """Heaviest queries in the window (elapsed basis; cost labeled estimate)."""
    days = bounded_days(days)
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT
    QUERY_ID,
    USER_NAME,
    WAREHOUSE_NAME,
    WAREHOUSE_SIZE,
    DATABASE_NAME,
    QUERY_TYPE,
    EXECUTION_STATUS,
    TOTAL_ELAPSED_TIME / 1000.0 AS ELAPSED_SEC,
    (COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3) AS SPILL_REMOTE_GB,
    LEFT(QUERY_TEXT, 180) AS QUERY_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {_query_scope(days, company, warehouse_contains, user_contains, database, schema_contains)}
ORDER BY TOTAL_ELAPSED_TIME DESC
LIMIT {limit}
"""


def failures_by_error(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    """Failed queries grouped by error code/message family."""
    days = bounded_days(days)
    where = and_where(
        _query_scope(days, company, database=database, schema_contains=schema_contains),
        "EXECUTION_STATUS = 'FAIL'",
    )
    return f"""
SELECT
    COALESCE(ERROR_CODE::VARCHAR, 'UNKNOWN') AS ERROR_CODE,
    LEFT(COALESCE(ERROR_MESSAGE, 'Unknown error'), 140) AS ERROR_MESSAGE,
    COUNT(*) AS FAILURES,
    COUNT(DISTINCT USER_NAME) AS USERS_AFFECTED,
    MAX(START_TIME) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2
ORDER BY FAILURES DESC
LIMIT 50
"""


def task_runs(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    """Task run outcomes grouped by task, newest failures surfaced."""
    days = bounded_days(days)
    where = and_where(
        f"QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME AS TASK_NAME,
    COUNT(*) AS RUNS,
    SUM(IFF(STATE = 'FAILED', 1, 0)) AS FAILED,
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)) AS AVG_SEC,
    MAX(QUERY_START_TIME) AS LAST_RUN,
    MAX_BY(STATE, QUERY_START_TIME) AS LAST_STATE,
    MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 200), QUERY_START_TIME) AS LAST_ERROR
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY FAILED DESC, LAST_RUN DESC
LIMIT 200
"""


def warehouse_pressure(days: int, company: str = "ALL") -> str:
    """Queue and spill pressure per warehouse for the window."""
    days = bounded_days(days)
    return f"""
SELECT
    WAREHOUSE_NAME,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {and_where(f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())", "WAREHOUSE_NAME IS NOT NULL", companies.warehouse_clause(company))}
GROUP BY 1
HAVING SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) > 0
    OR SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) > 0
ORDER BY QUEUED_SEC DESC
LIMIT 50
"""


def lock_contention(days: int) -> str:
    """Lock waits (account-wide; LOCK_WAIT_HISTORY has no warehouse grain)."""
    days = bounded_days(days, maximum=14)
    return f"""
SELECT
    OBJECT_NAME,
    LOCK_TYPE,
    COUNT(*) AS WAIT_EVENTS,
    SUM(DATEDIFF('second', REQUESTED_AT, COALESCE(ACQUIRED_AT, REQUESTED_AT))) AS TOTAL_WAIT_SEC,
    MAX(REQUESTED_AT) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY
WHERE REQUESTED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2
ORDER BY TOTAL_WAIT_SEC DESC
LIMIT 50
"""


def poor_pruning_queries(days: int, company: str = "ALL", database: str = "",
                         schema_contains: str = "") -> str:
    """Query families scanning >80% of a 100+-partition table — missing
    clustering keys or unpruned predicates."""
    days = bounded_days(days)
    scope = _query_scope(days, company, "", "", database, schema_contains)
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT,
    COUNT(*) AS RUNS,
    ROUND(AVG(PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0)) * 100, 1) AS AVG_SCAN_PCT,
    ROUND(AVG(PARTITIONS_TOTAL), 0) AS AVG_PARTITIONS,
    ROUND(SUM(BYTES_SCANNED) / POWER(1024, 4), 3) AS TB_SCANNED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {scope}
  AND EXECUTION_STATUS = 'SUCCESS'
  AND PARTITIONS_TOTAL >= 100
  AND PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0) > 0.8
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY SUM(BYTES_SCANNED) DESC
LIMIT 25
"""


def result_cache_daily(days: int, company: str = "ALL") -> str:
    """Share of successful queries answered without scanning (result cache /
    metadata answers). A falling line means redundant recomputation."""
    days = bounded_days(days)
    scope = _query_scope(days, company, "", "", "", "")
    return f"""
SELECT
    DATE_TRUNC('day', START_TIME) AS DAY,
    COUNT(*) AS QUERIES,
    COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND COALESCE(BYTES_SCANNED, 0) = 0) AS ZERO_SCAN,
    ROUND(COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND COALESCE(BYTES_SCANNED, 0) = 0)
          / NULLIF(COUNT(*), 0) * 100, 1) AS HIT_PCT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {scope}
GROUP BY 1
ORDER BY 1
"""


def warehouse_concurrency_peaks(days: int, company: str = "ALL") -> str:
    """Peak running/queued load per warehouse — right-size multi-cluster
    BEFORE queuing hurts, not after."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    WAREHOUSE_NAME,
    ROUND(MAX(AVG_RUNNING), 1) AS PEAK_RUNNING,
    ROUND(MAX(AVG_QUEUED_LOAD), 1) AS PEAK_QUEUED,
    COUNT_IF(AVG_QUEUED_LOAD > 0.5) AS QUEUED_INTERVALS,
    COUNT(*) AS INTERVALS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY PEAK_QUEUED DESC, PEAK_RUNNING DESC
LIMIT 100
"""


def copy_load_failures(days: int, company: str = "ALL") -> str:
    """Failed / partial COPY and Snowpipe file loads by target table."""
    days = bounded_days(days)
    where = and_where(
        f"LAST_LOAD_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "STATUS IN ('Load failed', 'Partially loaded')",
        companies.database_clause(company, "TABLE_CATALOG_NAME"),
    )
    return f"""
SELECT
    TABLE_CATALOG_NAME AS DATABASE_NAME,
    TABLE_SCHEMA_NAME AS SCHEMA_NAME,
    TABLE_NAME,
    MAX(PIPE_NAME) AS PIPE_NAME,
    COUNT(*) AS FAILED_FILES,
    MAX(LAST_LOAD_TIME) AS LAST_FAILURE,
    LEFT(MAX(FIRST_ERROR_MESSAGE), 300) AS SAMPLE_ERROR,
    'FAILED' AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY FAILED_FILES DESC
LIMIT 100
"""
