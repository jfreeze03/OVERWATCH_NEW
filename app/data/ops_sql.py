"""Operations SQL builders: queries, tasks, warehouses, contention."""

from __future__ import annotations

from app import companies
from app.core.sqlsafe import contains_filter, sql_literal
from app.data.common import and_where, bounded_days


def _query_scope(days: int, company: str, warehouse_contains: str = "", user_contains: str = "",
                 database: str = "", schema_contains: str = "") -> str:
    # C10: scope QUERY_HISTORY by WAREHOUSE company only — COMPANY_FOR_WAREHOUSE, the
    # exact stamp the FACT_QUERY_HOURLY loader and the Cost pages use — NOT warehouse
    # AND user. The old intersection dropped all cross-company activity (a Trexis user
    # on an ALFA warehouse appeared only under ALL, and ALFA+Trexis+UNKNOWN did not
    # partition ALL) and could never reconcile against warehouse-grain spend. The
    # user_contains free-text filter still applies within the scope.
    comp = str(company or "ALL")
    company_scope = ("" if comp.upper() == "ALL"
                     else f"{companies.company_case_sql('WAREHOUSE_NAME')} = {sql_literal(comp)}")
    return and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        company_scope,
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
    SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
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
    START_TIME,
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
        "EXECUTION_STATUS <> 'SUCCESS'",
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
    """Lock waits (account-wide; LOCK_WAIT_HISTORY has no warehouse grain).
    Window capped at 7d (was 14): the 14-day scan read ~56GB per run on this
    account (fleet board, 2026-07-10) and lock triage is a this-week
    question — history beyond that lives in the incident timeline."""
    days = bounded_days(days, maximum=7)
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    OBJECT_NAME,
    LOCK_TYPE,
    COUNT(*) AS WAIT_EVENTS,
    -- COALESCE(...REQUESTED_AT) zeroed the WORST cases: locks never acquired
    -- (statement aborted/timed out) counted as zero wait (review #12). They
    -- are split out and ranked first instead.
    SUM(IFF(ACQUIRED_AT IS NOT NULL,
            DATEDIFF('second', REQUESTED_AT, ACQUIRED_AT), 0)) AS ACQUIRED_WAIT_SEC,
    COUNT_IF(ACQUIRED_AT IS NULL) AS NEVER_ACQUIRED,
    MAX(REQUESTED_AT) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY
WHERE REQUESTED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2, 3, 4
ORDER BY NEVER_ACQUIRED DESC, ACQUIRED_WAIT_SEC DESC
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
    -- "share of SUCCESSFUL queries served zero-scan" (docstring): denominator is
    -- successful queries, not all queries — else a spike in failures dilutes the
    -- hit rate even though cache behavior did not change.
    ROUND(COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND COALESCE(BYTES_SCANNED, 0) = 0)
          / NULLIF(COUNT_IF(EXECUTION_STATUS = 'SUCCESS'), 0) * 100, 1) AS HIT_PCT
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


def dynamic_table_health(days: int) -> str:
    """Refresh outcomes per dynamic table; failures mean downstream tables
    are silently serving stale data."""
    days = bounded_days(days, 14)
    return f"""
SELECT
    DATABASE_NAME, SCHEMA_NAME, NAME,
    COUNT(*) AS REFRESHES,
    COUNT_IF(STATE = 'FAILED') AS FAILURES,
    MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE,
    MAX(REFRESH_END_TIME) AS LAST_REFRESH,
    IFF(COUNT_IF(STATE = 'FAILED') > 0, 'FAILED', 'SUCCEEDED') AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
WHERE REFRESH_END_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY FAILURES DESC, LAST_REFRESH DESC
LIMIT 200
"""


def show_streams_sql() -> str:
    """SHOW-based (no ACCOUNT_USAGE view exists for stream staleness).
    LIMIT keeps the runtime row-cap rewrite from touching a SHOW command."""
    return "SHOW STREAMS IN ACCOUNT LIMIT 200"


def running_queries(warehouse: str) -> str:
    """Live in-flight statements (INFORMATION_SCHEMA function — real time,
    unlike the lagged ACCOUNT_USAGE view). Feeds the kill-switch panel.

    BY_WAREHOUSE variant (live error 2026-07-11, code 090234): the no-arg
    QUERY_HISTORY() defaults to CURRENT-USER scoping, and owner's-rights
    procedures — which is what Streamlit-in-Snowflake runs as — cannot
    access current-user information. Per-warehouse also matches the
    kill-switch flow: you already know which warehouse is burning."""
    from app.core.sqlsafe import sql_literal
    wh = str(warehouse or "").strip()
    if not wh:
        raise ValueError("running_queries needs a warehouse name")
    return f"""
SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, EXECUTION_STATUS,
       START_TIME, ROUND(TOTAL_ELAPSED_TIME / 1000, 1) AS ELAPSED_S,
       LEFT(QUERY_TEXT, 90) AS QUERY_PREVIEW
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE(
    WAREHOUSE_NAME => {sql_literal(wh.upper())}, RESULT_LIMIT => 1000))
WHERE EXECUTION_STATUS IN ('RUNNING', 'QUEUED', 'RESUMING_WAREHOUSE', 'BLOCKED')
ORDER BY START_TIME
LIMIT 100
"""


def _task_graph_snapshot_ctes(root_task_id: str = "", graph_version: int | None = None) -> str:
    """Current tasks joined to one coherent version per root task graph."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    root_clause = f"WHERE v.ROOT_TASK_ID = {sql_literal(root)}" if root else ""
    version_clause = ""
    if graph_version is not None:
        version = int(graph_version)
        if version < 0:
            raise ValueError("graph_version must be non-negative")
        version_clause = f"\n      AND v.GRAPH_VERSION = {version}"
        if not root_clause:
            root_clause = "WHERE 1 = 1"
    return f"""
WITH current_tasks AS (
    SELECT TO_VARCHAR(ID) AS TASK_ID, STATE, WAREHOUSE AS WAREHOUSE_NAME, SCHEDULE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASKS
    WHERE DELETED IS NULL
),
versions AS (
    SELECT TO_VARCHAR(COALESCE(v.ROOT_TASK_ID, v.ID)) AS ROOT_TASK_ID,
           TO_VARCHAR(v.ID) AS TASK_ID,
           v.GRAPH_VERSION, v.GRAPH_VERSION_CREATED_ON,
           v.DATABASE_NAME, v.SCHEMA_NAME, v.NAME,
           v.DATABASE_NAME || '.' || v.SCHEMA_NAME || '.' || v.NAME AS TASK_FQN,
           v.PREDECESSORS, t.STATE, t.WAREHOUSE_NAME, t.SCHEDULE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS v
    INNER JOIN current_tasks t ON t.TASK_ID = TO_VARCHAR(v.ID)
    INNER JOIN current_tasks r
        ON r.TASK_ID = TO_VARCHAR(COALESCE(v.ROOT_TASK_ID, v.ID))
),
graph_versions AS (
    SELECT DISTINCT v.ROOT_TASK_ID, v.GRAPH_VERSION, v.GRAPH_VERSION_CREATED_ON
    FROM versions v
    {root_clause}{version_clause}
),
latest_graphs AS (
    SELECT ROOT_TASK_ID, GRAPH_VERSION, GRAPH_VERSION_CREATED_ON
    FROM graph_versions
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ROOT_TASK_ID
        ORDER BY GRAPH_VERSION_CREATED_ON DESC, GRAPH_VERSION DESC
    ) = 1
),
snapshot AS (
    SELECT v.*
    FROM versions v
    INNER JOIN latest_graphs g
        ON g.ROOT_TASK_ID = v.ROOT_TASK_ID
       AND g.GRAPH_VERSION = v.GRAPH_VERSION
)
"""


def task_graph_roots() -> str:
    """Latest coherent graph snapshot per active root, for the graph picker."""
    return _task_graph_snapshot_ctes() + """
SELECT ROOT_TASK_ID, GRAPH_VERSION, MAX(GRAPH_VERSION_CREATED_ON) AS GRAPH_CREATED_ON,
       COALESCE(
           MAX(IFF(TASK_ID = ROOT_TASK_ID, TASK_FQN, NULL)),
           MAX(IFF(COALESCE(ARRAY_SIZE(PREDECESSORS), 0) = 0, TASK_FQN, NULL))
       ) AS ROOT_TASK_FQN,
       COUNT(*) AS NODE_COUNT,
       COUNT_IF(LOWER(COALESCE(STATE, '')) LIKE '%suspend%') AS SUSPENDED_NODES
FROM snapshot
GROUP BY ROOT_TASK_ID, GRAPH_VERSION
ORDER BY NODE_COUNT DESC, ROOT_TASK_FQN
"""


def task_graph_nodes(root_task_id: str = "", graph_version: int | None = None) -> str:
    """One complete task-graph snapshot with current state and 24h failures.

    The old reader chose the newest row independently per task and silently
    stopped at 300 rows. That could combine graph versions or draw dangling
    edges. This reader keys the snapshot by ``ROOT_TASK_ID + GRAPH_VERSION``;
    the UI applies an explicit fetch cap and refuses a truncated frame.
    """
    return _task_graph_snapshot_ctes(root_task_id, graph_version) + """,
fails AS (
    SELECT DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS TASK_FQN,
           COUNT(*) AS FAILURES_24H
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE COMPLETED_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
      AND STATE = 'FAILED'
    GROUP BY 1
)
SELECT s.ROOT_TASK_ID, s.GRAPH_VERSION, s.GRAPH_VERSION_CREATED_ON,
       s.TASK_ID, s.TASK_FQN, s.PREDECESSORS, s.STATE,
       s.WAREHOUSE_NAME, s.SCHEDULE,
       COALESCE(f.FAILURES_24H, 0) AS FAILURES_24H,
       COUNT(*) OVER () AS SNAPSHOT_NODE_COUNT
FROM snapshot s
LEFT JOIN fails f ON f.TASK_FQN = s.TASK_FQN
ORDER BY s.TASK_FQN
"""


def task_graph_recent_runs(root_task_id: str = "0", days: int = 7,
                           limit: int = 40) -> str:
    """Recent graph executions for one root, bounded for TASK_HISTORY pruning."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    if not root:
        raise ValueError("root_task_id is required")
    horizon = max(1, min(int(days or 7), 14))
    cap = max(1, min(int(limit or 40), 100))
    return f"""
SELECT COALESCE(TO_VARCHAR(GRAPH_RUN_GROUP_ID), QUERY_ID) AS RUN_KEY,
       MIN(SCHEDULED_TIME) AS SCHEDULED_AT,
       MIN(QUERY_START_TIME) AS STARTED_AT,
       MAX(COMPLETED_TIME) AS COMPLETED_AT,
       COUNT(*) AS TASKS,
       COUNT_IF(STATE = 'FAILED') AS FAILED_TASKS,
       ROUND(SUM(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                                   QUERY_START_TIME), 0)) / 1000, 2) AS QUEUE_SEC,
       DATEDIFF('second', MIN(SCHEDULED_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE SCHEDULED_TIME >= DATEADD('day', -{horizon}, CURRENT_DATE())
  AND TO_VARCHAR(ROOT_TASK_ID) = {sql_literal(root, 80)}
  AND GRAPH_RUN_GROUP_ID IS NOT NULL
GROUP BY COALESCE(TO_VARCHAR(GRAPH_RUN_GROUP_ID), QUERY_ID)
ORDER BY SCHEDULED_AT DESC
LIMIT {cap}
"""


def task_graph_run_nodes(root_task_id: str = "0", run_key: str = "0",
                         days: int = 7) -> str:
    """Node timing and profile evidence for one selected graph execution."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    run = str(run_key or "").strip()
    if not root or not run:
        raise ValueError("root_task_id and run_key are required")
    horizon = max(1, min(int(days or 7), 14))
    return f"""
SELECT DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS TASK_FQN,
       QUERY_ID, STATE, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
       ROUND(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                               QUERY_START_TIME), 0) / 1000, 2) AS QUEUE_SEC,
       ROUND(GREATEST(DATEDIFF('millisecond', QUERY_START_TIME,
                               COMPLETED_TIME), 0) / 1000, 2) AS EXEC_SEC,
       ROUND(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                               COMPLETED_TIME), 0) / 1000, 2) AS RUN_SEC,
       COALESCE(ERROR_CODE::VARCHAR, '') AS ERROR_CODE,
       LEFT(COALESCE(ERROR_MESSAGE, ''), 500) AS ERROR_MESSAGE
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE SCHEDULED_TIME >= DATEADD('day', -{horizon}, CURRENT_DATE())
  AND TO_VARCHAR(ROOT_TASK_ID) = {sql_literal(root, 80)}
  AND TO_VARCHAR(GRAPH_RUN_GROUP_ID) = {sql_literal(run, 100)}
ORDER BY SCHEDULED_TIME, TASK_FQN
LIMIT 2000
"""


def task_graph_versions(root_task_id: str = "0", limit: int = 30) -> str:
    """Historical coherent graph versions for a selected root."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    if not root:
        raise ValueError("root_task_id is required")
    cap = max(2, min(int(limit or 30), 100))
    return f"""
SELECT GRAPH_VERSION, MAX(GRAPH_VERSION_CREATED_ON) AS CREATED_AT,
       COUNT(*) AS NODE_COUNT,
       SUM(COALESCE(ARRAY_SIZE(PREDECESSORS), 0)) AS DEPENDENCY_COUNT
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
WHERE TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) = {sql_literal(root, 80)}
GROUP BY GRAPH_VERSION
ORDER BY CREATED_AT DESC, GRAPH_VERSION DESC
LIMIT {cap}
"""


def task_graph_version_nodes(root_task_id: str = "0",
                             graph_version: int = 0) -> str:
    """Historical topology for one root/version, including deleted nodes."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    version = int(graph_version)
    if not root:
        raise ValueError("root_task_id is required")
    if version < 0:
        raise ValueError("graph_version must be non-negative")
    return f"""
SELECT TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) AS ROOT_TASK_ID,
       GRAPH_VERSION, GRAPH_VERSION_CREATED_ON,
       TO_VARCHAR(ID) AS TASK_ID,
       DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS TASK_FQN,
       PREDECESSORS
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
WHERE TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) = {sql_literal(root, 80)}
  AND GRAPH_VERSION = {version}
ORDER BY TASK_FQN
LIMIT 2000
"""


def volume_deltas() -> str:
    """Yesterday's rows-added vs prior-7d average per moving table — the panel behind
    the PIPE_VOLUME_DROP alert. That alert is LIVE: SP_ANOMALY_SWEEP (TASK_ANOMALY_SWEEP,
    06:40 CT daily) raises a HIGH event when a PROD table's rows-added collapses past a 50%
    drop vs its prior-7-day average, gated on ALERT_CONFIG.ENABLED (so it can be turned off).
    This panel is the account-wide informational view (all databases + the 30-50% WATCH
    band), so it intentionally shows rows that do not page."""
    return """
SELECT DB AS DATABASE_NAME, SCH AS SCHEMA_NAME, TBL AS TABLE_NAME,
       Y_ROWS, ROUND(AVG_ROWS, 0) AS AVG_ROWS_PRIOR_7D,
       ROUND((1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100, 1) AS DROP_PCT,
       CASE WHEN (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 > 50 THEN 'FAILED'
            WHEN (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 > 30 THEN 'WATCH'
            ELSE 'NORMAL' END AS STATUS
FROM (
    SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
           SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()), d.ROWS_ADDED, 0)) AS Y_ROWS,
           SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()), d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
    WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
      AND d.START_TIME < CURRENT_DATE()
    GROUP BY 1, 2, 3
    HAVING AVG_ROWS >= 1000
)
ORDER BY DROP_PCT DESC
LIMIT 50
"""

def warehouse_blast_radius(warehouse: str, days: int = 7) -> str:
    """Who an operator action on this warehouse would affect: per-user share
    of the last N days' work, with role and query-tag evidence (tags reveal
    scheduled tools/tasks the user grain hides)."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    wh = safe_identifier(str(warehouse or "").strip())
    days = bounded_days(days, 30)
    return f"""
SELECT
    USER_NAME,
    ANY_VALUE(ROLE_NAME)                            AS ROLE_NAME,
    COUNT(*)                                        AS QUERIES,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0   AS ELAPSED_SEC,
    MAX(START_TIME)                                 AS LAST_SEEN,
    ANY_VALUE(NULLIF(QUERY_TAG, ''))                AS SAMPLE_TAG
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND UPPER(WAREHOUSE_NAME) = {sql_literal(wh.upper())}
GROUP BY USER_NAME
ORDER BY ELAPSED_SEC DESC
LIMIT 25
"""
