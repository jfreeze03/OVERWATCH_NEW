"""Cost SQL builders (F1-F4 cost-audit fixes, 2026-07-14).

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
    """Per-warehouse daily compute credits (exact usage, not billed), company-scoped."""
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
    SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_TOTAL
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE {where}
  AND WAREHOUSE_ID > 0
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
  AND WAREHOUSE_ID > 0
GROUP BY 1, 2
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
ORDER BY CREDITS_CURRENT DESC
"""


def allocated_attribution(days: int, dimension: str, company: str = "ALL",
                          database: str = "", schema_contains: str = "") -> str:
    """Elapsed-time-share attribution by USER_NAME or DATABASE_NAME.

    Produces shares, not dollars: the caller multiplies by scoped warehouse
    spend and MUST label the result 'allocated'.

    Size note (F2, 2026-07-14): this LIVE builder shares by elapsed time, which
    is warehouse-size-blind (an XS second and a 4XL second count the same). It
    is the fallback path; the normal path is mart27_sql.alloc_attribution, whose
    ALLOC_CREDITS share is weighted per warehouse-hour by real credits (size-
    aware). The elapsed-share form here is deliberate — the global-share law
    (below) was a bug-fix and is lock-tested — so the UI caption flags the live
    path as the coarser estimate rather than silently credit-weighting it.

    Shares are GLOBAL over the company's scoped warehouse activity in the
    window (live math fix 2026-07-11: RATIO_TO_REPORT over the FILTERED set
    renormalized any database filter to 100%, so every selected database
    'cost' the whole window). Database/schema filters and the dimension
    visibility rules only choose which rows DISPLAY — the denominator
    never moves, so a filtered view shows its true slice."""
    days = bounded_days(days)
    dim = "USER_NAME" if str(dimension).upper() == "USER_NAME" else "DATABASE_NAME"
    vis = (companies.user_clause(company) if dim == "USER_NAME"
           else companies.database_visibility_clause(company))
    from app.core.sqlsafe import contains_filter

    scope_where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        "WAREHOUSE_NAME IS NOT NULL",
        companies.warehouse_clause(company),
    )
    display_where = and_where(
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        vis,
    )
    return f"""
WITH scoped AS (
    SELECT {dim} AS DIM_VAL, DATABASE_NAME, SCHEMA_NAME, USER_NAME,
           COALESCE(TOTAL_ELAPSED_TIME, 0) AS ELAPSED_MS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {scope_where}
)
SELECT
    COALESCE(DIM_VAL, 'NONE') AS DIMENSION,
    COUNT(*) AS QUERY_COUNT,
    SUM(ELAPSED_MS) / 1000.0 AS ELAPSED_SEC,
    SUM(ELAPSED_MS) / NULLIF((SELECT SUM(ELAPSED_MS) FROM scoped), 0) AS ELAPSED_SHARE
FROM scoped
WHERE {display_where}
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
    """Per-database storage on the BILLING basis: the average of daily bytes
    over the window (F1, 2026-07-14). Snowflake bills storage on the monthly
    average of daily on-disk bytes, so the r19 latest-day snapshot over/under-
    stated any database that grew or shrank mid-window. FACT_STORAGE_DAILY
    holds one row per day per database, each already that day's average bytes;
    the page falls back to the _live variant while the fact is empty."""
    days = bounded_days(days)
    where = and_where(
        f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
        companies.database_equals_clause(database),
    )
    return f"""
SELECT DATABASE_NAME,
       AVG(COALESCE(DB_BYTES, 0))       AS DB_BYTES,
       AVG(COALESCE(FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES,
       COUNT(DISTINCT DAY)              AS DAYS_AVERAGED,
       MAX(DAY)                         AS LATEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING AVG(COALESCE(DB_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_by_database_live(days: int, company: str = "ALL", database: str = "") -> str:
    """Live fallback for storage_by_database (fact empty / not deployed):
    average of daily AVERAGE_*_BYTES over the window per database — the same
    monthly-average billing basis as the fact path (F1, 2026-07-14)."""
    days = bounded_days(days)
    where = and_where(
        f"USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
        companies.database_equals_clause(database),
    )
    return f"""
SELECT
    DATABASE_NAME,
    AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
    AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES,
    COUNT(DISTINCT USAGE_DATE)               AS DAYS_AVERAGED,
    MAX(USAGE_DATE)                          AS LATEST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_by_database_calendar(company: str = "ALL", database: str = "", prior: bool = False) -> str:
    """Per-database storage on the CALENDAR-month billing basis (item 7,
    2026-07-14): average of daily bytes over the current month-to-date
    (excluding today's partial day) or the prior completed calendar month.
    Snowflake bills storage on the monthly average of daily on-disk bytes."""
    if prior:
        lo = "DATE_TRUNC('month', DATEADD('month', -1, CURRENT_DATE()))"
        hi = "DATE_TRUNC('month', CURRENT_DATE())"
    else:
        lo = "DATE_TRUNC('month', CURRENT_DATE())"
        hi = "CURRENT_DATE()"
    where = and_where(f"DAY >= {lo}", f"DAY < {hi}",
                      companies.database_clause(company),
                      companies.database_equals_clause(database))
    return f"""
SELECT DATABASE_NAME,
       AVG(COALESCE(DB_BYTES, 0))       AS DB_BYTES,
       AVG(COALESCE(FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES,
       COUNT(DISTINCT DAY)              AS DAYS_AVERAGED,
       MAX(DAY)                         AS LATEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING AVG(COALESCE(DB_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_by_database_calendar_live(company: str = "ALL", database: str = "", prior: bool = False) -> str:
    """Live fallback for storage_by_database_calendar (fact empty): same
    calendar-month billing basis from DATABASE_STORAGE_USAGE_HISTORY."""
    if prior:
        lo = "DATE_TRUNC('month', DATEADD('month', -1, CURRENT_DATE()))"
        hi = "DATE_TRUNC('month', CURRENT_DATE())"
    else:
        lo = "DATE_TRUNC('month', CURRENT_DATE())"
        hi = "CURRENT_DATE()"
    where = and_where(f"USAGE_DATE >= {lo}", f"USAGE_DATE < {hi}",
                      companies.database_clause(company),
                      companies.database_equals_clause(database))
    return f"""
SELECT
    DATABASE_NAME,
    AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
    AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES,
    COUNT(DISTINCT USAGE_DATE)               AS DAYS_AVERAGED,
    MAX(USAGE_DATE)                          AS LATEST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_account_truth(days: int) -> str:
    """Account-wide storage by tier on the billing basis (F1b/R3, V046):
    average of daily bytes for table, stage, fail-safe, hybrid, and archive
    cool/cold. Account grain only — STORAGE_USAGE (and this fact) carry no
    per-database split for stage/hybrid/archive. Reads FACT_STORAGE_ACCOUNT_
    DAILY; page falls back to the _live variant while the fact is empty."""
    days = bounded_days(days, maximum=400)
    return f"""
SELECT
    AVG(COALESCE(TABLE_BYTES, 0))        AS TABLE_BYTES,
    AVG(COALESCE(STAGE_BYTES, 0))        AS STAGE_BYTES,
    AVG(COALESCE(FAILSAFE_BYTES, 0))     AS FAILSAFE_BYTES,
    AVG(COALESCE(HYBRID_BYTES, 0))       AS HYBRID_BYTES,
    AVG(COALESCE(ARCHIVE_COOL_BYTES, 0)) AS ARCHIVE_COOL_BYTES,
    AVG(COALESCE(ARCHIVE_COLD_BYTES, 0)) AS ARCHIVE_COLD_BYTES,
    COUNT(DISTINCT DAY)                  AS DAYS_AVERAGED,
    MAX(DAY)                             AS LATEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
"""


def storage_account_truth_live(days: int) -> str:
    """Live fallback for storage_account_truth from ACCOUNT_USAGE.STORAGE_USAGE
    (F1b/R3, V046). Same monthly-average billing basis; account-wide. Note the
    view is Snowflake's own estimate that will not match the invoice exactly —
    org USAGE_IN_CURRENCY is billing truth."""
    days = bounded_days(days, maximum=400)
    return f"""
SELECT
    AVG(COALESCE(STORAGE_BYTES, 0))              AS TABLE_BYTES,
    AVG(COALESCE(STAGE_BYTES, 0))               AS STAGE_BYTES,
    AVG(COALESCE(FAILSAFE_BYTES, 0))            AS FAILSAFE_BYTES,
    AVG(COALESCE(HYBRID_TABLE_STORAGE_BYTES, 0)) AS HYBRID_BYTES,
    AVG(COALESCE(ARCHIVE_STORAGE_COOL_BYTES, 0)) AS ARCHIVE_COOL_BYTES,
    AVG(COALESCE(ARCHIVE_STORAGE_COLD_BYTES, 0)) AS ARCHIVE_COLD_BYTES,
    COUNT(DISTINCT USAGE_DATE)                  AS DAYS_AVERAGED,
    MAX(USAGE_DATE)                             AS LATEST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE
WHERE USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
"""


def org_usage_in_currency(days: int) -> str:
    """Org-wide daily spend in currency per account (Accounts Spend Summary).

    Requires ORGANIZATION_USAGE access on this account; the page shows a
    friendly setup note when the role cannot see the view. Org-usage data is
    UTC, can lag up to ~72h, and mutates until month close (item 6 caveat).
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


def org_contract_items() -> str:
    """Contract line items from the org rate card (amounts in currency).

    ORGANIZATION_USAGE.CONTRACT_ITEMS is the billing truth behind Snowsight
    Admin → Cost Management: committed amount and term dates per contract.
    Needs org visibility on this account; callers degrade honestly when the
    role cannot see the view.
    """
    return """
SELECT CONTRACT_NUMBER, CONTRACT_ITEM, AMOUNT, CURRENCY,
       START_DATE, END_DATE, ORGANIZATION_NAME
FROM SNOWFLAKE.ORGANIZATION_USAGE.CONTRACT_ITEMS
ORDER BY START_DATE DESC, CONTRACT_NUMBER DESC
"""


def org_remaining_balance(days: int = 120) -> str:
    """Daily remaining contract balance — the number that burns each day.

    REMAINING_BALANCE_DAILY per Snowflake billing: FREE_USAGE_BALANCE +
    CAPACITY_BALANCE + ROLLOVER_BALANCE is what's left on the contract in
    currency; ON_DEMAND_CONSUMPTION_BALANCE goes negative once usage runs
    past the commitment (billed on demand). Refreshed daily by Snowflake.
    """
    days = bounded_days(days, maximum=400)
    return f"""
SELECT
    DATE AS DAY,
    CONTRACT_NUMBER,
    MAX(CURRENCY) AS CURRENCY,
    SUM(COALESCE(FREE_USAGE_BALANCE, 0)) AS FREE_USAGE_BALANCE,
    SUM(COALESCE(CAPACITY_BALANCE, 0)) AS CAPACITY_BALANCE,
    SUM(COALESCE(ROLLOVER_BALANCE, 0)) AS ROLLOVER_BALANCE,
    SUM(COALESCE(ON_DEMAND_CONSUMPTION_BALANCE, 0)) AS ON_DEMAND_CONSUMPTION_BALANCE,
    SUM(COALESCE(FREE_USAGE_BALANCE, 0) + COALESCE(CAPACITY_BALANCE, 0)
        + COALESCE(ROLLOVER_BALANCE, 0)) AS TOTAL_REMAINING
FROM SNOWFLAKE.ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY
WHERE DATE >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2
ORDER BY DAY, CONTRACT_NUMBER
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
  AND WAREHOUSE_ID > 0
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
    app's model are rate-card reality, not a bug in either number. Data is UTC,
    can lag up to ~72h, and mutates until month close; classification of the
    compute bucket still uses USAGE_TYPE pending the structured-dimension
    rebuild (item 6 — needs the account's RATING_TYPE/SERVICE_TYPE values).
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


def cs_by_query_type(days: int, company: str = "ALL") -> str:
    """Cloud-services credits by statement type (COST_DB recon R6) — makes
    metadata storms (SHOW/DESCRIBE floods) visible beside the compile-heavy
    families when the CS ratio is ELEVATED."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "CREDITS_USED_CLOUD_SERVICES > 0",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    QUERY_TYPE,
    COUNT(*) AS QUERIES,
    ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 4) AS CS_CREDITS,
    ROUND(SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(COUNT(*), 0) * 1000, 4) AS CS_CREDITS_PER_1K
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY QUERY_TYPE
ORDER BY CS_CREDITS DESC
LIMIT 12
"""
