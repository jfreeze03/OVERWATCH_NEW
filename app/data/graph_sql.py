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
WITH runs AS (
    SELECT
        COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
        MIN_BY(h.NAME, h.QUERY_START_TIME) AS PIPELINE,
        MIN_BY(h.DATABASE_NAME, h.QUERY_START_TIME) AS DATABASE_NAME,
        MIN_BY(h.SCHEMA_NAME, h.QUERY_START_TIME) AS SCHEMA_NAME,
        DATE(MIN(h.QUERY_START_TIME)) AS DAY,
        COUNT(*) AS TASK_RUNS,
        COUNT_IF(h.STATE = 'FAILED') AS FAILED_TASKS,
        DATEDIFF('second', MIN(h.QUERY_START_TIME), MAX(h.COMPLETED_TIME)) AS WALL_SEC,
        SUM(COALESCE(a.CREDITS, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
    LEFT JOIN (
        SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_DATE())
        GROUP BY QUERY_ID
    ) a ON a.QUERY_ID = h.QUERY_ID
    WHERE {where}
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
