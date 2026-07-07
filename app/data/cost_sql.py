"""Cost SQL builders.

Formula contract (see ARCHITECTURE.md):
- Account billed spend: METERING_DAILY_HISTORY with the cloud-services
  adjustment applied (CREDITS_BILLED, or used+adjustment when absent).
- Company-scoped spend: WAREHOUSE_METERING_HISTORY (exact per warehouse).
- User/database spend: allocated by elapsed-time share, labeled allocated.
Dollarization happens in app/logic/formulas.py, not in SQL.
"""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days, lag_offset_start

_BILLED = (
    "COALESCE(CREDITS_BILLED, GREATEST(0, COALESCE(CREDITS_USED, 0) "
    "+ COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))"
)


def metering_daily_by_service(days: int) -> str:
    """Account-wide billed credits by day and service type (adjustment applied)."""
    days = bounded_days(days)
    return f"""
SELECT
    USAGE_DATE AS DAY,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
    SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE,
    SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CREDITS_CLOUD_SERVICES,
    SUM(COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)) AS CREDITS_ADJUSTMENT,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_USED,
    SUM({_BILLED}) AS CREDITS_BILLED
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY USAGE_DATE, UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN'))
ORDER BY DAY
"""


def warehouse_daily_credits(days: int, company: str = "ALL") -> str:
    """Per-warehouse daily compute credits (exact metering), company-scoped."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    WAREHOUSE_NAME,
    {companies.company_case_sql()} AS COMPANY,
    SUM(COALESCE(CREDITS_USED_COMPUTE, CREDITS_USED)) AS CREDITS_COMPUTE,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_TOTAL
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY DAY, CREDITS_TOTAL DESC
"""


def warehouse_window_vs_prior(days: int, company: str = "ALL") -> str:
    """Current vs prior window credits per warehouse, both lag-offset."""
    days = bounded_days(days)
    current_start = lag_offset_start(days)
    prior_start = lag_offset_start(days * 2)
    horizon = "DATEADD('hour', -24, CURRENT_TIMESTAMP())"
    where = and_where(
        f"START_TIME >= {prior_start}",
        f"START_TIME < {horizon}",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    WAREHOUSE_NAME,
    {companies.company_case_sql()} AS COMPANY,
    SUM(IFF(START_TIME >= {current_start}, COALESCE(CREDITS_USED, 0), 0)) AS CREDITS_CURRENT,
    SUM(IFF(START_TIME <  {current_start}, COALESCE(CREDITS_USED, 0), 0)) AS CREDITS_PRIOR
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE {where}
GROUP BY 1, 2
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
ORDER BY CREDITS_CURRENT DESC
"""


def allocated_attribution(days: int, dimension: str, company: str = "ALL",
                          database: str = "", schema_contains: str = "") -> str:
    """Elapsed-time-share attribution by USER_NAME or DATABASE_NAME.

    Produces shares, not dollars: the caller multiplies by scoped warehouse
    spend and MUST label the result 'allocated'.
    """
    days = bounded_days(days)
    dim = "USER_NAME" if str(dimension).upper() == "USER_NAME" else "DATABASE_NAME"
    scope = companies.user_clause(company) if dim == "USER_NAME" else companies.database_clause(company)
    from app.core.sqlsafe import contains_filter

    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        "WAREHOUSE_NAME IS NOT NULL",
        companies.warehouse_clause(company),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        scope,
    )
    return f"""
SELECT
    COALESCE({dim}, 'UNKNOWN') AS DIMENSION,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS ELAPSED_SEC,
    RATIO_TO_REPORT(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0))) OVER () AS ELAPSED_SHARE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY ELAPSED_SEC DESC
LIMIT 100
"""


def cortex_daily_spend(days: int) -> str:
    """AI/Cortex service credits by day (account-wide, billed basis)."""
    days = bounded_days(days)
    return f"""
SELECT
    USAGE_DATE AS DAY,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
    SUM({_BILLED}) AS CREDITS_BILLED
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
  AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE '%AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')
GROUP BY 1, 2
ORDER BY DAY
"""


def storage_by_database(days: int, company: str = "ALL", database: str = "") -> str:
    """Average database storage per day (bytes), company-scoped."""
    days = bounded_days(days)
    where = and_where(
        f"USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
        companies.database_equals_clause(database),
    )
    return f"""
SELECT
    USAGE_DATE AS DAY,
    DATABASE_NAME,
    AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
    AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
WHERE {where}
GROUP BY 1, 2
ORDER BY DAY
"""


def contract_consumed_credits(contract_start_date: str) -> str:
    """Total billed credits since the contract start (account-wide).

    ``contract_start_date`` must be ISO ``YYYY-MM-DD``; validated by caller
    (settings layer) — defensively re-checked here.
    """
    text = str(contract_start_date or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-" or not text.replace("-", "").isdigit():
        raise ValueError(f"contract_start_date must be YYYY-MM-DD, got {text!r}")
    return f"""
SELECT
    SUM({_BILLED}) AS CREDITS_BILLED_TO_DATE,
    MIN(USAGE_DATE) AS FIRST_DAY,
    MAX(USAGE_DATE) AS LAST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATE '{text}'
"""
