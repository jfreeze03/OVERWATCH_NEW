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
from app.data.common import and_where, bounded_days, resolve_effective_window

# The recommended structured QUERY_TAG keys (JSON object).
TAG_KEYS = ("pipeline", "run_id", "target_object", "environment", "cost_center")


def etl_cost_by_pipeline(days: int, company: str = "ALL", database: str = "",
                         schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Measured attribution credits per tagged pipeline: runs, credits, per-run,
    per-million-rows-written, per-TiB-scanned, and failed-run waste.

    v4.51 formula corrections (Codex #8): queries aggregate to RUN grain
    before pipeline grain, so per-run math can't be skewed by statement
    count; ROWS counts WRITE statements only (a 3-stage pipeline no longer
    counts its logical rows up to 3x, and tagged validation SELECTs add
    nothing); RUN_ID_CREDIT_PCT reports how much of the spend carries a
    run_id — $/run divides only that share, so partially tagged fleets
    degrade honestly instead of overstating.
    """
    days = bounded_days(days)
    from app.core.sqlsafe import contains_filter

    where = and_where(
        (resolve_effective_window(days, "q.START_TIME", bounds=bounds)[1]
         if bounds is not None
         else f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "q.QUERY_TAG IS NOT NULL",
        "GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline') IS NOT NULL",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
    )
    cred_where = (
        resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
        if bounds is not None
        else f"START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())"
    )
    return f"""
WITH tagged AS (
    SELECT q.QUERY_ID,
           GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline')::VARCHAR AS PIPELINE,
           GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'run_id')::VARCHAR   AS RUN_ID,
           IFF(q.QUERY_TYPE IN ('INSERT', 'MERGE', 'COPY', 'UPDATE', 'DELETE',
                                'CREATE_TABLE_AS_SELECT', 'MULTI_TABLE_INSERT'),
               COALESCE(q.ROWS_PRODUCED, 0), 0) AS ROWS_WRITTEN,
           COALESCE(q.BYTES_SCANNED, 0) AS BYTES_SCANNED,
           q.EXECUTION_STATUS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
),
cred AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE {cred_where}
    GROUP BY QUERY_ID
),
runs AS (
    SELECT t.PIPELINE, t.RUN_ID,
           SUM(c.CREDITS) AS CREDITS,
           SUM(t.ROWS_WRITTEN) AS ROWS_WRITTEN,
           SUM(t.BYTES_SCANNED) AS BYTES_SCANNED,
           SUM(IFF(t.EXECUTION_STATUS <> 'SUCCESS', c.CREDITS, 0)) AS FAILED_CREDITS
    FROM tagged t
    JOIN cred c ON c.QUERY_ID = t.QUERY_ID
    GROUP BY t.PIPELINE, t.RUN_ID
)
SELECT
    PIPELINE,
    COUNT_IF(RUN_ID IS NOT NULL) AS RUNS,
    ROUND(SUM(CREDITS), 4) AS CREDITS,
    ROUND(SUM(IFF(RUN_ID IS NOT NULL, CREDITS, 0))
          / NULLIF(COUNT_IF(RUN_ID IS NOT NULL), 0), 6) AS CREDITS_PER_RUN,
    ROUND(100 * SUM(IFF(RUN_ID IS NOT NULL, CREDITS, 0)) / NULLIF(SUM(CREDITS), 0), 1) AS RUN_ID_CREDIT_PCT,
    ROUND(SUM(CREDITS) / NULLIF(SUM(ROWS_WRITTEN), 0) * 1000000, 6) AS CREDITS_PER_M_ROWS,
    ROUND(SUM(CREDITS) / NULLIF(SUM(BYTES_SCANNED) / POWER(1024, 4), 0), 4) AS CREDITS_PER_TIB,
    ROUND(SUM(FAILED_CREDITS), 4) AS RETRY_WASTE_CREDITS
FROM runs
GROUP BY PIPELINE
HAVING SUM(CREDITS) > 0
ORDER BY CREDITS DESC
LIMIT 100
"""


def etl_failed_runs_for_pipeline(pipeline: str, days: int, company: str = "ALL",
                                 database: str = "", schema_contains: str = "",
                                 *, bounds: tuple | None = None) -> str:
    """Per-pipeline drill: every non-SUCCESS statement for ONE tagged pipeline,
    one row per statement, with its measured attribution credits (the retry/abort
    waste that rolls up into the parent's RETRY_WASTE_CREDITS / Failed-run $).

    Scoping (company/db/schema) and BOTH windows are IDENTICAL to
    etl_cost_by_pipeline — including the Last-month ``bounds`` path — so the drill's
    summed waste $ reconciles to the row's Failed-run $ under every scope window.
    Pipeline equality is an EXACT, case-preserving match on the JSON tag value
    (no UPPER() — the parent extracts PIPELINE from the same GET_PATH(...)::VARCHAR,
    so the clicked cell round-trips exactly)."""
    days = bounded_days(days)
    from app.core.sqlsafe import contains_filter, sql_literal

    where = and_where(
        (resolve_effective_window(days, "q.START_TIME", bounds=bounds)[1]
         if bounds is not None
         else f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "q.QUERY_TAG IS NOT NULL",
        "GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline') IS NOT NULL",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
        f"GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'pipeline')::VARCHAR = {sql_literal(pipeline)}",
        "q.EXECUTION_STATUS <> 'SUCCESS'",
    )
    cred_where = (
        resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
        if bounds is not None
        else f"START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())"
    )
    return f"""
WITH cred AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE {cred_where}
    GROUP BY QUERY_ID
)
SELECT
    q.START_TIME,
    GET_PATH(TRY_PARSE_JSON(q.QUERY_TAG), 'run_id')::VARCHAR AS RUN_ID,
    q.QUERY_ID,
    q.QUERY_TYPE,
    q.EXECUTION_STATUS,
    q.ERROR_CODE,
    q.ERROR_MESSAGE,
    q.WAREHOUSE_NAME,
    q.USER_NAME,
    q.TOTAL_ELAPSED_TIME,
    COALESCE(c.CREDITS, 0) AS WASTE_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
LEFT JOIN cred c ON c.QUERY_ID = q.QUERY_ID
WHERE {where}
ORDER BY q.START_TIME DESC
LIMIT 100
"""


def etl_tag_coverage(days: int, company: str = "ALL", database: str = "",
                     schema_contains: str = "") -> str:
    """Credit-weighted pipeline-tag coverage: how much MEASURED compute carries
    a pipeline tag vs not — the honest denominator for the per-pipeline KPIs."""
    days = bounded_days(days)
    from app.core.sqlsafe import contains_filter

    where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
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
