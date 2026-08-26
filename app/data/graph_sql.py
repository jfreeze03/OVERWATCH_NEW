"""SQL builders for task-graph (pipeline) cost and runtime trends.

Grain honesty:
- One "graph run" = one GRAPH_RUN_GROUP_ID in TASK_HISTORY (standalone tasks
  fall back to their run's QUERY_ID, so a single-task pipeline still counts).
- The pipeline label is the NAME of the first task to start in the run —
  the root task fires first, so this is the root without needing a TASK_ID
  column (ACCOUNT_USAGE.TASK_HISTORY does not expose one).
- Warehouse-task credits are MEASURED per run via QUERY_ATTRIBUTION_HISTORY
  (child statements roll up to the task's query; ~6h lag). Serverless task
  credits live at task-day grain in SERVERLESS_TASK_HISTORY and are reported
  separately — never smeared across graphs they can't be tied to.
"""

from __future__ import annotations

from app import companies
from app.core.sqlsafe import contains_filter
from app.data.common import and_where, bounded_days


def graph_daily_costs(days: int, company: str = "ALL", database: str = "",
                      schema_contains: str = "") -> str:
    """Per DAY x pipeline: graph runs, failures, wall time, measured credits."""
    days = bounded_days(days)
    where = and_where(
        f"h.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "h.STATE IN ('SUCCEEDED', 'FAILED')",
        companies.database_clause(company, "h.DATABASE_NAME"),
        companies.database_equals_clause(database, "h.DATABASE_NAME"),
        contains_filter("h.SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH attempts AS (
    SELECT
        COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
        h.NAME,
        h.DATABASE_NAME,
        h.SCHEMA_NAME,
        h.QUERY_START_TIME,
        h.COMPLETED_TIME,
        h.STATE,
        COALESCE(a.CREDITS, 0) AS CREDITS,
        -- Auto-retries emit multiple rows sharing one SCHEDULED_TIME within a graph run;
        -- tag the terminal attempt so TASK_RUNS/FAILED_TASKS count scheduled TASKS not
        -- attempts (a retried-then-succeeded task is not a failure). Credits still SUM over
        -- all attempts below — each retry really billed compute.
        ROW_NUMBER() OVER (
            PARTITION BY COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID), h.NAME, h.SCHEDULED_TIME
            ORDER BY h.COMPLETED_TIME DESC NULLS LAST) AS TERMINAL_RN
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
    LEFT JOIN (
        -- Roll each statement's compute up to the task's CALL query id via
        -- COALESCE(ROOT_QUERY_ID, QUERY_ID): a task whose body is a stored
        -- procedure attributes credits to its CHILD statements, which carry the
        -- task's query id only as ROOT_QUERY_ID. The old join on the bare
        -- QUERY_ID matched only the ~0-credit CALL row, collapsing proc-driven
        -- pipeline cost to ~0 (audit #10; matches the rollup insights_sql uses).
        SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID,
               SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_DATE())
          -- Prune before the GROUP BY: only task-run queries matter here.
          -- Aggregating the whole view was the 139s family (perf pass #9).
          AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
                AND STATE IN ('SUCCEEDED', 'FAILED')
          )
        GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)
    ) a ON a.ROOT_ID = h.QUERY_ID
    WHERE {where}
),
runs AS (
    SELECT
        RUN_KEY,
        MIN_BY(NAME, QUERY_START_TIME) AS PIPELINE,
        MIN_BY(DATABASE_NAME, QUERY_START_TIME) AS DATABASE_NAME,
        MIN_BY(SCHEMA_NAME, QUERY_START_TIME) AS SCHEMA_NAME,
        DATE(MIN(QUERY_START_TIME)) AS DAY,
        COUNT_IF(TERMINAL_RN = 1) AS TASK_RUNS,
        COUNT_IF(TERMINAL_RN = 1 AND STATE = 'FAILED') AS FAILED_TASKS,
        DATEDIFF('second', MIN(QUERY_START_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC,
        SUM(CREDITS) AS CREDITS
    FROM attempts
    GROUP BY RUN_KEY
)
SELECT
    DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
    COUNT(*) AS GRAPH_RUNS,
    COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
    SUM(TASK_RUNS) AS TASK_RUNS,
    ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
    ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
    ROUND(SUM(CREDITS), 4) AS WH_CREDITS
FROM runs
GROUP BY 1, 2, 3, 4
ORDER BY DAY, WH_CREDITS DESC
LIMIT 5000
"""


def serverless_task_daily(days: int, company: str = "ALL", database: str = "",
                          schema_contains: str = "") -> str:
    """Serverless task credits per DAY x task (task-day grain, exact).

    Guarded at the caller: accounts without serverless tasks return empty;
    if the view itself is missing the panel degrades honestly.
    """
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database, "DATABASE_NAME"),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    DATABASE_NAME, SCHEMA_NAME,
    TASK_NAME,
    ROUND(SUM(COALESCE(CREDITS_USED, 0)), 4) AS SERVERLESS_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY
WHERE {where}
GROUP BY 1, 2, 3, 4
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
ORDER BY DAY, SERVERLESS_CREDITS DESC
LIMIT 2000
"""


def object_dependency_edges(limit: int = 10000) -> str:
    """#19: declared object-dependency edges (REFERENCED -> REFERENCING) for the
    downstream blast-radius walk.

    OBJECT_DEPENDENCIES records that a REFERENCING object depends on a REFERENCED
    object (a view REFERENCING a table). The downstream dependents of X are the
    objects that REFERENCE X — transitively — and those are what break if X changes.
    This returns the raw edge list account-wide; the transitive walk from a chosen
    root runs in pure Python (``lineage.downstream_dependents``) so it is unit-tested
    and cycle-safe, and one cached fetch serves every object viewed in a session.

    UNVERIFIED view (no prior reader on this account) — callers run it probe=True and
    degrade to 'dependency graph unavailable'. OBJECT_DEPENDENCIES records declared
    view/matview/policy references but NOT stored-proc bodies or dynamic SQL, so the
    declared graph is PARTIAL — it is deliberately paired with observed ACCESS_HISTORY
    consumers, and the panel says so. Deterministic ORDER BY so that if the account-
    wide edge set exceeds the row cap the retained subset is stable; the caller passes
    an explicit max_rows and surfaces ``truncated`` as a lower-bound warning (the count
    is never silently cut)."""
    n = int(max(1, min(50000, limit)))
    return f"""
SELECT
    REFERENCED_DATABASE  || '.' || REFERENCED_SCHEMA  || '.' || REFERENCED_OBJECT_NAME  AS REFERENCED_FQN,
    REFERENCED_OBJECT_ID  AS REFERENCED_ID,
    REFERENCING_DATABASE || '.' || REFERENCING_SCHEMA || '.' || REFERENCING_OBJECT_NAME AS REFERENCING_FQN,
    REFERENCING_OBJECT_ID AS REFERENCING_ID,
    REFERENCING_OBJECT_DOMAIN AS REFERENCING_DOMAIN
FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
WHERE REFERENCED_OBJECT_NAME IS NOT NULL
  AND REFERENCING_OBJECT_NAME IS NOT NULL
ORDER BY REFERENCED_FQN, REFERENCING_FQN
LIMIT {n}
"""


def object_blast_consumers(fqns: tuple[str, ...], days: int = 30, limit: int = 200) -> str:
    """#19: OBSERVED consumers (ACCESS_HISTORY) of the root object + its declared
    dependents. Per objectName: distinct queries, distinct users, the read/write
    split, last touch, and a sample user. This is the observed half that catches the
    stored-proc / dynamic-SQL consumers the declared OBJECT_DEPENDENCIES graph misses.

    Enterprise-edition only (ACCESS_HISTORY) — callers run probe=True and degrade to
    'dependency graph only, no measured consumers'. ``days`` bounded; the FQN IN-list
    is whitelisted through sql_literal (the FQNs are ACCOUNT_USAGE-derived, but they
    are literal-guarded anyway — never raw text into SQL).

    Two coverage details that matter for a view-heavy dependent set: (1) the objectName
    is UPPER-folded on BOTH sides of the match so a quoted mixed-case dependent still
    joins (the IN-list is already uppercase; the walk's FQNs are too). (2) The READ arm
    flattens BOTH BASE_OBJECTS_ACCESSED and DIRECT_OBJECTS_ACCESSED — a view read by
    name lands in DIRECT (only its base tables land in BASE), so a BASE-only scan would
    miss exactly the view dependents this feature is built to surface. COUNT(DISTINCT
    QUERY_ID) dedups an object that appears in both arrays for one query."""
    from app.core.sqlsafe import sql_literal
    days = bounded_days(days, 90)
    clean = tuple(dict.fromkeys(str(f).strip().upper() for f in fqns if str(f).strip()))
    if not clean:
        clean = ("\x00__none__",)   # matches nothing -> empty result, never a syntax error
    in_list = ", ".join(sql_literal(f) for f in clean)
    n = int(max(1, min(1000, limit)))
    return f"""
WITH touches AS (
    SELECT UPPER(f.value:"objectName"::STRING) AS FQN, a.QUERY_ID, a.USER_NAME,
           a.QUERY_START_TIME, 'READ' AS KIND
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND UPPER(f.value:"objectName"::STRING) IN ({in_list})
    UNION ALL
    SELECT UPPER(f.value:"objectName"::STRING), a.QUERY_ID, a.USER_NAME,
           a.QUERY_START_TIME, 'READ'
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.DIRECT_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND UPPER(f.value:"objectName"::STRING) IN ({in_list})
    UNION ALL
    SELECT UPPER(f.value:"objectName"::STRING), a.QUERY_ID, a.USER_NAME,
           a.QUERY_START_TIME, 'WRITE'
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.OBJECTS_MODIFIED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND UPPER(f.value:"objectName"::STRING) IN ({in_list})
)
SELECT FQN,
       COUNT(DISTINCT QUERY_ID)                            AS QUERIES,
       COUNT(DISTINCT USER_NAME)                           AS USERS,
       COUNT(DISTINCT IFF(KIND = 'READ', QUERY_ID, NULL))  AS READ_QUERIES,
       COUNT(DISTINCT IFF(KIND = 'WRITE', QUERY_ID, NULL)) AS WRITE_QUERIES,
       MAX(QUERY_START_TIME)                               AS LAST_TOUCH,
       ANY_VALUE(USER_NAME)                                AS SAMPLE_USER
FROM touches
GROUP BY FQN
ORDER BY QUERIES DESC
LIMIT {n}
"""
