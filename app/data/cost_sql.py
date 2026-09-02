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
from app.core.sqlsafe import sql_literal
from app.data.common import (
    account_month_start_sql,
    account_today_sql,
    ai_service_predicate,
    and_where,
    bounded_days,
    resolve_effective_window,
    scope_window_where,
)

_BILLED = (
    "COALESCE(CREDITS_BILLED, GREATEST(0, COALESCE(CREDITS_USED, 0) "
    "+ COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))"
)


def metering_daily_by_service(days: int, *, bounds: tuple | None = None) -> str:
    """Account-wide billed credits by day and service type (adjustment applied).

    Live ACCOUNT_USAGE fallback for the headline metering fact; honors the same
    'Last month' bounded (start, end_exclusive) range as its mart twin when ``bounds``
    is set, else keeps the trailing day-offset."""
    days = bounded_days(days)
    where = (resolve_effective_window(days, "USAGE_DATE", bounds=bounds)[1]
             if bounds is not None
             else f"USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())")
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
WHERE {where}
GROUP BY USAGE_DATE, UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN'))
ORDER BY DAY
"""


def _wh_company_scope(company: str) -> str:
    """Company scope for a live WAREHOUSE_METERING_HISTORY read via the COMPANY_SCOPE-aware
    UDF (COMPANY_FOR_WAREHOUSE) — the SAME axis the COMPANY label and the mart path use, and
    the pattern ops_sql._query_scope (C10) established. The name-pattern warehouse_clause()
    drops a COMPANY_SCOPE-mapped warehouse whose name doesn't match WH_ALFA_/the Trexis list
    from its per-company view, so the live leg disagreed with the mart for a mapped warehouse
    (round-11 MC-1). Empty for ALL (no filter)."""
    return ("" if str(company or "ALL").upper() == "ALL"
            else f"{companies.company_case_sql('WAREHOUSE_NAME')} = {sql_literal(company)}")


def warehouse_daily_credits(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Per-warehouse daily compute credits (exact usage, not billed), company-scoped."""
    days = bounded_days(days)
    where = and_where(
        scope_window_where("START_TIME", days, bounds=bounds),
        _wh_company_scope(company),
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


def warehouse_window_vs_prior(days: int, company: str = "ALL", *,
                              bounds: tuple | None = None) -> str:
    """Current vs prior window credits per warehouse on ONE explicit half-open
    CALENDAR window — the same [start, end) the allocation SHARES use.

    For 'Last month' (``bounds``) CURRENT is the previous calendar month and PRIOR is
    the month before it (an equal calendar period, not an equal day-count).

    #17: the dollar POOL this builds (CREDITS_CURRENT) is multiplied by the live
    allocation shares (allocated_attribution), which resolve their window through
    resolve_effective_window — complete calendar days, today excluded. The old
    form pooled on a ROLLING 24h-ago timestamp (lag_offset_start), so the pool
    window and the share window were shifted by up to a day (share = calendar
    [today-eff, today); pool = [now-24h-eff*24h, now-24h)) and per-entity dollars
    mis-attributed at the window edges. Both now anchor CURRENT_DATE() through the
    ONE truth and keep the 90-day live cap. Prior = the immediately preceding
    equal-length calendar window."""
    if bounds is not None:
        cur_start, cur_end = bounds
        _prior_start_d = (cur_start.replace(year=cur_start.year - 1, month=12)
                          if cur_start.month == 1
                          else cur_start.replace(month=cur_start.month - 1))
        current_start = f"'{cur_start.isoformat()}'"
        prior_start = f"'{_prior_start_d.isoformat()}'"
        window_end = f"'{cur_end.isoformat()}'"
    else:
        eff, _win = resolve_effective_window(days, "START_TIME", max_days=90)
        current_start = f"DATEADD('day', -{eff}, CURRENT_DATE())"
        prior_start = f"DATEADD('day', -{2 * eff}, CURRENT_DATE())"
        window_end = "CURRENT_DATE()"
    where = and_where(
        f"START_TIME >= {prior_start}",
        f"START_TIME < {window_end}",
        _wh_company_scope(company),
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


def hourly_credits(hours: int, company: str = "ALL") -> str:
    """Per-hour compute credits over the last N hours, company-scoped (CR9).

    Feeds the credit overlay on the Control Room incident-correlation timeline,
    so a spend spike and the events around it read on one time axis. HOUR_TS is
    derived the SAME way the timeline events derive their AT column
    (``::TIMESTAMP_NTZ``), so the two align under the shared display-timezone
    conversion instead of drifting by the session-vs-account offset.
    """
    hours = max(1, min(int(hours), 168))  # <= 7 days, matching the timeline windows
    where = and_where(
        f"START_TIME >= DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())",
        _wh_company_scope(company),
    )
    return f"""
SELECT
    DATE_TRUNC('hour', START_TIME)::TIMESTAMP_NTZ AS HOUR_TS,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE {where}
  AND WAREHOUSE_ID > 0
GROUP BY 1
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
ORDER BY 1
"""


def allocated_attribution(days: int, dimension: str, company: str = "ALL",
                          database: str = "", schema_contains: str = "", *,
                          bounds: tuple | None = None) -> str:
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
    # rec 4: exclude today's partial and share the effective-window contract with the
    # mart share + the dollar pool (resolve_effective_window), so the live fallback's
    # denominator lines up too. Keep the 90-day cap here — this is a live QUERY_HISTORY
    # scan, and the mart path (182d, credit-weighted) is the normal, preferred estimate.
    days, _win = resolve_effective_window(days, "START_TIME", max_days=90, bounds=bounds)
    dim = "USER_NAME" if str(dimension).upper() == "USER_NAME" else "DATABASE_NAME"
    vis = (companies.user_scope_subquery(company, source="SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", distinct_where=_win)
           if dim == "USER_NAME"
           else companies.database_visibility_clause(company))
    from app.core.sqlsafe import contains_filter

    scope_where = and_where(
        _win,
        "EXECUTION_STATUS = 'SUCCESS'",
        "WAREHOUSE_NAME IS NOT NULL",
        _wh_company_scope(company),   # MC-1: match the pool's COMPANY_SCOPE-aware axis
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


def cortex_daily_spend(days: int, *, bounds: tuple | None = None) -> str:
    """AI/Cortex service credits by day (account-wide, billed basis)."""
    days = bounded_days(days)
    where = scope_window_where("USAGE_DATE", days, bounds=bounds)
    return f"""
SELECT
    USAGE_DATE AS DAY,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
    SUM({_BILLED}) AS CREDITS_BILLED
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE {where}
  AND {ai_service_predicate()}
GROUP BY 1, 2
ORDER BY DAY
"""


def replication_by_database(days: int, company: str = "ALL", database: str = "", *, bounds: tuple | None = None) -> str:
    """Native replication credits and transferred bytes by secondary database."""
    days = bounded_days(days, 365)
    where = and_where(
        scope_window_where("START_TIME", days, bounds=bounds),
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATABASE_NAME,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS,
    SUM(COALESCE(BYTES_TRANSFERRED, 0)) AS BYTES_TRANSFERRED,
    SUM(COALESCE(BYTES_TRANSFERRED, 0)) / POWER(1024, 4) AS TIB_TRANSFERRED,
    MIN(START_TIME) AS FIRST_USE,
    MAX(END_TIME) AS LAST_USE
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_REPLICATION_USAGE_HISTORY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
    OR SUM(COALESCE(BYTES_TRANSFERRED, 0)) > 0
ORDER BY CREDITS DESC, BYTES_TRANSFERRED DESC
"""


def transfer_egress_priced(days: int, *, bounds: tuple | None = None) -> str:
    """rec#11: outbound data-transfer (egress) bytes grouped by source/target
    cloud+region and transfer type, with a BILLABLE flag.

    Cross-region OR cross-cloud transfer OUT is billed; same-region same-cloud is
    free — so BILLABLE is the app's cross-boundary estimate (Snowflake owns the
    exact determination). The IFF reads the BASE columns, not the COALESCE'd
    display aliases. Account-wide (transfer carries no company grain). Bytes only:
    the UI dollarizes with the org rate-card implied rate (TRANSFER_USD / billable
    TB), falling back to the DATA_TRANSFER_USD_PER_TB setting when org currency is
    not visible — never an inlined rate (house rule d)."""
    days = bounded_days(days, 365)
    where = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
             if bounds is not None
             else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    return f"""
SELECT
    COALESCE(SOURCE_CLOUD, '(unknown)')      AS SOURCE_CLOUD,
    COALESCE(SOURCE_REGION, '(unknown)')     AS SOURCE_REGION,
    COALESCE(TARGET_CLOUD, '(internal)')     AS TARGET_CLOUD,
    COALESCE(TARGET_REGION, '(same region)') AS TARGET_REGION,
    TRANSFER_TYPE,
    IFF(TARGET_REGION IS NOT NULL
        AND (COALESCE(TARGET_REGION, '') <> COALESCE(SOURCE_REGION, '')
             OR COALESCE(TARGET_CLOUD, '') <> COALESCE(SOURCE_CLOUD, '')),
        TRUE, FALSE)                         AS BILLABLE,
    ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 4), 6) AS TB,
    SUM(BYTES_TRANSFERRED)                   AS BYTES
FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
WHERE {where}
GROUP BY 1, 2, 3, 4, 5, 6
HAVING SUM(BYTES_TRANSFERRED) > 0
ORDER BY BILLABLE DESC, TB DESC
"""


def qas_roi(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """rec#6: Query Acceleration Service ROI per warehouse — QAS credits SPENT
    (QUERY_ACCELERATION_HISTORY) beside the eligible acceleration workload
    (QUERY_ACCELERATION_ELIGIBLE, the benefit side the app never read).

    A FULL OUTER JOIN so both regimes surface: a warehouse PAYING for QAS with
    little eligible workload (drop candidate) and one with meaningful eligible
    workload but QAS off (enable candidate). Eligibility is a utilization signal,
    not a dollarized benefit — Snowflake reports eligible query time, not the
    compute it would save. Account-wide unless scoped to a company's warehouses.
    """
    days = bounded_days(days, 365)
    wc = _wh_company_scope(company)   # MC-1: COMPANY_SCOPE-aware, not name-pattern
    win = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
           if bounds is not None
           else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    return f"""
WITH elig AS (
    SELECT WAREHOUSE_NAME,
           COUNT(DISTINCT QUERY_ID) AS ELIGIBLE_QUERIES,
           ROUND(SUM(COALESCE(ELIGIBLE_QUERY_ACCELERATION_TIME, 0)), 1) AS ELIGIBLE_SEC,
           MAX(UPPER_LIMIT_SCALE_FACTOR) AS MAX_SCALE_FACTOR
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ACCELERATION_ELIGIBLE
    WHERE {and_where(win, wc)}
    GROUP BY 1
),
used AS (
    SELECT WAREHOUSE_NAME, SUM(COALESCE(CREDITS_USED, 0)) AS QAS_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ACCELERATION_HISTORY
    WHERE {and_where(win, wc)}
    GROUP BY 1
)
SELECT
    COALESCE(e.WAREHOUSE_NAME, u.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
    ROUND(COALESCE(u.QAS_CREDITS, 0), 4)         AS QAS_CREDITS,
    COALESCE(e.ELIGIBLE_QUERIES, 0)              AS ELIGIBLE_QUERIES,
    COALESCE(e.ELIGIBLE_SEC, 0)                  AS ELIGIBLE_SEC,
    e.MAX_SCALE_FACTOR                           AS MAX_SCALE_FACTOR
FROM elig e
FULL OUTER JOIN used u ON e.WAREHOUSE_NAME = u.WAREHOUSE_NAME
WHERE COALESCE(u.QAS_CREDITS, 0) > 0 OR COALESCE(e.ELIGIBLE_QUERIES, 0) > 0
ORDER BY QAS_CREDITS DESC, ELIGIBLE_SEC DESC
LIMIT 200
"""


def compute_pool_usage(days: int, *, bounds: tuple | None = None) -> str:
    """SPCS credits by compute pool and owning application (account-wide)."""
    days = bounded_days(days, 365)
    where = scope_window_where("START_TIME", days, bounds=bounds)
    return f"""
SELECT
    COMPUTE_POOL_NAME,
    COALESCE(APPLICATION_NAME, 'Unassigned') AS APPLICATION_NAME,
    IS_EXCLUSIVE,
    SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS,
    MIN(START_TIME) AS FIRST_USE,
    MAX(END_TIME) AS LAST_USE
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY
WHERE {where}
GROUP BY COMPUTE_POOL_NAME, COALESCE(APPLICATION_NAME, 'Unassigned'), IS_EXCLUSIVE
HAVING SUM(COALESCE(CREDITS_USED, 0)) > 0
ORDER BY CREDITS DESC
"""


def notebook_container_usage(days: int, *, bounds: tuple | None = None) -> str:
    """Notebook runtime and credits, a non-additive subset of SPCS usage."""
    days = bounded_days(days, 365)
    where = scope_window_where("START_TIME", days, bounds=bounds)
    return f"""
SELECT
    NOTEBOOK_NAME,
    USER_NAME,
    COMPUTE_POOL_NAME,
    SERVICE_NAME,
    SUM(COALESCE(NOTEBOOK_EXECUTION_TIME_SECS, 0)) AS EXECUTION_TIME_SEC,
    SUM(COALESCE(CREDITS, 0)) AS CREDITS,
    MIN(START_TIME) AS FIRST_USE,
    MAX(END_TIME) AS LAST_USE
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE {where}
GROUP BY NOTEBOOK_NAME, USER_NAME, COMPUTE_POOL_NAME, SERVICE_NAME
HAVING SUM(COALESCE(CREDITS, 0)) > 0
    OR SUM(COALESCE(NOTEBOOK_EXECUTION_TIME_SECS, 0)) > 0
ORDER BY CREDITS DESC, EXECUTION_TIME_SEC DESC
"""


def marketplace_paid_usage(days: int, *, bounds: tuple | None = None) -> str:
    """Paid Marketplace charges for this account in their native currency."""
    days = bounded_days(days, 365)
    where = scope_window_where("USAGE_DATE", days, bounds=bounds)
    return f"""
SELECT
    PROVIDER_NAME,
    LISTING_DISPLAY_NAME,
    DATABASE_NAME,
    CHARGE_TYPE,
    CURRENCY,
    SUM(COALESCE(UNITS, 0)) AS UNITS,
    SUM(COALESCE(CHARGE, 0)) AS CHARGE,
    MIN(USAGE_DATE) AS FIRST_USE,
    MAX(USAGE_DATE) AS LAST_USE
FROM SNOWFLAKE.ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY
WHERE {where}
  AND CONSUMER_ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
GROUP BY PROVIDER_NAME, LISTING_DISPLAY_NAME, DATABASE_NAME, CHARGE_TYPE, CURRENCY
HAVING SUM(COALESCE(CHARGE, 0)) <> 0
ORDER BY CHARGE DESC
"""


def storage_by_database(days: int, company: str = "ALL", database: str = "") -> str:
    """Per-database storage on the BILLING basis: the average of daily bytes
    over the window (F1, 2026-07-14). Snowflake bills storage on the monthly
    average of daily on-disk bytes, so the r19 latest-day snapshot over/under-
    stated any database that grew or shrank mid-window. FACT_STORAGE_DAILY
    holds one row per day per database, each already that day's average bytes;
    the page falls back to the _live variant while the fact is empty.
    Mart-backed, so it honors the long window (v4.54)."""
    days = bounded_days(days, 365)
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
HAVING AVG(COALESCE(DB_BYTES, 0)) + AVG(COALESCE(FAILSAFE_BYTES, 0)) > 0
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
HAVING AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) + AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_by_database_calendar(company: str = "ALL", database: str = "", prior: bool = False) -> str:
    """Per-database storage on the CALENDAR-month billing basis (item 7,
    2026-07-14): average of daily bytes over the current month-to-date
    (excluding today's partial day) or the prior completed calendar month.
    Snowflake bills storage on the monthly average of daily on-disk bytes."""
    # Account-tz month anchors (America/Chicago), matching account_today() and the app's
    # other calendar-month surfaces. Session-tz CURRENT_DATE() drifts a day at the month
    # boundary — blanking the current-month panel and mislabeling the prior month, and
    # disagreeing with account-anchored siblings (round-2 bug hunt).
    if prior:
        lo = f"DATE_TRUNC('month', DATEADD('month', -1, {account_today_sql()}))"
        hi = account_month_start_sql()
    else:
        lo = account_month_start_sql()
        hi = account_today_sql()
    where = and_where(f"DAY >= {lo}", f"DAY < {hi}",
                      companies.database_clause(company),
                      companies.database_equals_clause(database))
    # Divide by DAYS-IN-PERIOD, not days-with-a-row: Snowflake's monthly-average
    # billing basis counts a database that existed only part of the window as 0
    # on its absent days. AVG(present days) overstated created/dropped-mid-period
    # databases. DAYS_AVERAGED still exposes how many days actually had a row.
    period = f"NULLIF(DATEDIFF('day', {lo}, {hi}), 0)"
    return f"""
SELECT DATABASE_NAME,
       SUM(COALESCE(DB_BYTES, 0)) / {period}       AS DB_BYTES,
       SUM(COALESCE(FAILSAFE_BYTES, 0)) / {period} AS FAILSAFE_BYTES,
       COUNT(DISTINCT DAY)              AS DAYS_AVERAGED,
       MAX(DAY)                         AS LATEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING SUM(COALESCE(DB_BYTES, 0)) + SUM(COALESCE(FAILSAFE_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_by_database_calendar_live(company: str = "ALL", database: str = "", prior: bool = False) -> str:
    """Live fallback for storage_by_database_calendar (fact empty): same
    calendar-month billing basis from DATABASE_STORAGE_USAGE_HISTORY."""
    # Account-tz month anchors (America/Chicago), matching account_today() and the app's
    # other calendar-month surfaces. Session-tz CURRENT_DATE() drifts a day at the month
    # boundary — blanking the current-month panel and mislabeling the prior month, and
    # disagreeing with account-anchored siblings (round-2 bug hunt).
    if prior:
        lo = f"DATE_TRUNC('month', DATEADD('month', -1, {account_today_sql()}))"
        hi = account_month_start_sql()
    else:
        lo = account_month_start_sql()
        hi = account_today_sql()
    where = and_where(f"USAGE_DATE >= {lo}", f"USAGE_DATE < {hi}",
                      companies.database_clause(company),
                      companies.database_equals_clause(database))
    # Days-in-period denominator, matching storage_by_database_calendar (the mart
    # path) so the two never disagree — see the note there.
    period = f"NULLIF(DATEDIFF('day', {lo}, {hi}), 0)"
    return f"""
SELECT
    DATABASE_NAME,
    SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) / {period} AS DB_BYTES,
    SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) / {period} AS FAILSAFE_BYTES,
    COUNT(DISTINCT USAGE_DATE)               AS DAYS_AVERAGED,
    MAX(USAGE_DATE)                          AS LATEST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
WHERE {where}
GROUP BY DATABASE_NAME
HAVING SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) + SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) > 0
ORDER BY DB_BYTES DESC
"""


def storage_account_truth(days: int, *, bounds: tuple | None = None) -> str:
    """Account-wide storage by tier on the billing basis (F1b/R3, V046):
    average of daily bytes for table, stage, fail-safe, hybrid, and archive
    cool/cold. Account grain only — STORAGE_USAGE (and this fact) carry no
    per-database split for stage/hybrid/archive. Reads FACT_STORAGE_ACCOUNT_
    DAILY; page falls back to the _live variant while the fact is empty."""
    days = bounded_days(days, maximum=400)
    where = scope_window_where("DAY", days, bounds=bounds)
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
WHERE {where}
"""


def storage_account_truth_live(days: int, *, bounds: tuple | None = None) -> str:
    """Live fallback for storage_account_truth from ACCOUNT_USAGE.STORAGE_USAGE
    (F1b/R3, V046). Same monthly-average billing basis; account-wide. Note the
    view is Snowflake's own estimate that will not match the invoice exactly —
    org USAGE_IN_CURRENCY is billing truth.

    Honors ``bounds`` (Last month) via scope_window_where so the live fallback
    covers the SAME bounded calendar month as the fact path — dropping bounds here
    scanned a trailing today-anchored window that mixed the current partial month
    into the figure presented as last month's (bug-hunt round 5)."""
    days = bounded_days(days, maximum=400)
    where = scope_window_where("USAGE_DATE", days, bounds=bounds)
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
WHERE {where}
"""


def object_cost_by_arm(days: int = 30, company: str = "ALL", database: str = "", *, bounds: tuple | None = None) -> str:
    """Object-cost breakdown by cost arm from FACT_OBJECT_COST_DAILY (V048,
    additive; V050 splits query compute by role). QUERY_COMPUTE_WRITE is the
    production share (building the object), QUERY_COMPUTE_READ the consumption
    share — measured compute+QAS split equally across touched objects (legacy
    QUERY_COMPUTE rows predate V050); the rest are direct per-object serverless
    credits (clustering / MV refresh / serverless task / Snowpipe / search-opt).

    #19: honors the global Database filter. FACT_OBJECT_COST_DAILY has no
    DATABASE_NAME column — the object's database is the FIRST label of
    OBJECT_FQN (``db.schema.object``), which is exactly what the V048 loader
    feeds COMPANY_FOR_DATABASE — so we scope on ``SPLIT_PART(OBJECT_FQN, '.', 1)``
    rather than a column that does not exist."""
    days = bounded_days(days, 400)
    comp = "" if str(company).upper() in ("ALL", "") else f"COMPANY = {companies.sql_literal(company)}"
    _db = str(database or "").strip()
    db_pred = (f"UPPER(SPLIT_PART(OBJECT_FQN, '.', 1)) = {companies.sql_literal(_db.upper())}"
               if _db else "")
    where = and_where(scope_window_where("DAY", days, bounds=bounds), comp, db_pred)
    return f"""
SELECT COST_ARM,
       COUNT(DISTINCT OBJECT_FQN) AS OBJECTS,
       ROUND(SUM(COALESCE(CREDITS, 0)), 4) AS CREDITS
FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
WHERE {where}
GROUP BY COST_ARM
ORDER BY CREDITS DESC
"""


def object_cost_top(days: int = 30, company: str = "ALL", limit: int = 25,
                    database: str = "", *, bounds: tuple | None = None) -> str:
    """Top objects by total measured + maintenance credits (V048), with the
    query-compute vs maintenance split per object.

    #19: honors the global Database filter on the object's own database — the
    first label of OBJECT_FQN (see object_cost_by_arm for why SPLIT_PART).

    #48: the label columns are derived deterministically, not via ANY_VALUE.
    ANY_VALUE(COMPANY/DOMAIN) under GROUP BY OBJECT_FQN could return an
    ARBITRARY row's label if the COMPANY_SCOPE mapping (and thus the stored
    per-day COMPANY) changed within the window. COMPANY is recomputed at query
    time from the object's database via COMPANY_FOR_DATABASE — one value per
    FQN, always the CURRENT mapping — and OBJECT_DOMAIN uses MAX() so the same
    object always shows the same label instead of a nondeterministic pick."""
    days = bounded_days(days, 400)
    lim = max(5, min(int(limit or 25), 200))
    comp = "" if str(company).upper() in ("ALL", "") else f"COMPANY = {companies.sql_literal(company)}"
    _db = str(database or "").strip()
    db_pred = (f"UPPER(SPLIT_PART(OBJECT_FQN, '.', 1)) = {companies.sql_literal(_db.upper())}"
               if _db else "")
    # Exclude the synthetic residual row (one per day: OBJECT_FQN='UNATTRIBUTED',
    # COST_ARM='QUERY_COMPUTE_RESIDUAL') from the per-OBJECT top-N: it is a non-object whose
    # query/maint split is $0/$0 yet CREDITS>0, so it can outrank a real table, take the #1 slot,
    # and its entity drill lands on a non-existent Entity 360. The residual stays visible in
    # object_cost_by_arm's arm breakdown (bug-hunt 2026-08-30).
    where = and_where(scope_window_where("DAY", days, bounds=bounds), comp, db_pred,
                      "OBJECT_FQN <> 'UNATTRIBUTED'")
    # COMPANY_FOR_DATABASE on the object's db: deterministic per OBJECT_FQN group
    # (the arg is a pure function of the grouped column), so MAX() just returns
    # that single value while keeping the column legal without a GROUP BY entry.
    _company_expr = companies.database_case_sql("SPLIT_PART(OBJECT_FQN, '.', 1)")
    return f"""
SELECT OBJECT_FQN,
       MAX(OBJECT_DOMAIN) AS OBJECT_DOMAIN,
       MAX({_company_expr}) AS COMPANY,
       ROUND(SUM(IFF(COST_ARM IN ('QUERY_COMPUTE', 'QUERY_COMPUTE_READ', 'QUERY_COMPUTE_WRITE'), CREDITS, 0)), 4) AS QUERY_CREDITS,
       ROUND(SUM(IFF(COST_ARM NOT IN ('QUERY_COMPUTE', 'QUERY_COMPUTE_READ', 'QUERY_COMPUTE_WRITE', 'QUERY_COMPUTE_RESIDUAL'), CREDITS, 0)), 4) AS MAINTENANCE_CREDITS,
       ROUND(SUM(COALESCE(CREDITS, 0)), 4) AS CREDITS
FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
WHERE {where}
GROUP BY OBJECT_FQN
HAVING SUM(COALESCE(CREDITS, 0)) > 0
ORDER BY CREDITS DESC
LIMIT {lim}
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
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
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
    # C7 coverage: sum the contract window via IFF (no WHERE) so SOURCE_FIRST_DAY is
    # the source's own earliest RETAINED day — the retention floor — NOT a
    # contract-filtered MIN (which read a quiet-start contract as no-coverage: the
    # exact r14 #8 trap the mart builder already avoids). The caller flags a gap only
    # when SOURCE_FIRST_DAY is truly after the contract start (retention too short).
    return f"""
SELECT
    SUM(IFF(USAGE_DATE >= DATE '{text}', {_BILLED}, 0)) AS CREDITS_BILLED_TO_DATE,
    MIN(USAGE_DATE) AS SOURCE_FIRST_DAY,
    MAX(USAGE_DATE) AS LAST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
"""


def cloud_services_ratio_by_warehouse(days: int, company: str = "ALL", *,
                                      bounds: tuple | None = None) -> str:
    """Cloud-services share of each warehouse's credits (CoCo's top finding).

    >10% deserves a look, >20% (the alert threshold) usually means many tiny
    queries, metadata-heavy patterns, or compile-heavy SQL.
    """
    days = bounded_days(days)
    where = and_where(
        (resolve_effective_window(days, "START_TIME", bounds=bounds)[1] if bounds is not None
         else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        _wh_company_scope(company),   # MC-1: COMPANY_SCOPE-aware, not name-pattern
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


def compile_heavy_families(days: int, company: str = "ALL", warehouse: str = "",
                           min_runs: int = 20, *, bounds: tuple | None = None) -> str:
    """Query families whose compile time dominates — the usual driver of a
    high cloud-services ratio.

    ``warehouse`` scopes to a single warehouse (the per-warehouse "why is IT
    elevated?" drill); ``min_runs`` is the sample floor (relaxed for a scoped drill
    where one warehouse has fewer runs per family). Account-wide default keeps the
    20-run floor."""
    days = bounded_days(days)
    min_runs = max(1, min(int(min_runs), 100))
    where = and_where(
        (resolve_effective_window(days, "START_TIME", bounds=bounds)[1] if bounds is not None
         else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        # an exact warehouse is already a complete scope; the company predicate
        # (hardcoded tuple/prefix) would only subtract and can zero a warehouse
        # mapped to a company via the runtime COMPANY_SCOPE table.
        _wh_company_scope(company) if not warehouse else "",   # MC-1: COMPANY_SCOPE-aware
        f"WAREHOUSE_NAME = {sql_literal(warehouse)}" if warehouse else "",
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
HAVING COUNT(*) >= {min_runs} AND AVG(COMPILATION_TIME) > 500
ORDER BY SUM(COMPILATION_TIME) DESC
LIMIT 25
"""

def org_account_month_usd(months: int = 2) -> str:
    """This account's org-billed dollars by month on the STRUCTURED billing
    dimensions (item 6, 2026-07-14 — verified against this account's RATING_TYPE
    values, retiring the old USAGE_TYPE string match). COMPUTE_USD =
    RATING_TYPE 'COMPUTE' (warehouse + serverless + cloud services, with the
    cloud-services rebate netted via the IS_ADJUSTMENT rows) — the bucket the
    app's credits x rate models. AI_USD (AI_COMPUTE/AI_INFERENCE), STORAGE_USD,
    TRANSFER_USD, an explicit ADJUSTMENT_USD, and TOTAL_USD (everything) round
    it out.

    Org rate-card = billing truth; residual vs the app model is rate-card
    reality, not a bug in either number. Data is UTC, can lag up to ~72h, and
    mutates until month close; BILLING_TYPE is uniformly 'CONSUMPTION' on this
    account (no overage/free-usage split today).
    """
    months = max(1, min(int(months or 2), 12))
    # rec #29: UPPER(RATING_TYPE) so a differently-cased value cannot silently drop
    # from every named bucket while still landing in TOTAL, and an explicit
    # OTHER_USD residual = everything not in a named rating type (marketplace,
    # priority support / VPS, and any new rating type Snowflake adds). The four
    # named buckets + OTHER_USD sum to TOTAL_USD exactly. ADJUSTMENT_USD is
    # ORTHOGONAL (Codex #5): IS_ADJUSTMENT rows already carry a RATING_TYPE and are
    # counted inside their bucket, so it is a disclosure, never a 6th additive slice.
    return f"""
SELECT
    DATE_TRUNC('month', USAGE_DATE)::DATE AS MONTH,
    SUM(IFF(UPPER(RATING_TYPE) = 'COMPUTE', USAGE_IN_CURRENCY, 0))                          AS COMPUTE_USD,
    SUM(IFF(UPPER(RATING_TYPE) IN ('AI_COMPUTE', 'AI_INFERENCE'), USAGE_IN_CURRENCY, 0))    AS AI_USD,
    SUM(IFF(UPPER(RATING_TYPE) IN ('STORAGE', 'BLOCK_STORAGE'), USAGE_IN_CURRENCY, 0))      AS STORAGE_USD,
    SUM(IFF(UPPER(RATING_TYPE) = 'DATA_TRANSFER', USAGE_IN_CURRENCY, 0))                    AS TRANSFER_USD,
    SUM(IFF(COALESCE(UPPER(RATING_TYPE), '') NOT IN ('COMPUTE', 'AI_COMPUTE', 'AI_INFERENCE',
            'STORAGE', 'BLOCK_STORAGE', 'DATA_TRANSFER'), USAGE_IN_CURRENCY, 0))            AS OTHER_USD,
    SUM(IFF(IS_ADJUSTMENT, USAGE_IN_CURRENCY, 0))                                           AS ADJUSTMENT_USD,
    SUM(USAGE_IN_CURRENCY)                                                                  AS TOTAL_USD,
    MAX(CURRENCY)                                                                           AS CURRENCY
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
  AND USAGE_DATE >= DATE_TRUNC('month', DATEADD('month', -{months - 1}, CURRENT_DATE()))
GROUP BY 1
ORDER BY 1 DESC
"""

def org_all_in_window_usd(days: int, *, bounds: tuple | None = None) -> str:
    """rec #8: this account's ALL-IN billed cost (org rate card) over the last N
    days — the invoice total the metering credit-spend headline omits (storage,
    data transfer, marketplace, org adjustments). Same trailing window as the
    credit-spend tile so the two reconcile side by side. Org data is UTC, lags
    up to ~72h, and mutates until month close, so it reads a bit behind the
    credit tile near the trailing edge.
    """
    days = bounded_days(days)
    where = scope_window_where("USAGE_DATE", days, bounds=bounds)
    return f"""
SELECT
    SUM(USAGE_IN_CURRENCY)                                                              AS TOTAL_USD,
    SUM(IFF(UPPER(RATING_TYPE) = 'COMPUTE', USAGE_IN_CURRENCY, 0))                      AS COMPUTE_USD,
    SUM(IFF(UPPER(RATING_TYPE) IN ('AI_COMPUTE', 'AI_INFERENCE'), USAGE_IN_CURRENCY, 0)) AS AI_USD,
    SUM(IFF(UPPER(RATING_TYPE) IN ('STORAGE', 'BLOCK_STORAGE'), USAGE_IN_CURRENCY, 0))  AS STORAGE_USD,
    SUM(IFF(UPPER(RATING_TYPE) = 'DATA_TRANSFER', USAGE_IN_CURRENCY, 0))                AS TRANSFER_USD,
    SUM(IFF(COALESCE(UPPER(RATING_TYPE), '') NOT IN ('COMPUTE', 'AI_COMPUTE', 'AI_INFERENCE', 'STORAGE',
            'BLOCK_STORAGE', 'DATA_TRANSFER'), USAGE_IN_CURRENCY, 0))                   AS OTHER_USD,
    MAX(CURRENCY)                                                                       AS CURRENCY
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
  AND {where}
"""


def org_rate_sheet() -> str:
    """rec #9: the ACTUAL contracted rate card for this account
    (ORGANIZATION_USAGE.RATE_SHEET_DAILY.EFFECTIVE_RATE, latest per usage type),
    so the configured SETTINGS credit price can be reconciled against the contract
    instead of trusting two hand-entered constants that silently drift at renewal.

    Read-only reconciliation input (not wired into primary pricing — that stays
    the admin-configured SETTINGS rate by design). Needs ORGANIZATION_USAGE
    visibility; callers degrade honestly when the role cannot see the view.
    NOTE: RATE_SHEET_DAILY USAGE_TYPE spellings are account-specific — confirm
    against Snowsight before relying on a specific bucket.
    """
    return """
SELECT UPPER(USAGE_TYPE)              AS USAGE_TYPE,
       MAX_BY(EFFECTIVE_RATE, DATE)   AS EFFECTIVE_RATE,
       MAX_BY(CURRENCY, DATE)         AS CURRENCY,
       MAX(DATE)                      AS AS_OF
FROM SNOWFLAKE.ORGANIZATION_USAGE.RATE_SHEET_DAILY
WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
GROUP BY 1
ORDER BY 1
"""


def tag_coverage(days: int, company: str = "ALL", database: str = "",
                 schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Query-tag governance: execution-time-weighted coverage + the top
    untagged workloads by user. Chargeback precision is capped by this.

    #36: honors the global Database/Schema filter. QUERY_HISTORY carries
    DATABASE_NAME/SCHEMA_NAME, so a scoped chargeback screen stays scoped on
    the live path (the user-grain MART_TAG_COVERAGE_DAILY dropped those columns
    in aggregation, so cost.py routes to this builder whenever a db/schema
    filter is active)."""
    from app.core.sqlsafe import contains_filter
    days = bounded_days(days)
    where = and_where(
        (resolve_effective_window(days, "START_TIME", bounds=bounds)[1] if bounds is not None
         else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        # Scope company by the USER (COMPANY_FOR_USER), NOT the warehouse: this is a USER-grain
        # board and the mart sibling MART_TAG_COVERAGE_DAILY stamps COMPANY = COMPANY_FOR_USER (V095).
        # Scoping the live fallback by warehouse-company returned a DIFFERENT user population than the
        # mart (a user on another company's warehouse flips in/out), so the same company's board and
        # "Tagged share" KPI changed with mart warmth (bug-hunt 2026-08-30).
        companies.user_clause(company, "USER_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    USER_NAME,
    SUM(EXECUTION_TIME) / 1000.0 AS EXEC_SEC,
    SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL, EXECUTION_TIME, 0)) / 1000.0 AS UNTAGGED_EXEC_SEC,
    -- TAGGED_PCT must repeat the SUM expressions, NOT reference the EXEC_SEC /
    -- UNTAGGED_EXEC_SEC aliases: Snowflake forbids lateral column-alias references
    -- inside the SELECT list (it compiles to "invalid identifier"), unlike HAVING /
    -- ORDER BY below where the aliases are allowed. Sibling builders repeat the
    -- aggregate the same way (cloud_services_ratio_by_warehouse). (bug-hunt round 5)
    ROUND(100 * (1 - SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL, EXECUTION_TIME, 0))
                     / NULLIF(SUM(EXECUTION_TIME), 0)), 1) AS TAGGED_PCT,
    COUNT(*) AS QUERIES,
    -- RD-1 (bug-hunt round 13): account-wide totals over the FULL post-HAVING
    -- population (every user above the 60s floor), computed as a window SUM of the
    -- group SUMs so they are evaluated BEFORE the LIMIT 30. The rolled-up "Tagged
    -- share" KPI reads these instead of summing the top-30-by-untagged display frame,
    -- which shrank the denominator far more than the numerator and biased the share
    -- LOW (a false ok->warn flip). These repeat the aggregate (no sibling-alias ref).
    SUM(SUM(EXECUTION_TIME)) OVER () / 1000.0 AS TOTAL_EXEC_SEC,
    SUM(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL, EXECUTION_TIME, 0))) OVER () / 1000.0 AS TOTAL_UNTAGGED_EXEC_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY USER_NAME
HAVING EXEC_SEC > 60
ORDER BY UNTAGGED_EXEC_SEC DESC
LIMIT 30
"""


def untagged_executions_for_user(user_name: str, days: int, company: str = "ALL",
                                 database: str = "", schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Per-user drill for tag_coverage: the top untagged statement TYPES for ONE
    user, so a chargeback owner can see WHAT is running without a QUERY_TAG.

    Reuses tag_coverage's EXACT predicate (WAREHOUSE_NAME IS NOT NULL,
    COALESCE(EXECUTION_TIME,0) > 0, NULLIF(QUERY_TAG,'') IS NULL) and the same
    company/db/schema scoping, plus an exact USER_NAME match, so it can't widen
    scope. Grouped by QUERY_TYPE. NOTE: this is a LIVE scan capped at ~90d
    (bounded_days); when the parent scoreboard is mart-served over a longer window
    the summed UNTAGGED_EXEC_SEC is a recent-90d subset, not a full reconciliation
    — the UI captions that."""
    from app.core.sqlsafe import contains_filter
    days = bounded_days(days)
    where = and_where(
        (resolve_effective_window(days, "START_TIME", bounds=bounds)[1] if bounds is not None
         else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        # user-company scope, matching the tag_coverage scoreboard this drills from (bug-hunt
        # 2026-08-30): a warehouse-company scope would drop the drill's rows for a user who ran
        # on another company's warehouse -- the exact user the scoreboard now includes.
        companies.user_clause(company, "USER_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        f"USER_NAME = {sql_literal(user_name)}",
        "NULLIF(QUERY_TAG, '') IS NULL",
    )
    return f"""
SELECT
    QUERY_TYPE,
    COUNT(*) AS QUERIES,
    ROUND(SUM(EXECUTION_TIME) / 1000.0, 1) AS UNTAGGED_EXEC_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY QUERY_TYPE
ORDER BY UNTAGGED_EXEC_SEC DESC
LIMIT 20
"""


def cs_by_query_type(days: int, company: str = "ALL", warehouse: str = "", *, bounds: tuple | None = None) -> str:
    """Cloud-services credits by statement type (COST_DB recon R6) — makes
    metadata storms (SHOW/DESCRIBE floods) visible beside the compile-heavy
    families when the CS ratio is ELEVATED. ``warehouse`` scopes to one warehouse
    for the per-warehouse elevation drill."""
    days = bounded_days(days)
    where = and_where(
        (resolve_effective_window(days, "START_TIME", bounds=bounds)[1] if bounds is not None
         else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"),
        "CREDITS_USED_CLOUD_SERVICES > 0",
        # exact warehouse is a complete scope (see compile_heavy_families) — the
        # company predicate would only subtract and can zero a runtime-mapped warehouse.
        _wh_company_scope(company) if not warehouse else "",   # MC-1: COMPANY_SCOPE-aware
        f"WAREHOUSE_NAME = {sql_literal(warehouse)}" if warehouse else "",
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


def object_cost_recon(days: int = 7) -> str:
    """Object-ledger reconciliation (Codex #7 app slice, v4.52): the additive
    contract, checked. Query arms + residual must equal QAH's credits for the
    same window; each maintenance arm must equal its source history. Window is
    lag-safe — it ends 2 days ago (QAH ~8h lag + the 06:45 daily load), so a
    clean account reads DELTA ~0 and drift means late-arriving attribution or
    a loader defect. Day attribution mirrors the loader exactly: per-query
    MIN(START_TIME)::DATE for the query row, START_TIME::DATE + CREDITS_USED>0
    for the maintenance arms."""
    days = bounded_days(days, 14)
    win = (f"BETWEEN DATEADD('day', -{days + 2}, CURRENT_DATE()) "
           "AND DATEADD('day', -2, CURRENT_DATE())")
    return f"""
WITH qa AS (
    SELECT QUERY_ID, MIN(START_TIME)::DATE AS DAY,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 3}, CURRENT_DATE())
    GROUP BY QUERY_ID
    HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
),
ledger AS (
    SELECT COST_ARM, SUM(COALESCE(CREDITS, 0)) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
    WHERE DAY {win}
    GROUP BY COST_ARM
),
checks AS (
    SELECT 'QUERY_ARMS_VS_QAH' AS CHECK_NAME,
           (SELECT SUM(CREDITS) FROM ledger
             WHERE COST_ARM IN ('QUERY_COMPUTE', 'QUERY_COMPUTE_READ',
                                'QUERY_COMPUTE_WRITE', 'QUERY_COMPUTE_RESIDUAL')) AS LEDGER_CREDITS,
           (SELECT SUM(CREDITS) FROM qa WHERE DAY {win}) AS SOURCE_CREDITS
    UNION ALL
    SELECT 'CLUSTERING',
           (SELECT SUM(CREDITS) FROM ledger WHERE COST_ARM = 'CLUSTERING'),
           (SELECT SUM(COALESCE(CREDITS_USED, 0)) FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
             WHERE START_TIME::DATE {win} AND CREDITS_USED > 0)
    UNION ALL
    SELECT 'MV_REFRESH',
           (SELECT SUM(CREDITS) FROM ledger WHERE COST_ARM = 'MV_REFRESH'),
           (SELECT SUM(COALESCE(CREDITS_USED, 0)) FROM SNOWFLAKE.ACCOUNT_USAGE.MATERIALIZED_VIEW_REFRESH_HISTORY
             WHERE START_TIME::DATE {win} AND CREDITS_USED > 0)
    UNION ALL
    SELECT 'SEARCH_OPT',
           (SELECT SUM(CREDITS) FROM ledger WHERE COST_ARM = 'SEARCH_OPT'),
           (SELECT SUM(COALESCE(CREDITS_USED, 0)) FROM SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY
             WHERE START_TIME::DATE {win} AND CREDITS_USED > 0)
    UNION ALL
    SELECT 'SERVERLESS_TASK',
           (SELECT SUM(CREDITS) FROM ledger WHERE COST_ARM = 'SERVERLESS_TASK'),
           (SELECT SUM(COALESCE(CREDITS_USED, 0)) FROM SNOWFLAKE.ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY
             WHERE START_TIME::DATE {win} AND CREDITS_USED > 0)
    UNION ALL
    SELECT 'SNOWPIPE',
           (SELECT SUM(CREDITS) FROM ledger WHERE COST_ARM = 'SNOWPIPE'),
           (SELECT SUM(COALESCE(CREDITS_USED, 0)) FROM SNOWFLAKE.ACCOUNT_USAGE.PIPE_USAGE_HISTORY
             WHERE START_TIME::DATE {win} AND CREDITS_USED > 0)
)
SELECT CHECK_NAME,
       ROUND(COALESCE(LEDGER_CREDITS, 0), 4) AS LEDGER_CREDITS,
       ROUND(COALESCE(SOURCE_CREDITS, 0), 4) AS SOURCE_CREDITS,
       ROUND(COALESCE(LEDGER_CREDITS, 0) - COALESCE(SOURCE_CREDITS, 0), 4) AS DELTA_CREDITS,
       ROUND(100 * (COALESCE(LEDGER_CREDITS, 0) - COALESCE(SOURCE_CREDITS, 0))
             / NULLIF(SOURCE_CREDITS, 0), 2) AS DELTA_PCT
FROM checks
ORDER BY CHECK_NAME
"""


def native_anomaly_insights() -> str:
    """Snowflake's managed ML cost-anomaly feed (repo review wave 2) — an
    INDEPENDENT second opinion beside the app's z-score sweep. SNOWFLAKE.LOCAL
    schema, fires daily when the native model flags account/org spend.

    OPTIONAL and schema-uncertain — SELECT * on purpose (rendering whatever the
    feed carries is the honest MVP; guessing columns would land wrong data, a
    worse failure than a raw table). Callers MUST pass probe=True; LIMIT bounds
    the read."""
    return """
SELECT *
FROM SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS
LIMIT 200
"""
