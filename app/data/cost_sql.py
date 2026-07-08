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
    """Database storage per day from FACT_STORAGE_DAILY (loaded daily) —
    the live DATABASE_STORAGE_USAGE_HISTORY scan now runs once in the
    loader, not per page view. Page falls back to the _live variant while
    the fact is empty."""
    days = bounded_days(days)
    where = and_where(
        f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
        companies.database_equals_clause(database),
    )
    return f"""
SELECT DAY, DATABASE_NAME,
       SUM(COALESCE(DB_BYTES, 0))       AS DB_BYTES,
       SUM(COALESCE(FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
WHERE {where}
GROUP BY 1, 2
ORDER BY DAY
"""


def storage_by_database_live(days: int, company: str = "ALL", database: str = "") -> str:
    """Live fallback for storage_by_database (fact empty / not deployed)."""
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


def org_usage_in_currency(days: int) -> str:
    """Org-wide daily spend in currency per account (Accounts Spend Summary).

    Requires ORGANIZATION_USAGE access on this account; the page shows a
    friendly setup note when the role cannot see the view.
    """
    days = bounded_days(days)
    return f"""
SELECT
    USAGE_DATE AS DAY,
    ACCOUNT_NAME,
    UPPER(COALESCE(USAGE_TYPE, 'UNKNOWN')) AS USAGE_TYPE,
    MAX(CURRENCY) AS CURRENCY,
    SUM(COALESCE(USAGE_IN_CURRENCY, 0)) AS USAGE_IN_CURRENCY
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2, 3
ORDER BY DAY, ACCOUNT_NAME
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


def cloud_services_ratio_by_warehouse(days: int, company: str = "ALL") -> str:
    """Cloud-services share of each warehouse's credits (CoCo's top finding).

    >10% deserves a look, >20% (the alert threshold) usually means many tiny
    queries, metadata-heavy patterns, or compile-heavy SQL.
    """
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    WAREHOUSE_NAME,
    ROUND(SUM(CREDITS_USED_COMPUTE), 2) AS COMPUTE_CREDITS,
    ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 2) AS CLOUD_SVC_CREDITS,
    ROUND(SUM(CREDITS_USED), 2) AS TOTAL_CREDITS,
    ROUND(SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(SUM(CREDITS_USED), 0) * 100, 1) AS CLOUD_SVC_PCT,
    CASE
        WHEN SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(SUM(CREDITS_USED), 0) > 0.20 THEN 'ELEVATED'
        WHEN SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(SUM(CREDITS_USED), 0) > 0.10 THEN 'WATCH'
        ELSE 'NORMAL'
    END AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE {where}
GROUP BY 1
HAVING SUM(CREDITS_USED) >= 0.5
ORDER BY CLOUD_SVC_PCT DESC
LIMIT 100
"""


def compile_heavy_families(days: int, company: str = "ALL") -> str:
    """Query families whose compile time dominates — the usual driver of a
    high cloud-services ratio."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT,
    COUNT(*) AS RUNS,
    ROUND(AVG(COMPILATION_TIME) / 1000, 2) AS AVG_COMPILE_S,
    ROUND(AVG(TOTAL_ELAPSED_TIME) / 1000, 2) AS AVG_TOTAL_S,
    ROUND(AVG(COMPILATION_TIME) / NULLIF(AVG(TOTAL_ELAPSED_TIME), 0) * 100, 1) AS COMPILE_PCT,
    ROUND(SUM(COMPILATION_TIME) / 3600000, 2) AS TOTAL_COMPILE_HOURS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
HAVING COUNT(*) >= 20 AND AVG(COMPILATION_TIME) > 500
ORDER BY SUM(COMPILATION_TIME) DESC
LIMIT 25
"""

def org_account_month_usd(months: int = 2) -> str:
    """This account's org-billed dollars by month and bucket, for the
    reconciliation panel: COMPUTE_USD (compute + cloud services + their
    adjustments — the bucket the app's credits x rate models) vs TOTAL_USD
    (everything: storage, transfer, serverless, priority support).

    Uses the org rate card, so this is billing truth; differences from the
    app's model are rate-card reality, not a bug in either number.
    """
    months = max(1, min(int(months or 2), 12))
    return f"""
SELECT
    DATE_TRUNC('month', USAGE_DATE)::DATE AS MONTH,
    SUM(IFF(LOWER(USAGE_TYPE) LIKE '%compute%' OR LOWER(USAGE_TYPE) LIKE '%cloud service%',
            USAGE_IN_CURRENCY, 0))        AS COMPUTE_USD,
    SUM(USAGE_IN_CURRENCY)                AS TOTAL_USD,
    MAX(CURRENCY)                         AS CURRENCY
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
  AND USAGE_DATE >= DATE_TRUNC('month', DATEADD('month', -{months - 1}, CURRENT_DATE()))
GROUP BY 1
ORDER BY 1 DESC
"""

def tag_coverage(days: int, company: str = "ALL") -> str:
    """Query-tag governance: execution-time-weighted coverage + the top
    untagged workloads by user. Chargeback precision is capped by this."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    USER_NAME,
    SUM(EXECUTION_TIME) / 1000.0 AS EXEC_SEC,
    SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL, EXECUTION_TIME, 0)) / 1000.0 AS UNTAGGED_EXEC_SEC,
    ROUND(100 * (1 - UNTAGGED_EXEC_SEC / NULLIF(EXEC_SEC, 0)), 1) AS TAGGED_PCT,
    COUNT(*) AS QUERIES
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY USER_NAME
HAVING EXEC_SEC > 60
ORDER BY UNTAGGED_EXEC_SEC DESC
LIMIT 30
"""
