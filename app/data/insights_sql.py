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
  AND M.WAREHOUSE_ID > 0
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
    {companies.database_case_sql("DATABASE_NAME")} AS COMPANY,
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
WHERE M.WAREHOUSE_ID > 0
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
      AND WAREHOUSE_ID > 0
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


def expensive_queries_usd(days: int, company: str = "ALL", limit: int = 50,
                          database: str = "") -> str:
    """Top queries by ALLOCATED credits — warehouse-hour credits split across
    that hour's queries by execution-time share.

    Labeled *allocated*, not billed: Snowflake bills the warehouse, not the
    query. Bucketing is by the query's start hour (a long query's later hours
    are attributed to its first — documented approximation). Idle credits in
    an hour with queries are carried pro-rata; fully idle hours are excluded
    (the idle advisor owns those).
    """
    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where_q = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        companies.warehouse_clause(company),
        companies.database_equals_clause(database),
    )
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH q AS (
    SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, QUERY_TYPE, EXECUTION_STATUS,
           START_TIME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
           COALESCE(EXECUTION_TIME, 0) AS EXEC_MS,
           COALESCE(TOTAL_ELAPSED_TIME, 0) / 1000.0 AS ELAPSED_SEC,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where_q}
),
m AS (
    SELECT WAREHOUSE_NAME, START_TIME AS HOUR_TS, SUM(CREDITS_USED) AS HOUR_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
t AS (
    SELECT WAREHOUSE_NAME, HOUR_TS, SUM(EXEC_MS) AS TOTAL_EXEC_MS
    FROM q GROUP BY 1, 2
)
SELECT
    q.QUERY_ID,
    MAX(q.USER_NAME)        AS USER_NAME,
    MAX(q.WAREHOUSE_NAME)   AS WAREHOUSE_NAME,
    MAX(q.QUERY_TYPE)       AS QUERY_TYPE,
    MAX(q.EXECUTION_STATUS) AS EXECUTION_STATUS,
    MIN(q.START_TIME)       AS START_TIME,
    MAX(q.ELAPSED_SEC)      AS ELAPSED_SEC,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) AS ALLOCATED_CREDITS,
    MAX(q.QUERY_SNIPPET)    AS QUERY_SNIPPET
FROM q
JOIN t ON t.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND t.HOUR_TS = q.HOUR_TS
JOIN m ON m.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND m.HOUR_TS = q.HOUR_TS
GROUP BY q.QUERY_ID
HAVING ALLOCATED_CREDITS > 0
ORDER BY ALLOCATED_CREDITS DESC
LIMIT {limit}
"""


def storage_reclaim(company: str = "ALL", min_gb: float = 1.0, read_days: int = 90) -> str:
    """storage_waste + read evidence: LAST_READ from ACCESS_HISTORY so a table
    can be STALE (no DML) *and* NEVER_READ — the safe-to-archive shortlist.

    ACCESS_HISTORY needs Enterprise edition; the page degrades to
    storage_waste() when this errors. Deliberately NOT in the canary registry:
    on Standard edition it would be a permanently red row (alert noise), and
    the panel already labels the degraded state.
    """
    read_days = bounded_days(read_days, 90)
    min_bytes = int(max(0.1, float(min_gb)) * 1024 ** 3)
    where = and_where(
        "m.DELETED = FALSE",
        f"m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES >= {min_bytes}",
        companies.database_clause(company, "m.TABLE_CATALOG"),
    )
    return f"""
WITH reads AS (
    SELECT f.value:"objectName"::STRING AS FQN, MAX(a.QUERY_START_TIME) AS LAST_READ
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{read_days}, CURRENT_TIMESTAMP())
      AND f.value:"objectDomain"::STRING = 'Table'
    GROUP BY 1
)
SELECT
    m.TABLE_CATALOG AS DATABASE_NAME,
    m.TABLE_SCHEMA  AS SCHEMA_NAME,
    m.TABLE_NAME,
    ROUND(m.ACTIVE_BYTES / POWER(1024, 3), 2)      AS ACTIVE_GB,
    ROUND(m.TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(m.FAILSAFE_BYTES / POWER(1024, 3), 2)    AS FAILSAFE_GB,
    ROUND(m.RETAINED_FOR_CLONE_BYTES / POWER(1024, 3), 2) AS CLONE_RETAINED_GB,
    d.LAST_DML,
    r.LAST_READ,
    IFF(d.LAST_DML IS NULL, 'STALE', 'ACTIVE') AS DML_STATUS,
    IFF(r.LAST_READ IS NULL, TRUE, FALSE)      AS NEVER_READ
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
LEFT JOIN (
    SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1
) d ON d.TABLE_ID = m.ID
LEFT JOIN reads r
       ON r.FQN = m.TABLE_CATALOG || '.' || m.TABLE_SCHEMA || '.' || m.TABLE_NAME
WHERE {where}
ORDER BY (m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES + m.RETAINED_FOR_CLONE_BYTES) DESC
LIMIT 50
"""

def expensive_patterns_usd(days: int, company: str = "ALL", limit: int = 30) -> str:
    """Recurring cost patterns: the SAME hour-share allocation as
    expensive_queries_usd, grouped by QUERY_PARAMETERIZED_HASH.

    One $9 query run 400x/day outranks a single $300 one — this is where
    caching/materialization actually pays. USD_PER_DAY = allocated / window.
    """
    days = bounded_days(days)
    limit = max(5, min(int(limit or 30), 100))
    where_q = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        companies.warehouse_clause(company),
    )
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH q AS (
    SELECT QUERY_PARAMETERIZED_HASH AS PATTERN_HASH,
           QUERY_ID, USER_NAME, WAREHOUSE_NAME,
           DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
           COALESCE(EXECUTION_TIME, 0) AS EXEC_MS,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where_q}
),
m AS (
    SELECT WAREHOUSE_NAME, START_TIME AS HOUR_TS, SUM(CREDITS_USED) AS HOUR_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
t AS (
    SELECT WAREHOUSE_NAME, HOUR_TS, SUM(EXEC_MS) AS TOTAL_EXEC_MS
    FROM q GROUP BY 1, 2
)
SELECT
    q.PATTERN_HASH,
    COUNT(DISTINCT q.QUERY_ID)  AS RUNS,
    COUNT(DISTINCT q.USER_NAME) AS USERS,
    COUNT(DISTINCT q.WAREHOUSE_NAME) AS WAREHOUSES,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) AS ALLOCATED_CREDITS,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) / {days} AS CREDITS_PER_DAY,
    ANY_VALUE(q.QUERY_SNIPPET)  AS QUERY_SNIPPET
FROM q
JOIN t ON t.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND t.HOUR_TS = q.HOUR_TS
JOIN m ON m.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND m.HOUR_TS = q.HOUR_TS
GROUP BY q.PATTERN_HASH
HAVING RUNS >= 5 AND ALLOCATED_CREDITS > 0
ORDER BY ALLOCATED_CREDITS DESC
LIMIT {limit}
"""

def table_tco(database: str, schema: str, table: str, days: int = 30) -> str:
    """Object-level cost evidence: reads, writers, last touch from
    ACCESS_HISTORY for ONE table (Enterprise edition; the page degrades).
    Storage dollars come from the reclaim row the caller already has."""
    from app.core.sqlsafe import safe_identifier

    fqn = ".".join(safe_identifier(part) for part in (database, schema, table)).upper()
    days = bounded_days(days, 90)
    return f"""
WITH touches AS (
    SELECT a.QUERY_START_TIME, a.USER_NAME,
           f.value:"objectName"::STRING AS FQN, 'READ' AS KIND
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectName"::STRING = '{fqn}'
    UNION ALL
    SELECT a.QUERY_START_TIME, a.USER_NAME,
           f.value:"objectName"::STRING, 'WRITE'
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.OBJECTS_MODIFIED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectName"::STRING = '{fqn}'
)
SELECT KIND,
       COUNT(*)                  AS TOUCHES,
       COUNT(DISTINCT USER_NAME) AS DISTINCT_USERS,
       MAX(QUERY_START_TIME)     AS LAST_TOUCH,
       ANY_VALUE(USER_NAME)      AS SAMPLE_USER
FROM touches
GROUP BY KIND
"""


def measured_query_costs(days: int, company: str = "ALL", database: str = "",
                         schema_contains: str = "", warehouse_contains: str = "",
                         user_contains: str = "", limit: int = 50) -> str:
    """Top queries by MEASURED compute credits (QUERY_ATTRIBUTION_HISTORY).

    Attribution excludes warehouse idle time: this answers "what did running
    THIS query cost" — the complement of expensive_queries_usd's allocated
    lens (which spreads the whole warehouse-hour bill, idle included).
    ~6h view lag; rows without attributed credits are omitted.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
        contains_filter("q.WAREHOUSE_NAME", warehouse_contains),
        contains_filter("q.USER_NAME", user_contains),
    )
    return f"""
WITH q AS (
    SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, DATABASE_NAME, SCHEMA_NAME,
           QUERY_TYPE, EXECUTION_STATUS, START_TIME,
           COALESCE(TOTAL_ELAPSED_TIME, 0) AS ELAPSED_MS,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
)
-- Join the FILTERED window first, THEN aggregate credits: pre-aggregating
-- the entire attribution view was the 139s-family pattern (Codex #11 —
-- the same fix the graph and procedure builders already got).
SELECT
    q.QUERY_ID, q.USER_NAME, q.WAREHOUSE_NAME, q.DATABASE_NAME, q.SCHEMA_NAME,
    q.QUERY_TYPE, q.EXECUTION_STATUS, q.START_TIME,
    ROUND(q.ELAPSED_MS / 1000.0, 1) AS ELAPSED_SEC,
    ROUND(SUM(a.CREDITS_ATTRIBUTED_COMPUTE), 6) AS CREDITS,
    q.QUERY_SNIPPET
FROM q
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
  ON a.QUERY_ID = q.QUERY_ID
 AND a.START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
GROUP BY q.QUERY_ID, q.USER_NAME, q.WAREHOUSE_NAME, q.DATABASE_NAME, q.SCHEMA_NAME,
         q.QUERY_TYPE, q.EXECUTION_STATUS, q.START_TIME, q.ELAPSED_MS, q.QUERY_SNIPPET
HAVING SUM(a.CREDITS_ATTRIBUTED_COMPUTE) > 0
ORDER BY CREDITS DESC
LIMIT {limit}
"""


def procedure_costs_usd(days: int, company: str = "ALL", database: str = "",
                        schema_contains: str = "", limit: int = 50) -> str:
    """$/call leaderboard for EVERY stored procedure (measured credits).

    Child statements roll up to the CALL via ROOT_QUERY_ID, so a procedure's
    cost includes everything it ran. DATABASE/SCHEMA are the CALL's session
    context (the proc may read other databases — labeled in the UI).
    Complements change-impact, which prices only changed objects.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where = and_where(
        f"c.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "c.QUERY_TYPE = 'CALL'",
        companies.warehouse_clause(company, "c.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "c.DATABASE_NAME"),
        contains_filter("c.SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH att AS (
    SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
           SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
      -- Prune before the GROUP BY: only rows rolling up to a CALL matter
      -- (children carry the CALL's id as ROOT_QUERY_ID). Perf pass #9.
      AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
          SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
          WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
            AND QUERY_TYPE = 'CALL'
      )
    GROUP BY 1
),
calls AS (
    SELECT c.QUERY_ID, c.DATABASE_NAME, c.SCHEMA_NAME, c.EXECUTION_STATUS,
           COALESCE(c.TOTAL_ELAPSED_TIME, 0) AS ELAPSED_MS,
           REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY c
    WHERE {where}
)
SELECT
    calls.PROC_NAME, calls.DATABASE_NAME, calls.SCHEMA_NAME,
    COUNT(*) AS CALLS,
    ROUND(100 * COUNT_IF(calls.EXECUTION_STATUS <> 'SUCCESS') / COUNT(*), 1) AS FAIL_PCT,
    ROUND(APPROX_PERCENTILE(calls.ELAPSED_MS, 0.95) / 1000, 1) AS P95_S,
    ROUND(SUM(COALESCE(att.CREDITS, 0)), 4) AS TOTAL_CREDITS,
    ROUND(SUM(COALESCE(att.CREDITS, 0)) / COUNT(*), 6) AS CREDITS_PER_CALL,
    COUNT(att.CREDITS) AS ATTRIBUTED_CALLS
FROM calls
LEFT JOIN att ON att.RID = calls.QUERY_ID
WHERE calls.PROC_NAME IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY TOTAL_CREDITS DESC
LIMIT {limit}
"""


def call_cost_lookup(ident: str, days: int = 7) -> str:
    """Measured $ per stored-proc CALL: pass a CALL's QUERY_ID or a
    SESSION_ID (owner question 2026-07-10: 'three procs in one session, no
    graph id'). No task graph needed — children roll up to their CALL via
    QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID for ad-hoc sessions too.
    ~6h attribution lag; idle time excluded; children that ran without a
    warehouse don't appear (same caveats as the proc leaderboard)."""
    from app.core.sqlsafe import sql_literal
    days = bounded_days(days, 30)
    lit = sql_literal(str(ident or "").strip())
    return f"""
WITH calls AS (
    SELECT QUERY_ID, SESSION_ID, START_TIME, LEFT(QUERY_TEXT, 80) AS CALL_TEXT
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND QUERY_TYPE = 'CALL'
      AND (QUERY_ID = {lit} OR SESSION_ID::VARCHAR = {lit})
)
SELECT c.QUERY_ID, c.START_TIME, c.CALL_TEXT,
       COUNT(a.QUERY_ID) AS ATTRIBUTED_QUERIES,
       ROUND(SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0)), 6) AS CREDITS
FROM calls c
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
       ON a.ROOT_QUERY_ID = c.QUERY_ID OR a.QUERY_ID = c.QUERY_ID
GROUP BY c.QUERY_ID, c.START_TIME, c.CALL_TEXT
ORDER BY c.START_TIME
LIMIT 100
"""


def call_children_costs(call_query_id: str, days: int = 7) -> str:
    """The child breakdown for ONE CALL: every attributed statement under
    ROOT_QUERY_ID with its own credits — 'where inside the proc did the
    money go'. The CALL's own row is included and labeled."""
    from app.core.sqlsafe import sql_literal
    days = bounded_days(days, 30)
    lit = sql_literal(str(call_query_id or "").strip())
    return f"""
SELECT a.QUERY_ID,
       IFF(a.QUERY_ID = {lit}, 'CALL (own time)', COALESCE(q.QUERY_TYPE, '?')) AS STEP_TYPE,
       LEFT(COALESCE(q.QUERY_TEXT, '(history pruned)'), 120) AS STEP_PREVIEW,
       ROUND(COALESCE(q.TOTAL_ELAPSED_TIME, 0) / 1000.0, 1) AS ELAPSED_SEC,
       ROUND(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0), 6) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
       ON q.QUERY_ID = a.QUERY_ID
      AND q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
WHERE a.ROOT_QUERY_ID = {lit} OR a.QUERY_ID = {lit}
ORDER BY CREDITS DESC
LIMIT 500
"""


def proc_cost_trend(proc_name: str, days: int, company: str = "ALL",
                    database: str = "", schema_contains: str = "") -> str:
    """Daily measured $ for ONE named procedure (owner ask 2026-07-11:
    "can I enter it myself" — yes, this is the type-the-name panel).

    Same extraction and ROOT_QUERY_ID rollup as the $/call leaderboard, so
    the two always agree; a bare name matches qualified CALLs via the
    suffix arm. Attribution lags ~6h; idle excluded; DATABASE/SCHEMA are
    the CALL's session context. POSIX classes only.
    """
    from app.core.sqlsafe import contains_filter, sql_literal

    days = bounded_days(days)
    name = str(proc_name or "").strip().upper().rstrip("(")
    lit = sql_literal(name)
    suffix = sql_literal("%." + name)
    where = and_where(
        f"c.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "c.QUERY_TYPE = 'CALL'",
        companies.warehouse_clause(company, "c.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "c.DATABASE_NAME"),
        contains_filter("c.SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH calls AS (
    SELECT c.QUERY_ID, DATE(c.START_TIME) AS DAY, c.EXECUTION_STATUS,
           REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY c
    WHERE {where}
),
named AS (
    SELECT * FROM calls
    WHERE PROC_NAME = {lit} OR PROC_NAME LIKE {suffix}
),
att AS (
    SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
           SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
      AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (SELECT QUERY_ID FROM named)
    GROUP BY 1
)
SELECT n.DAY,
       COUNT(*) AS CALLS,
       COUNT_IF(n.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
       ROUND(SUM(COALESCE(a.CREDITS, 0)), 6) AS CREDITS,
       ROUND(SUM(COALESCE(a.CREDITS, 0)) / NULLIF(COUNT(*), 0), 6) AS CREDITS_PER_CALL,
       COUNT(a.CREDITS) AS ATTRIBUTED_CALLS
FROM named n
LEFT JOIN att a ON a.RID = n.QUERY_ID
GROUP BY n.DAY
ORDER BY n.DAY
LIMIT 400
"""


def clustering_by_table(days: int = 30, company: str = "ALL") -> str:
    """Automatic-clustering spend per table (COST_DB recon R7) — serverless
    reclustering credits are the classic silent burner; a table rewriting
    itself daily shows up here long before anyone looks for it."""
    days = bounded_days(days, 90)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "CREDITS_USED > 0",
        companies.database_clause(company, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TABLE_NAME AS TABLE_FQN,
    ROUND(SUM(CREDITS_USED), 4) AS CREDITS,
    ROUND(SUM(COALESCE(NUM_BYTES_RECLUSTERED, 0)) / POWER(1024, 4), 3) AS TB_RECLUSTERED,
    COUNT(*) AS RECLUSTER_RUNS
FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY CREDITS DESC
LIMIT 25
"""
