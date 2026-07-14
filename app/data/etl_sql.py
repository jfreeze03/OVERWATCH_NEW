"""ETL unit-cost SQL builders (architectural Phase 3, 2026-07-14).

Reads a structured JSON QUERY_TAG convention — pipeline / run_id /
target_object / environment / cost_center (see docs/design/ETL_COST_TAGS.md) —
and joins it to MEASURED attribution credits to answer 'what does one pipeline
run cost'. Untagged queries are excluded; etl_tag_coverage reports how much of
the measured spend that leaves out. GET_PATH (not the ':' variant path) keeps
these parse-clean for the canary gate. No dollar rates in SQL — app/logic
dollarizes.
"""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days

# The recommended structured QUERY_TAG keys (JSON object).
TAG_KEYS = ("pipeline", "run_id", "target_object", "environment", "cost_center")


def etl_cost_by_pipeline(days: int, company: str = "ALL") -> str:
    """Measured attribution credits per tagged pipeline: runs, credits, per-run,
    per-million-rows, per-TiB-scanned, and failed-run (retry/abort) waste."""
    days = bounded_days(days)
    where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "q.QUERY_TAG IS NOT NULL",
        "GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline') IS NOT NULL",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
    )
    return f"""
WITH tagged AS (
    SELECT q.QUERY_ID,
           GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline')::VARCHAR AS PIPELINE,
           GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'run_id')::VARCHAR   AS RUN_ID,
           COALESCE(q.ROWS_PRODUCED, 0) AS ROWS_PRODUCED,
           COALESCE(q.BYTES_SCANNED, 0) AS BYTES_SCANNED,
           q.EXECUTION_STATUS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
),
cred AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
    GROUP BY QUERY_ID
)
SELECT
    t.PIPELINE,
    COUNT(DISTINCT t.RUN_ID) AS RUNS,
    ROUND(SUM(c.CREDITS), 4) AS CREDITS,
    ROUND(SUM(c.CREDITS) / NULLIF(COUNT(DISTINCT t.RUN_ID), 0), 6) AS CREDITS_PER_RUN,
    ROUND(SUM(c.CREDITS) / NULLIF(SUM(t.ROWS_PRODUCED), 0) * 1000000, 6) AS CREDITS_PER_M_ROWS,
    ROUND(SUM(c.CREDITS) / NULLIF(SUM(t.BYTES_SCANNED) / POWER(1024, 4), 0), 4) AS CREDITS_PER_TIB,
    ROUND(SUM(IFF(t.EXECUTION_STATUS <> 'SUCCESS', c.CREDITS, 0)), 4) AS RETRY_WASTE_CREDITS
FROM tagged t
JOIN cred c ON c.QUERY_ID = t.QUERY_ID
GROUP BY t.PIPELINE
HAVING SUM(c.CREDITS) > 0
ORDER BY CREDITS DESC
LIMIT 100
"""


def etl_tag_coverage(days: int, company: str = "ALL") -> str:
    """Credit-weighted pipeline-tag coverage: how much MEASURED compute carries
    a pipeline tag vs not — the honest denominator for the per-pipeline KPIs."""
    days = bounded_days(days)
    where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
    )
    return f"""
WITH cred AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
    GROUP BY QUERY_ID
),
q AS (
    SELECT q.QUERY_ID,
           IFF(GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline') IS NOT NULL, 1, 0) AS IS_TAGGED
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
)
SELECT
    ROUND(SUM(c.CREDITS), 4) AS TOTAL_CREDITS,
    ROUND(SUM(IFF(q.IS_TAGGED = 1, c.CREDITS, 0)), 4) AS TAGGED_CREDITS,
    ROUND(SUM(IFF(q.IS_TAGGED = 0, c.CREDITS, 0)), 4) AS UNTAGGED_CREDITS,
    ROUND(100 * SUM(IFF(q.IS_TAGGED = 1, c.CREDITS, 0)) / NULLIF(SUM(c.CREDITS), 0), 1) AS TAGGED_CREDIT_PCT
FROM q
JOIN cred c ON c.QUERY_ID = q.QUERY_ID
"""
