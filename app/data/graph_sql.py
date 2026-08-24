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
