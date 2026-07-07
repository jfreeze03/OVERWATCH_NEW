"""Insight builders ported from the original OVERWATCH (features 1-7).

Idle warehouse analysis, repeat-query fingerprints, storage growth, release
window compares, task failure detail, pipeline SLA readers, dormant users.
Pure SQL strings: bounded windows, company scoping, no dollar rates.
"""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.data.common import and_where, bounded_days


def _iso_date(value: str, name: str) -> str:
    text = str(value or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-" or not text.replace("-", "").isdigit():
        raise ValueError(f"{name} must be YYYY-MM-DD, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# 1. Idle warehouse analysis: metered warehouse-hours with zero query activity
# ---------------------------------------------------------------------------

def idle_warehouse_analysis(days: int, company: str = "ALL") -> str:
    """Per warehouse: total vs idle credits (hour slices with no queries).

    WAREHOUSE_METERING_HISTORY bills by hour slice; joining each slice to
    query activity in the same warehouse-hour isolates credits burned while
    nothing ran — the auto-suspend opportunity.
    """
    days = bounded_days(days)
    where = and_where(
        f"M.START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
    )
    return f"""
WITH query_hours AS (
    SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
)
SELECT
    M.WAREHOUSE_NAME,
    {companies.company_case_sql("M.WAREHOUSE_NAME")} AS COMPANY,
    COUNT(*) AS METERED_HOURS,
    SUM(IFF(Q.HOUR_TS IS NULL, 1, 0)) AS IDLE_HOURS,
    SUM(COALESCE(M.CREDITS_USED, 0)) AS TOTAL_CREDITS,
    SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
LEFT JOIN query_hours Q
       ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
      AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
WHERE {where}
GROUP BY 1, 2
HAVING SUM(COALESCE(M.CREDITS_USED, 0)) > 0
ORDER BY IDLE_CREDITS DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 2. Repeat-query fingerprints (cache/materialization candidates)
# ---------------------------------------------------------------------------

def repeat_query_fingerprints(days: int, company: str = "ALL", min_runs: int = 10,
                              database: str = "", schema_contains: str = "") -> str:
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    min_runs = max(2, min(int(min_runs), 1000))
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        "QUERY_TYPE = 'SELECT'",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
        companies.warehouse_clause(company),
        companies.user_clause(company),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH AS FINGERPRINT,
    COUNT(*) AS RUNS,
    COUNT(DISTINCT USER_NAME) AS USERS,
    COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 3600000.0 AS TOTAL_ELAPSED_HOURS,
    AVG(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS AVG_ELAPSED_SEC,
    SUM(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 4) AS TOTAL_TB_SCANNED,
    AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)) AS AVG_CACHE_PCT,
    ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS QUERY_PREVIEW,
    MAX(START_TIME) AS LAST_RUN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY QUERY_PARAMETERIZED_HASH
HAVING COUNT(*) >= {min_runs}
ORDER BY TOTAL_ELAPSED_HOURS DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 3. Storage growth movers
# ---------------------------------------------------------------------------

def storage_growth_by_database(days: int, company: str = "ALL") -> str:
    days = bounded_days(days)
    where = and_where(
        f"USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
    )
    return f"""
WITH daily AS (
    SELECT
        DATABASE_NAME,
        USAGE_DATE,
        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE {where}
    GROUP BY 1, 2
)
SELECT
    DATABASE_NAME,
    {companies.company_case_sql("DATABASE_NAME")} AS COMPANY,
    MIN(USAGE_DATE) AS FIRST_DAY,
    MAX(USAGE_DATE) AS LAST_DAY,
    MIN_BY(DB_BYTES, USAGE_DATE) AS FIRST_BYTES,
    MAX_BY(DB_BYTES, USAGE_DATE) AS LAST_BYTES,
    MAX_BY(FAILSAFE_BYTES, USAGE_DATE) AS FAILSAFE_BYTES,
    DATEDIFF('day', MIN(USAGE_DATE), MAX(USAGE_DATE)) AS SPAN_DAYS
FROM daily
GROUP BY 1, 2
HAVING MAX_BY(DB_BYTES, USAGE_DATE) > 0 OR MIN_BY(DB_BYTES, USAGE_DATE) > 0
ORDER BY (MAX_BY(DB_BYTES, USAGE_DATE) - MIN_BY(DB_BYTES, USAGE_DATE)) DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 4. Release window compare (before vs after a deploy date)
# ---------------------------------------------------------------------------

def release_query_compare(release_date: str, window_days: int, company: str = "ALL") -> str:
    """Overall query health in the N days before vs after a release date."""
    release = _iso_date(release_date, "release_date")
    window = max(1, min(int(window_days), 14))
    where = and_where(
        f"START_TIME >= DATEADD('day', -{window}, DATE '{release}')",
        f"START_TIME < DATEADD('day', {window}, DATE '{release}')",
        companies.warehouse_clause(company),
        companies.user_clause(company),
    )
    return f"""
SELECT
    IFF(START_TIME < DATE '{release}', 'BEFORE', 'AFTER') AS PERIOD,
    COUNT(*) AS QUERY_COUNT,
    SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)) AS FAILED_COUNT,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
"""


def release_task_compare(release_date: str, window_days: int, company: str = "ALL") -> str:
    """Per-task runs/failures/runtime before vs after a release date."""
    release = _iso_date(release_date, "release_date")
    window = max(1, min(int(window_days), 14))
    where = and_where(
        f"QUERY_START_TIME >= DATEADD('day', -{window}, DATE '{release}')",
        f"QUERY_START_TIME < DATEADD('day', {window}, DATE '{release}')",
        companies.database_clause(company, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATABASE_NAME,
    NAME AS TASK_NAME,
    IFF(QUERY_START_TIME < DATE '{release}', 'BEFORE', 'AFTER') AS PERIOD,
    COUNT(*) AS RUNS,
    SUM(IFF(STATE = 'FAILED', 1, 0)) AS FAILED,
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)) AS AVG_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY DATABASE_NAME, TASK_NAME, PERIOD
LIMIT 1000
"""


# ---------------------------------------------------------------------------
# 5. Task failure detail (root-cause timeline)
# ---------------------------------------------------------------------------

def task_failure_details(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days, maximum=14)
    where = and_where(
        f"QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "STATE = 'FAILED'",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME AS TASK_NAME,
    ROOT_TASK_ID,
    GRAPH_RUN_GROUP_ID,
    QUERY_START_TIME,
    DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SEC,
    COALESCE(ERROR_CODE::VARCHAR, '') AS ERROR_CODE,
    LEFT(COALESCE(ERROR_MESSAGE, ''), 300) AS ERROR_MESSAGE
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE {where}
ORDER BY QUERY_START_TIME DESC
LIMIT 500
"""


# ---------------------------------------------------------------------------
# Warehouse sizing profile (credits + load stats + idle share per warehouse)
# ---------------------------------------------------------------------------

def warehouse_sizing_profile(days: int, company: str = "ALL") -> str:
    days = bounded_days(days)
    where_m = and_where(
        f"M.START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
    )
    return f"""
WITH query_stats AS (
    SELECT
        WAREHOUSE_NAME,
        COUNT(*) AS QUERY_COUNT,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
    GROUP BY WAREHOUSE_NAME
),
query_hours AS (
    SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
)
SELECT
    M.WAREHOUSE_NAME,
    {companies.company_case_sql("M.WAREHOUSE_NAME")} AS COMPANY,
    SUM(COALESCE(M.CREDITS_USED, 0)) AS CREDITS_TOTAL,
    ROUND(SUM(IFF(H.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0))
          / NULLIF(SUM(COALESCE(M.CREDITS_USED, 0)), 0) * 100, 1) AS IDLE_PCT,
    COALESCE(MAX(Q.QUERY_COUNT), 0) AS QUERY_COUNT,
    COALESCE(MAX(Q.P95_ELAPSED_SEC), 0) AS P95_ELAPSED_SEC,
    COALESCE(MAX(Q.QUEUED_SEC), 0) AS QUEUED_SEC,
    COALESCE(MAX(Q.SPILL_REMOTE_GB), 0) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
LEFT JOIN query_hours H
       ON H.WAREHOUSE_NAME = M.WAREHOUSE_NAME
      AND H.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
LEFT JOIN query_stats Q ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
WHERE {where_m}
GROUP BY 1, 2
HAVING SUM(COALESCE(M.CREDITS_USED, 0)) > 0
ORDER BY CREDITS_TOTAL DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# Query drill-through
# ---------------------------------------------------------------------------

_QUERY_ID_RE_HINT = "Snowflake query IDs are UUID-like hex strings"


def query_detail(query_id: str) -> str:
    """Full detail for one query; the ID is validated before embedding."""
    import re as _re

    qid = str(query_id or "").strip()
    if not _re.match(r"^[0-9a-fA-F-]{16,64}$", qid):
        raise ValueError(f"Invalid query id ({_QUERY_ID_RE_HINT}): {qid!r}")
    from app.core.sqlsafe import sql_literal as _lit

    return f"""
SELECT
    QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE,
    DATABASE_NAME, SCHEMA_NAME, QUERY_TYPE, EXECUTION_STATUS,
    ERROR_CODE, ERROR_MESSAGE,
    START_TIME, END_TIME,
    TOTAL_ELAPSED_TIME / 1000.0 AS ELAPSED_SEC,
    COMPILATION_TIME / 1000.0 AS COMPILE_SEC,
    EXECUTION_TIME / 1000.0 AS EXECUTION_SEC,
    (COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    BYTES_SCANNED / POWER(1024, 3) AS GB_SCANNED,
    PERCENTAGE_SCANNED_FROM_CACHE AS CACHE_PCT,
    COALESCE(BYTES_SPILLED_TO_LOCAL_STORAGE, 0) / POWER(1024, 3) AS LOCAL_SPILL_GB,
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3) AS REMOTE_SPILL_GB,
    ROWS_PRODUCED,
    PARTITIONS_SCANNED, PARTITIONS_TOTAL,
    QUERY_TEXT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_ID = {_lit(qid)}
LIMIT 1
"""


# ---------------------------------------------------------------------------
# 6. Pipeline SLA (config in V006; status computed against table freshness)
# ---------------------------------------------------------------------------

def pipeline_sla_status() -> str:
    return f"""
SELECT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, OWNER, MAX_AGE_HOURS,
       LAST_ALTERED, HOURS_SINCE, SLA_MET
FROM {core_object("PIPELINE_SLA_STATUS")}
ORDER BY SLA_MET, HOURS_SINCE DESC
"""


def pipeline_sla_config() -> str:
    return f"""
SELECT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, MAX_AGE_HOURS, OWNER, ENABLED, UPDATED_AT
FROM {core_object("PIPELINE_SLA_CONFIG")}
ORDER BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME
"""


# ---------------------------------------------------------------------------
# 7. Dormant users & grant review
# ---------------------------------------------------------------------------

def dormant_users(dormant_days: int = 90, company: str = "ALL") -> str:
    dormant_days = max(30, min(int(dormant_days), 365))
    where = and_where(
        "U.DELETED_ON IS NULL",
        "COALESCE(U.DISABLED, FALSE) = FALSE",
        f"(U.LAST_SUCCESS_LOGIN IS NULL OR U.LAST_SUCCESS_LOGIN < DATEADD('day', -{dormant_days}, CURRENT_TIMESTAMP()))",
        f"U.CREATED_ON < DATEADD('day', -{dormant_days}, CURRENT_TIMESTAMP())",
        companies.user_clause(company, "U.NAME"),
    )
    return f"""
WITH role_counts AS (
    SELECT GRANTEE_NAME, COUNT(*) AS ROLE_COUNT,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
    GROUP BY GRANTEE_NAME
)
SELECT
    U.NAME AS USER_NAME,
    U.EMAIL,
    U.CREATED_ON,
    U.LAST_SUCCESS_LOGIN,
    COALESCE(DATEDIFF('day', U.LAST_SUCCESS_LOGIN, CURRENT_TIMESTAMP()), 9999) AS DAYS_DORMANT,
    COALESCE(R.ROLE_COUNT, 0) AS ROLE_COUNT,
    LEFT(COALESCE(R.ROLES, ''), 300) AS ROLES
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
LEFT JOIN role_counts R ON R.GRANTEE_NAME = U.NAME
WHERE {where}
ORDER BY DAYS_DORMANT DESC, ROLE_COUNT DESC
LIMIT 300
"""


def storage_waste(company: str = "ALL", min_gb: float = 1.0) -> str:
    """Tables carrying heavy Time-Travel/failsafe bytes, flagged STALE when
    no DML touched them in 90 days — retention money for nothing."""
    min_bytes = int(max(0.1, float(min_gb)) * 1024 ** 3)
    where = and_where(
        "m.DELETED = FALSE",
        f"m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES >= {min_bytes}",
        companies.database_clause(company, "m.TABLE_CATALOG"),
    )
    return f"""
SELECT
    m.TABLE_CATALOG AS DATABASE_NAME,
    m.TABLE_SCHEMA AS SCHEMA_NAME,
    m.TABLE_NAME,
    ROUND(m.ACTIVE_BYTES / POWER(1024, 3), 2) AS ACTIVE_GB,
    ROUND(m.TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(m.FAILSAFE_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
    d.LAST_DML,
    IFF(d.LAST_DML IS NULL, 'STALE', 'ACTIVE') AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
LEFT JOIN (
    SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1
) d ON d.TABLE_ID = m.ID
WHERE {where}
ORDER BY m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES DESC
LIMIT 50
"""


def warehouse_hourly_activity(days: int, company: str = "ALL") -> str:
    """Hour-of-day credits vs query activity per warehouse — the input to
    the off-hours schedule advisor (credits with no queries = waste)."""
    days = bounded_days(days, 30)
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH m AS (
    SELECT WAREHOUSE_NAME, HOUR(START_TIME) AS HR,
           SUM(CREDITS_USED) AS CR,
           COUNT(DISTINCT DATE(START_TIME)) AS DAYS_SEEN
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
q AS (
    SELECT WAREHOUSE_NAME, HOUR(HOUR_TS) AS HR, SUM(QUERY_COUNT) AS QC
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
    GROUP BY 1, 2
)
SELECT m.WAREHOUSE_NAME, m.HR AS HOUR_OF_DAY,
       ROUND(m.CR / NULLIF(m.DAYS_SEEN, 0), 3) AS AVG_CREDITS,
       ROUND(COALESCE(q.QC, 0) / NULLIF(m.DAYS_SEEN, 0), 1) AS AVG_QUERIES
FROM m
LEFT JOIN q ON q.WAREHOUSE_NAME = m.WAREHOUSE_NAME AND q.HR = m.HR
ORDER BY m.WAREHOUSE_NAME, m.HR
"""


def anomaly_evidence(event_date: str, warehouse_contains: str = "") -> str:
    """Evidence pack for one anomalous day: which query families' elapsed
    hours moved vs their prior-7-day average. Feeds the grounded AI
    explanation — never shown as fact, always as ranked evidence."""
    import datetime as _dt

    from app.core.sqlsafe import contains_filter

    day = _dt.date.fromisoformat(str(event_date).strip()).isoformat()  # validates
    where = and_where(
        f"START_TIME >= DATEADD('day', -7, DATE '{day}')",
        f"START_TIME < DATEADD('day', 1, DATE '{day}')",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        contains_filter("WAREHOUSE_NAME", warehouse_contains),
    )
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
    ANY_VALUE(WAREHOUSE_NAME) AS WAREHOUSE_NAME,
    COUNT_IF(DATE(START_TIME) = DATE '{day}') AS RUNS_DAY,
    ROUND(SUM(IFF(DATE(START_TIME) = DATE '{day}', TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS ELAPSED_H_DAY,
    ROUND(SUM(IFF(DATE(START_TIME) < DATE '{day}', TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS ELAPSED_H_PRIOR_AVG
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
HAVING ELAPSED_H_DAY > 0
ORDER BY ELAPSED_H_DAY - ELAPSED_H_PRIOR_AVG DESC
LIMIT 15
"""
