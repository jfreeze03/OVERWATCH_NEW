"""Readers for OVERWATCH's own Snowflake objects (marts, facts, ops tables).

Object shapes are defined by snowflake/migrations/. These builders read only;
lifecycle INSERT/UPDATE statements are built in the pages that own them.
"""

from __future__ import annotations

from app.config import (
    CORE_SCHEMA,
    CURRENT_MONTH_WINDOW,
    CURRENT_YEAR_WINDOW,
    LEDGER_AUTOBOOKED_LEVERS,
    LEDGER_TWIN_MATCH_DAYS,
    MAX_MART_WINDOW_DAYS,
    OVERWATCH_DB,
    SAVINGS_ACTIVE_MONTHS,
    THRESHOLDS,
    core_object,
    mart_object,
)
from app.core.sqlsafe import contains_filter, sql_literal
from app.data.common import (
    account_month_start_sql,
    account_today_sql,
    ai_service_predicate,
    and_where,
    app_self_sql,
    bounded_days,
    cs_by_query_type_projection,
    not_ai_service_predicate,
    resolve_effective_window,
    scope_window_where,
)


def _company_filter(company: str) -> str:
    value = str(company or "ALL")
    if value.upper() == "ALL":
        return f"COMPANY = {sql_literal('ALL')}"
    return f"COMPANY = {sql_literal(value)}"


def exec_board(company: str, days: int, window: object = None) -> str:
    """First-paint executive board rows for one company scope and window."""
    # Mart read: honor the long window (MART_EXEC_BOARD holds 180/365 rows,
    # V052/V054). The live-scan default (90) would silently read the 90-day
    # board rows under a 180/365 label — the KPIs, spend trend, and cost drivers
    # would all show 90-day data. The page filter constrains days to
    # DAY_WINDOW_OPTIONS, so WINDOW_DAYS always resolves to a populated board row.
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    # Calendar-preset WINDOW_DAYS keys off the ACCOUNT clock (America/Chicago), matching
    # the V123 loader (SP_REFRESH_EXEC_BOARD) and every other account_today-anchored
    # calendar-month surface (health-strip MTD, storage calendar, DS quarter, MTD pace
    # KPI). Session/UTC CURRENT_DATE() drifted one day from those each evening past
    # Chicago midnight, so the board's days-into-month and the reader<->loader key could
    # disagree (blank board / a "this month" differing from the pace KPI by a month).
    _acct_today = account_today_sql()
    if window == CURRENT_MONTH_WINDOW:
        window_clause = (
            f"WINDOW_DAYS = DATEDIFF('day', DATE_TRUNC('month', {_acct_today}), {_acct_today})"
        )
    elif window == CURRENT_YEAR_WINDOW:
        window_clause = (
            f"WINDOW_DAYS = DATEDIFF('day', DATE_TRUNC('year', {_acct_today}), {_acct_today})"
        )
    else:
        window_clause = f"WINDOW_DAYS = {days}"
    where = and_where(_company_filter(company), window_clause)
    return f"""
SELECT PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER, REFRESHED_AT
FROM {mart_object("MART_EXEC_BOARD")}
WHERE {where}
ORDER BY PANEL, SORT_ORDER, PERIOD_START
"""


def source_freshness() -> str:
    return f"SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, HOURS_SINCE_LOAD FROM {mart_object('MART_SOURCE_FRESHNESS')} ORDER BY SOURCE_NAME"


def fact_metering_by_service(days: int, *, bounds: tuple | None = None) -> str:
    """Spend-tab hot path: same output shape as the live metering reader,
    served from the hourly-loaded fact instead of ACCOUNT_USAGE.

    'Last month' passes an explicit (start, end_exclusive) calendar range via ``bounds``;
    every other window keeps the trailing day-offset. This is THE headline spend number
    (total credits, spend-by-service, daily trend), so it honors the bounded range."""
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    where = (resolve_effective_window(days, "DAY", max_days=MAX_MART_WINDOW_DAYS, bounds=bounds)[1]
             if bounds is not None
             else f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())")
    return f"""
SELECT DAY, SERVICE_TYPE, CREDITS_USED, CREDITS_BILLED, CREDITS_ADJUSTMENT
FROM {mart_object("FACT_METERING_DAILY")}
WHERE {where}
ORDER BY DAY, SERVICE_TYPE
"""


def fact_query_window_summary(days: int, company: str = "ALL", warehouse_contains: str = "",
                              user_contains: str = "", database: str = "", *,
                              bounds: tuple | None = None) -> str:
    """Ops Queries-tab hot path from FACT_QUERY_HOURLY.

    Counts, failures, queued time and spill are exact sums of the hourly
    fact. P95 is the PEAK hourly-group p95 (a true p95 needs raw rows) —
    the UI labels it as such. No schema dimension in the fact, so callers
    fall back to live when a schema filter is active.
    """
    from app import companies
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    # triage #12: midnight-aligned window (CURRENT_DATE) to match the live twin
    # ops_sql.query_window_summary and fact_warehouse_pressure, so the same
    # "24h"/"Nd" tile covers the same span whichever source serves it.
    where = [scope_window_where("HOUR_TS", days, bounds=bounds)]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    where.append(contains_filter("WAREHOUSE_NAME", warehouse_contains))
    where.append(contains_filter("USER_NAME", user_contains))
    where.append(companies.database_equals_clause(database, "DATABASE_NAME"))
    return f"""
SELECT
    SUM(QUERY_COUNT) AS QUERY_COUNT,
    SUM(FAILED_COUNT) AS FAILED_COUNT,
    MAX(P95_ELAPSED_SEC) AS P95_ELAPSED_SEC,
    SUM(QUEUED_SEC_SUM) AS QUEUED_SEC,
    SUM(SPILL_REMOTE_GB) AS SPILL_REMOTE_GB
FROM {mart_object("FACT_QUERY_HOURLY")}
WHERE {and_where(*where)}
"""


def app_statement_stats(days: int = 7) -> str:
    """The app's own slowest tagged statement families on its shared warehouse.

    Groups by QUERY_PARAMETERIZED_HASH so each app query pattern (all pages,
    all filter values) collapses to one row — the honest way to find which
    builder to optimize next.
    """
    from app.config import APP_WAREHOUSE

    days = bounded_days(days, 30)
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT,
    COUNT(*) AS RUNS,
    COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
    ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
    ROUND(AVG(BYTES_SCANNED) / POWER(1024, 3), 3) AS AVG_GB_SCANNED,
    MAX_BY(QUERY_ID, TOTAL_ELAPSED_TIME) AS SLOWEST_QUERY_ID
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
  AND {app_self_sql()}
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY P95_S DESC
LIMIT 30
"""


def app_statement_stats_telemetry(days: int = 7) -> str:
    """P11: the app's own slowest statement families, from the app's OWN
    telemetry instead of a QUERY_HISTORY scan.

    app_statement_stats() answers the same question by scanning
    ACCOUNT_USAGE.QUERY_HISTORY for a week of tagged WH_ALFA_ADMIN traffic — 4.6s, and
    ironically the Admin > Performance panel's own worst offender. APP_QUERY_TELEMETRY
    already records every fetch the app decided to keep, keyed by PAGE + QUERY_KEY,
    which is a BETTER grain than QUERY_PARAMETERIZED_HASH for "which builder do I
    optimize": the key names the call site.

    What this cannot give is BYTES_SCANNED — that only exists in QUERY_HISTORY —
    so the scanning builder stays available behind an explicit toggle for the
    GB figure. Note the sampling: RUNS is a persisted count (slow/failed always,
    ~2% of healthy), EST_RUNS re-weights it by 1/SAMPLE_PROB, and MEDIAN_S is
    therefore exception-weighted — the same caveat the fleet panels carry.
    """
    days = bounded_days(days, 90)
    return f"""
SELECT
    PAGE,
    QUERY_KEY,
    COUNT(*) AS RUNS,
    ROUND(SUM(1.0 / COALESCE(SAMPLE_PROB, 1.0))) AS EST_RUNS,
    COUNT_IF(NOT OK) AS FAILS,
    ROUND(MEDIAN(ELAPSED_MS) / 1000, 2) AS MEDIAN_S,
    ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.95) / 1000, 2) AS P95_S,
    ROUND(SUM(ELAPSED_MS / 1000.0 / COALESCE(SAMPLE_PROB, 1.0)), 1) AS EST_WAIT_S,
    -- #30: WEIGHTED cache-hit rate (match app_query_fleet_summary ~1573). An unweighted
    -- AVG over this exception-biased sample collapses toward 0: the must-persist stream is
    -- almost all cache MISSES (a hit rarely crosses the 2s persist bar), swamping the
    -- 2%-sampled healthy hits. Weighting each non-null row by 1/SAMPLE_PROB restores the
    -- true fleet rate. NULL cache_hit rows (pre-rider) drop out of both sums; NULLIF guards
    -- a page with no such rows. Pre-V064 rows read SAMPLE_PROB NULL -> weight 1 (safe).
    ROUND(
        SUM(IFF(CACHE_HIT IS NULL, 0, IFF(CACHE_HIT, 1, 0) / COALESCE(SAMPLE_PROB, 1.0)))
        / NULLIF(SUM(IFF(CACHE_HIT IS NULL, 0, 1.0 / COALESCE(SAMPLE_PROB, 1.0))), 0)
        * 100, 1) AS CACHE_HIT_PCT,
    MAX(AT) AS NEWEST,
    -- Exact representative, not an arbitrary sample. NULL means every persisted
    -- row was a cache hit/pre-V064 row and therefore had no server query.
    MAX_BY(QUERY_ID, IFF(QUERY_ID IS NOT NULL, ELAPSED_MS, NULL)) AS SLOWEST_QUERY_ID
FROM {core_object("APP_QUERY_TELEMETRY")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  -- P3: the batch wall-clock rows are a superset of their 'batch:%' members;
  -- they are not a builder anyone can optimize, and they would double-count wait.
  AND NOT STARTSWITH(QUERY_KEY, 'batch_wall:')
GROUP BY PAGE, QUERY_KEY
ORDER BY EST_WAIT_S DESC NULLS LAST
LIMIT 30
"""


# Canonical AI/Cortex service-type predicate over FACT_METERING_DAILY.SERVICE_TYPE
# — the exact one proven in fact_cortex_daily_spend / fact_daily_spend_compute.
# NULL SERVICE_TYPE evaluates NULL here, so the CASE below routes it to OTHER
# (compute), matching fact_daily_spend_compute's COALESCE(...,'') NOT ILIKE arm.
_AI_SERVICE_PRED = ai_service_predicate()  # v4.158.0: canonical, incl. CoCo/CoWork


def _billed_split_cols(credits_col: str = "CREDITS_BILLED") -> str:
    """SELECT fragment splitting billed credits into AI vs OTHER partitions.

    Cost review C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the
    compute rate, so every all-service rollup carries the split and the caller
    dollarizes with formulas.blended_billed_usd. AI + OTHER = total by
    construction (every row lands in exactly one bucket)."""
    return (
        f"SUM({credits_col}) AS CREDITS_BILLED, "
        f"SUM(CASE WHEN {_AI_SERVICE_PRED} THEN {credits_col} ELSE 0 END) AS CREDITS_BILLED_AI, "
        f"SUM(CASE WHEN {_AI_SERVICE_PRED} THEN 0 ELSE {credits_col} END) AS CREDITS_BILLED_OTHER"
    )


def billed_split(days: int = 30, *, bounds: tuple | None = None) -> str:
    """rec29: windowed billed credits split into AI vs OTHER partitions (account-
    wide — billing carries no company grain) so the Cost Truth BILLED basis
    dollarizes with the house rate mix (AI/Cortex at ai_rate, compute at the
    compute rate) instead of a naive single rate. Feeds blended_billed_usd."""
    horizon = bounded_days(days, 400)
    return f"""
SELECT {_billed_split_cols()}
FROM {core_object('FACT_METERING_DAILY')}
WHERE {scope_window_where('DAY', horizon, bounds=bounds)}
"""


def fact_daily_spend_year() -> str:
    """Calendar-year billed credits per day (COST_DB recon R9). Own builder:
    bounded_days clamps at 90 by default, which would silently turn "YTD"
    into "last 90 days" in the back half of a year."""
    return f"""
SELECT DAY, {_billed_split_cols()}
FROM {mart_object("FACT_METERING_DAILY")}
-- r30 #1: anchor the year boundary to the ACCOUNT clock, matching the MTD (account_
-- month_start_sql) and quarter (account_today_sql) sibling builders. Session-tz
-- CURRENT_DATE() is UTC under SiS, so on New Year's Eve evening (America/Chicago) it
-- already reads Jan 1 and DATE_TRUNC('year', ...) jumped to the new year -> the YTD
-- chart went empty for the last ~6 hours of the year.
WHERE DAY >= DATE_TRUNC('year', {account_today_sql()})
GROUP BY DAY
ORDER BY DAY
"""


# PERF #46: the single wide window every daily-spend caller shares. Must cover the
# largest caller (Overview forecast backtest = 150). FACT_METERING_DAILY is daily-grain,
# so a smaller window is a strict suffix — callers slice the wide frame client-side
# (formulas.daily_spend_last_n) instead of issuing a distinct scan per window.
WIDE_DAILY_SPEND_DAYS = 150


def fact_daily_spend(days: int) -> str:
    """Account billed credits per day from the daily fact (adjustment applied).

    Emits the AI/OTHER split (C1) alongside the CREDITS_BILLED total so
    consumers can price AI credits at the AI rate; total is unchanged."""
    # Mart read (FACT_METERING_DAILY, long retention): honor the long window so
    # the forecast backtest labeled "3-month" (fact_daily_spend(150)) is not
    # silently clamped to 90 days.
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    return f"""
SELECT DAY, {_billed_split_cols()}
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY DAY
ORDER BY DAY
"""


def fact_daily_spend_compute(days: int) -> str:
    """Account billed credits per day, COMPUTE ONLY (excludes AI/Cortex) — for the
    rate-card reconciliation, whose org side (COMPUTE_USD) also excludes AI, so the
    model and org sides compare like for like (triage #6). Negation of the AI
    predicate in fact_cortex_daily_spend; COALESCE keeps NULL-service rows as
    compute."""
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    return f"""
SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
  AND {not_ai_service_predicate()}
GROUP BY DAY
ORDER BY DAY
"""


def fact_warehouse_daily(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    # perf: honor a bounded calendar window (Last month) via scope_window_where — the day-grain
    # fact CAN express a [start, end) month (unlike the trailing-window-keyed exec_board), so
    # Overview's Last-month daily-spend read can serve from this mart instead of a live
    # WAREHOUSE_METERING_HISTORY scan. The trailing (bounds=None) predicate stays byte-identical.
    where = [scope_window_where("DAY", days, bounds=bounds)]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
ORDER BY DAY
"""


def fact_task_daily(days: int, company: str = "ALL", database: str = "",
                    schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    where = [scope_window_where("DAY", days, bounds=bounds)]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    if str(database or "").strip():
        where.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    # Task Health must honor the Schema filter its section contract promises — the
    # mart carries SCHEMA_NAME at task grain, so filter on it exactly as the live
    # fallback (ops_sql.task_runs) does, or the two paths yield different failure
    # counts for the same filter. contains_filter() returns '' when schema is off.
    schema_clause = contains_filter("SCHEMA_NAME", schema_contains)
    if schema_clause:
        where.append(schema_clause)
    return f"""
SELECT DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR,
       -- r8: uncapped window totals (computed server-side BEFORE run()'s 5000-row transport cap).
       -- This frame is (DAY x TASK) grain ordered FAILED DESC, so a >5000-row truncation would drop
       -- the zero-failure high-volume task-days and make a plain df["RUNS"].sum() UNDERCOUNT runs +
       -- INFLATE the fail-rate. The KPI reads these totals (mirrors the r7 live-path task_runs fix).
       SUM(RUNS) OVER () AS TOTAL_RUNS_WIN,
       SUM(FAILED) OVER () AS TOTAL_FAILED_WIN
FROM {mart_object("FACT_TASK_DAILY")}
WHERE {and_where(*where)}
ORDER BY FAILED DESC, DAY DESC
"""


def fact_warehouse_window_vs_prior(days: int, company: str = "ALL", *,
                                   bounds: tuple | None = None) -> str:
    """Window-vs-prior warehouse credits from FACT_WAREHOUSE_DAILY.

    Same output contract as cost_sql.warehouse_window_vs_prior but reads the
    hourly-loaded fact instead of scanning WAREHOUSE_METERING_HISTORY live
    (perf pass: Control Room movers). Inherits up-to-an-hour loader lag —
    callers keep the live builder as fallback and label the source.

    For 'Last month' (``bounds``), CURRENT is the previous calendar month and PRIOR
    is the month before it (an equal *calendar* period, not an equal day-count), so
    the mover reads as 'August vs July' rather than 'last 31 days vs the 31 before'.
    """
    # This builder scans 2*days (current + prior equal windows). Cap at HALF the
    # mart max so the pair stays within retention and the prior window has data —
    # a naive bump to 365 would scan 730d and read the (empty, post-rebuild) prior
    # half as a false 100% swing (audit Batch A verify). rec 4: the CURRENT window
    # [today-days, today) IS the shared effective window (resolve_effective_window)
    # the allocation shares must span, so pool dollars and share denominators reconcile.
    if bounds is not None:
        cur_start, cur_end = bounds
        prior_start = (cur_start.replace(year=cur_start.year - 1, month=12)
                       if cur_start.month == 1
                       else cur_start.replace(month=cur_start.month - 1))
        where = [f"DAY >= '{prior_start.isoformat()}'", f"DAY < '{cur_end.isoformat()}'"]
        cur_from = f"DAY >= '{cur_start.isoformat()}'"
        prior_to = f"DAY < '{cur_start.isoformat()}'"
    else:
        days, _ = resolve_effective_window(days)   # clamped MAX_MART_WINDOW_DAYS // 2, today-excluded
        where = [f"DAY >= DATEADD('day', -{2 * days}, CURRENT_DATE())",
                 "DAY < CURRENT_DATE()"]   # equal-length windows, exclude today (Codex P0-3)
        cur_from = f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"
        prior_to = f"DAY < DATEADD('day', -{days}, CURRENT_DATE())"
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT
    WAREHOUSE_NAME,
    COMPANY,
    ROUND(SUM(IFF({cur_from}, CREDITS_TOTAL, 0)), 4) AS CREDITS_CURRENT,
    ROUND(SUM(IFF({prior_to}, CREDITS_TOTAL, 0)), 4) AS CREDITS_PRIOR
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
GROUP BY 1, 2
HAVING CREDITS_CURRENT > 0 OR CREDITS_PRIOR > 0
ORDER BY CREDITS_CURRENT DESC
LIMIT 500
"""


def fact_cloud_services_ratio(days: int, company: str = "ALL", *,
                              bounds: tuple | None = None) -> str:
    """Cloud-services share per warehouse from FACT_WAREHOUSE_DAILY.

    The fact already stores CREDITS_TOTAL and CREDITS_COMPUTE, so cloud
    services = TOTAL - COMPUTE — same thresholds as the live builder, no
    schema change needed (Codex #6, improved: they assumed a migration).
    Daily grain vs the live builder's hourly precision; identical for the
    windowed ratio this panel shows.
    """
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    where = [scope_window_where("DAY", days, bounds=bounds),
             # triage #8: match the live builder — drop the CLOUD_SERVICES_ONLY
             # pseudo-warehouse (a 100%-CS metadata bucket that sorts first and
             # spuriously triggers the compile-heavy drill); the >= 0.5 HAVING
             # below drops near-idle warehouses, also matching live.
             "UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT
    WAREHOUSE_NAME,
    ROUND(SUM(CREDITS_COMPUTE), 2) AS COMPUTE_CREDITS,
    ROUND(SUM(CREDITS_TOTAL - CREDITS_COMPUTE), 2) AS CLOUD_SVC_CREDITS,
    ROUND(SUM(CREDITS_TOTAL), 2) AS TOTAL_CREDITS,
    ROUND(SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) * 100, 1) AS CLOUD_SVC_PCT,
    CASE
        WHEN SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) > 0.20 THEN 'ELEVATED'
        WHEN SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) > 0.10 THEN 'WATCH'
        ELSE 'NORMAL'
    END AS STATUS
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
GROUP BY 1
HAVING SUM(CREDITS_TOTAL) >= 0.5
ORDER BY CLOUD_SVC_PCT DESC
LIMIT 500
"""


def _cloud_svc_where(days: int, company: str, warehouse: str, *, bounds: tuple | None = None) -> str:
    where = [resolve_effective_window(days, "DAY", max_days=MAX_MART_WINDOW_DAYS, bounds=bounds)[1]
             if bounds is not None
             else f"DAY >= DATEADD('day', -{bounded_days(days, MAX_MART_WINDOW_DAYS)}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    if str(warehouse or "").strip():
        where.append(f"WAREHOUSE_NAME = {sql_literal(str(warehouse).strip())}")
    return and_where(*where)


def cloud_svc_top_shapes(days: int, company: str = "ALL", warehouse: str = "", *, bounds: tuple | None = None) -> str:
    """Top query shapes by cloud-services credits (V055, MART_CLOUD_SVC_DAILY).

    The shape-grain lens the compile-heavy view misses: a metadata storm of tiny
    SHOW/DESCRIBE queries has near-zero compile time but can dominate CS credits.
    RUNS / CS-per-1k-runs / avg exec / cache% expose which pattern to throttle.
    """
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(QUERY_TYPE) AS QUERY_TYPE,
    ANY_VALUE(SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(RUNS) AS RUNS,
    ROUND(SUM(CS_CREDITS), 4) AS CS_CREDITS,
    ROUND(SUM(CS_CREDITS) / NULLIF(SUM(RUNS), 0) * 1000, 4) AS CS_CREDITS_PER_1K,
    ROUND(SUM(EXEC_SEC_SUM) / NULLIF(SUM(RUNS), 0), 3) AS AVG_EXEC_S,
    ROUND(SUM(CACHE_PCT_SUM) / NULLIF(SUM(RUNS), 0) * 100, 0) AS AVG_CACHE_PCT
FROM {mart_object("MART_CLOUD_SVC_DAILY")}
WHERE {_cloud_svc_where(days, company, warehouse, bounds=bounds)}
GROUP BY QUERY_PARAMETERIZED_HASH
ORDER BY CS_CREDITS DESC
LIMIT 30
"""


def cloud_svc_by_user(days: int, company: str = "ALL", warehouse: str = "", *, bounds: tuple | None = None) -> str:
    """Cloud-services credits by user/role (V055) — who (or which tool) drives
    the ratio. A service account topping this list points at the fix (throttle
    its polling / batch its DML / consolidate its metadata calls)."""
    return f"""
SELECT
    USER_NAME,
    ANY_VALUE(ROLE_NAME) AS ROLE_NAME,
    SUM(RUNS) AS RUNS,
    ROUND(SUM(CS_CREDITS), 4) AS CS_CREDITS,
    ROUND(SUM(CS_CREDITS) / NULLIF(SUM(RUNS), 0) * 1000, 4) AS CS_CREDITS_PER_1K
FROM {mart_object("MART_CLOUD_SVC_DAILY")}
WHERE {_cloud_svc_where(days, company, warehouse, bounds=bounds)}
GROUP BY USER_NAME
ORDER BY CS_CREDITS DESC
LIMIT 25
"""


def cs_by_query_type_mart(days: int, company: str = "ALL", warehouse: str = "",
                          *, bounds: tuple | None = None) -> str:
    """K2: cost_sql.cs_by_query_type served from MART_CLOUD_SVC_DAILY.

    Byte-identical output contract to the live builder — QUERY_TYPE, QUERIES,
    CS_CREDITS, CS_CREDITS_PER_1K, same ORDER BY / LIMIT 12 — so spend.py can
    put the two behind run_mart_first without touching the render. ``warehouse``
    scopes to one warehouse for the per-warehouse elevation drill (MART_CLOUD_SVC_DAILY
    carries WAREHOUSE_NAME); as with the live builder, callers pass company='ALL' with a
    warehouse so the company predicate — which the live path drops for an exact warehouse —
    is not AND-ed in.

    Two honest differences from the live path, both in the mart's favour:
    the mart is loaded from the QH extract at CS>0 only (matching the live
    ``CREDITS_USED_CLOUD_SERVICES > 0`` filter) and its COMPANY column comes
    from COMPANY_FOR_WAREHOUSE at load time rather than the app's warehouse
    name-pattern clause — the loader's mapping is the authoritative one. The
    mart also reaches MAX_MART_WINDOW_DAYS where the live scan clamps at 90,
    which is exactly why callers must read the served window via
    components.served_days() instead of assuming the requested one.
    """
    return cs_by_query_type_projection(
        "SUM(RUNS)",
        "SUM(CS_CREDITS)",
        mart_object("MART_CLOUD_SVC_DAILY"),
        _cloud_svc_where(days, company, warehouse, bounds=bounds),
    )


def open_alert_events(limit: int = 200, company: str = "ALL") -> str:
    """Open/ack events, most severe first, honoring the company filter.

    ``company`` keeps that company's rows PLUS account-level rows
    (COMPANY = 'ALL'): an account-wide fire (daily credit cap, telemetry
    stall) belongs on everyone's triage view, but Trexis warehouse noise
    must not surface under an ALFA scope (live finding, 2026-07-08).
    """
    limit = max(1, min(int(limit), 1000))
    where = ["STATUS IN ('OPEN', 'ACK')"]
    comp = str(company or "ALL").strip()
    if comp.upper() != "ALL":
        where.append(f"(COMPANY = {sql_literal(comp)} OR UPPER(COMPANY) = 'ALL')")
    return f"""
SELECT EVENT_ID, RULE_ID, RAISED_AT, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, STATUS, ACK_BY, ACK_AT
FROM {core_object("ALERT_EVENTS")}
WHERE {and_where(*where)}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, RAISED_AT DESC
LIMIT {limit}
"""


def snoozed_alert_events(limit: int = 100, company: str = "ALL") -> str:
    """V086: currently-snoozed events + their wake times — the Snoozed view and the
    early un-snooze surface. Same company scope as the open feed. References the V086
    SNOOZE_* columns, so callers probe-gate it (it degrades to empty pre-V086, where
    no SNOOZED rows exist anyway); the main triage feed is unchanged."""
    limit = max(1, min(int(limit), 500))
    where = ["STATUS = 'SNOOZED'"]
    comp = str(company or "ALL").strip()
    if comp.upper() != "ALL":
        where.append(f"(COMPANY = {sql_literal(comp)} OR UPPER(COMPANY) = 'ALL')")
    return f"""
SELECT EVENT_ID, RULE_ID, RAISED_AT, COMPANY, SEVERITY, TITLE,
       SNOOZED_UNTIL, SNOOZE_BY, SNOOZE_REASON
FROM {core_object("ALERT_EVENTS")}
WHERE {and_where(*where)}
ORDER BY SNOOZED_UNTIL
LIMIT {limit}
"""


def open_alert_severity_counts(company: str = "ALL") -> str:
    """C4/C7: TRUE open+ack counts by severity in ONE aggregate row, so the Alerts
    KPI tiles never undercount when a storm exceeds the 500-row feed cap. Same
    STATUS + company predicate as open_alert_events() so the numbers reconcile."""
    where = ["STATUS IN ('OPEN', 'ACK')"]
    comp = str(company or "ALL").strip()
    if comp.upper() != "ALL":
        where.append(f"(COMPANY = {sql_literal(comp)} OR UPPER(COMPANY) = 'ALL')")
    return f"""
SELECT
    COUNT_IF(UPPER(SEVERITY) = 'CRITICAL') AS CRIT,
    COUNT_IF(UPPER(SEVERITY) = 'HIGH')     AS HIGH,
    COUNT_IF(UPPER(SEVERITY) = 'MEDIUM')   AS MED,
    COUNT_IF(UPPER(SEVERITY) NOT IN ('CRITICAL', 'HIGH', 'MEDIUM')) AS LOW,
    COUNT(*) AS TOTAL,
    -- deferred-item: age (minutes) of the OLDEST still-open critical, so the page
    -- verdict can carry MTTR pressure (duration) that a raw count hides. Mirrors the
    -- UNDELIVERED_OLDEST_MIN age pattern; NULL when no open critical. New column —
    -- existing consumers read CRIT/HIGH/TOTAL by name and ignore it.
    MAX(CASE WHEN UPPER(SEVERITY) = 'CRITICAL'
             THEN DATEDIFF('minute', RAISED_AT, CURRENT_TIMESTAMP()) END) AS OLDEST_CRIT_MIN
FROM {core_object("ALERT_EVENTS")}
WHERE {and_where(*where)}
"""


def alert_event_history(days: int) -> str:
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    return f"""
SELECT DATE(RAISED_AT) AS DAY, SEVERITY, COUNT(*) AS EVENTS
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2
ORDER BY DAY
"""


# A >= this-many-minute gap in a viewer's own APP_USAGE activity marks the
# boundary of a new visit (Cost3). This render's own usage row is still buffered
# (main.py flushes at end-of-render), so it cannot count as the last visit.
SINCE_LAST_VISIT_GAP_MIN = 30


def since_last_visit(company: str = "ALL", gap_minutes: int = SINCE_LAST_VISIT_GAP_MIN) -> str:
    """This viewer's last visit + what changed since — new alerts (by severity)
    and new actions (Cost3, 'what changed since your last visit').

    'Last visit' requires a GENUINE >=gap_minutes idle break: if the viewer has
    ANY activity in the last gap_minutes they are mid-session (the current
    render's own row is still buffered), so LAST_VISIT resolves to NULL and the
    caller suppresses the opener — it only greets a real return, never a rolling
    window during a long session. Otherwise LAST_VISIT is their most recent prior
    activity. Identity matches the writer (identity_sql); everything compares in
    TIMESTAMP_NTZ so the account-time stamps line up. Company-scoped like the page
    (ALL-company events included); NULL last-visit yields zero counts."""
    from app.core.identity import identity_sql

    gap = max(5, min(int(gap_minutes), 1440))
    who = identity_sql()
    comp = str(company or "ALL")
    alert_scope = ("" if comp.upper() == "ALL"
                   else f"AND UPPER(e.COMPANY) IN ({sql_literal(comp.upper())}, 'ALL')")
    action_scope = ("" if comp.upper() == "ALL"
                    else f"AND UPPER(a.COMPANY) IN ({sql_literal(comp.upper())}, 'ALL')")
    return f"""
WITH recent AS (
    SELECT COUNT(*) AS N_RECENT
    FROM {core_object("APP_USAGE")}
    WHERE USER_NAME = {who}
      AND AT >= DATEADD('minute', -{gap}, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
),
lv AS (
    -- Only a real idle gap counts: mid-session activity (N_RECENT > 0) forces
    -- LAST_VISIT to NULL, so the opener greets a genuine return, not a rolling
    -- 30-min window during a continuous session.
    SELECT MAX(u.AT) AS LAST_VISIT
    FROM {core_object("APP_USAGE")} u
    CROSS JOIN recent r
    WHERE u.USER_NAME = {who}
      AND u.AT < DATEADD('minute', -{gap}, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
      AND r.N_RECENT = 0
),
al AS (
    SELECT
        COUNT(*) AS NEW_ALERTS,
        COUNT_IF(UPPER(e.SEVERITY) = 'CRITICAL') AS NEW_CRIT,
        COUNT_IF(UPPER(e.SEVERITY) = 'HIGH') AS NEW_HIGH
    FROM {core_object("ALERT_EVENTS")} e
    CROSS JOIN lv
    WHERE lv.LAST_VISIT IS NOT NULL
      AND e.RAISED_AT::TIMESTAMP_NTZ > lv.LAST_VISIT {alert_scope}
),
ac AS (
    SELECT COUNT(*) AS NEW_ACTIONS
    FROM {core_object("ACTION_QUEUE")} a
    CROSS JOIN lv
    WHERE lv.LAST_VISIT IS NOT NULL
      AND a.CREATED_AT::TIMESTAMP_NTZ > lv.LAST_VISIT {action_scope}
)
SELECT
    lv.LAST_VISIT,
    DATEDIFF('minute', lv.LAST_VISIT, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ) AS MINUTES_AGO,
    al.NEW_ALERTS, al.NEW_CRIT, al.NEW_HIGH, ac.NEW_ACTIONS
FROM lv, al, ac
"""


def alert_mttr(days: int = 90) -> str:
    """Weekly MTTA/MTTR from alert lifecycle timestamps."""
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    return f"""
SELECT
    DATE_TRUNC('week', RAISED_AT)::DATE AS WEEK,
    COUNT(*) AS EVENTS,
    SUM(IFF(ACK_AT IS NOT NULL, 1, 0)) AS ACKED,
    -- codex#40 companion: a MACHINE close (V067 escalation SUPERSEDED, or the V091
    -- auto-clear sweep marking a cleared condition AUTO_CLEARED) is NOT a human
    -- resolution — exclude both from the RESOLVED count and MTTR so machine closes
    -- don't pollute the operator panel.
    SUM(IFF(RESOLVED_AT IS NOT NULL AND COALESCE(RESOLUTION_KIND, '') NOT IN ('SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED'), 1, 0)) AS RESOLVED,
    ROUND(AVG(DATEDIFF('minute', RAISED_AT, ACK_AT)), 1) AS MTTA_MIN,
    ROUND(AVG(IFF(COALESCE(RESOLUTION_KIND, '') NOT IN ('SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED'),
                  DATEDIFF('minute', RAISED_AT, RESOLVED_AT), NULL)), 1) AS MTTR_MIN
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1
ORDER BY WEEK
"""


def alert_rules() -> str:
    return f"""
SELECT RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, OWNER, CHANNEL, UPDATED_AT
FROM {core_object("ALERT_CONFIG")}
ORDER BY FAMILY, RULE_ID
"""


def action_queue(limit: int = 200, company: str = "ALL") -> str:
    """Newest OPEN actions (r19 #5): status filters in SQL so an old open
    critical can never age out of the newest-N fetch window. Ranking stays
    in logic.rank_actions — one place; the literals here mirror
    logic.actions.OPEN_STATUSES (cross-locked in tests).

    codex#23: order by SEVERITY before the LIMIT so the cap keeps the top-severity
    open actions. The old CREATED_AT-only sort could truncate an old open CRITICAL out
    of the newest-N window when >N actions are open; rank_actions re-ranks the fetched
    set, but only over rows that survive this cap.

    ``company`` (owner ask 2026-08-17: the triage filter must apply) scopes to that
    company's actions PLUS account-level ('ALL') actions that apply to everyone.
    'ALL' is a no-op — the full queue, same as before.

    DEFER_UNTIL (V074, below floor 88) lets rank_actions / action_summary drop parked items (Next-Fifty #20);
    parked rows sort LAST before the cap and DEFERRED_TOTAL / NEXT_RESUME_DATE are uncapped."""
    limit = max(1, min(int(limit), 1000))
    _aq_today = account_today_sql()
    comp = str(company or "ALL").strip()
    company_clause = ("" if comp.upper() == "ALL"
                      else f"\n  AND UPPER(COALESCE(COMPANY, 'ALL')) IN ('ALL', {sql_literal(comp.upper())})")
    return f"""
SELECT ACTION_ID, CREATED_AT, COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, DUE_DATE,
       SOURCE, PROOF_SQL, ESTIMATED_USD, PERIOD, DEFER_UNTIL, UPDATED_AT,
       COUNT_IF(DEFER_UNTIL > {_aq_today}) OVER () AS DEFERRED_TOTAL,
       MIN(IFF(DEFER_UNTIL > {_aq_today}, DEFER_UNTIL, NULL)) OVER () AS NEXT_RESUME_DATE
FROM {core_object("ACTION_QUEUE")}
WHERE UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS'){company_clause}
ORDER BY CASE WHEN DEFER_UNTIL > {_aq_today} THEN 1 ELSE 0 END,
         CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
              WHEN 'MEDIUM' THEN 2 ELSE 3 END, CREATED_AT DESC
LIMIT {limit}
"""


def _ledger_twin_select() -> str:
    """One row per MANUAL ledger item (SOURCE_CHANGE_ID NULL) that the autobook has ALSO booked from
    the change scan: same warehouse, same lever (app RESIZE == registry SIZE), registry CHANGE_SEEN_AT
    within LEDGER_TWIN_MATCH_DAYS after the manual CREATED_AT, and the auto row already SETTLED. The
    settled auto row is the measured booking-of-record; the manual twin is excluded from every total
    (Next-Fifty #5, double-booking). While the auto row is still ESTIMATED it carries $0, so nothing
    double-counts yet. CHANGE_SEEN_AT is LTZ, CREATED_AT NTZ (V005) — the cast uses the session TZ,
    the account's America/Chicago, the same clock as the NTZ defaults; -1h slack covers a scan that
    lands between the ALTER and the ledger INSERT.

    V153 (Next-Fifty #11): SP_LEDGER_AUTOBOOK now ADOPTS a matching manual ESTIMATED row (stamps its
    SOURCE_CHANGE_ID) when it sees the change first — its ADOPT UPDATE uses these same ON/WHERE lines —
    so new twins arise only from history (pre-V153 rows) or from pairings the 1:1 adopt leaves unpaired."""
    levers = ", ".join(sql_literal(x) for x in sorted(LEDGER_AUTOBOOKED_LEVERS))
    return f"""SELECT m.ITEM_ID AS TWIN_ITEM_ID, r.CHANGE_ID AS TWIN_CHANGE_ID, a.ITEM_ID AS TWIN_AUTO_ITEM_ID
    FROM {core_object("SAVINGS_LEDGER")} m
    JOIN {core_object("WAREHOUSE_CHANGE_REGISTRY")} r
      ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))
     AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))
     AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)
     AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', {int(LEDGER_TWIN_MATCH_DAYS)}, m.CREATED_AT)
    JOIN {core_object("SAVINGS_LEDGER")} a
      ON a.SOURCE_CHANGE_ID = r.CHANGE_ID
     AND a.STATE <> 'ESTIMATED'
    WHERE m.SOURCE_CHANGE_ID IS NULL
      AND UPPER(TRIM(m.FINDING_TYPE)) IN ({levers})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1"""


def _ledger_twin_cte() -> str:
    return f"twin AS (\n    {_ledger_twin_select()}\n)"


def savings_ledger(limit: int | None = 500) -> str:
    """The savings ledger, newest first. `limit` caps rows for a browsable DETAIL table;
    pass limit=None for the ECONOMICS reads (all-time verified $, realization, QTD, run-rate,
    per-lever) so those totals sum the WHOLE ledger. A row cap silently truncates the oldest-
    CREATED rows, which understates all-time verified savings (it shrinks as the ledger grows)
    and makes the ledger's QTD disagree with the uncapped savings_summary_quarter mart that the
    Brief/Scorecard cite for the same quarter (cost-hunt5 2026-08-30).

    SUPERSEDED_BY_CHANGE_ID (Next-Fifty #5): set on a manual row whose change the autobook ALSO booked
    and settled (_ledger_twin_select) — actions.split_superseded drops it from every rollup.

    Full-window re-measure (Next-Fifty #11 / V153) — read-only disclosure from the linked registry row,
    never written back:
      MEASURED_AFTER_DAYS — the after-window the change scan measured (~14 once it closed).
      REMEASURED_14D_MONTHLY_USD — the V153 settle recomputed on the CLOSED window: the same gate as the
        proc (the 5 closed verdicts, window closed, credits metered after), the same $5 floor, the same
        LBA-1 RN (a co-attributed row reads $0) and the FLOAT credit rate from SETTINGS. NULL until the
        window closes, below the floor, or on a manual row. A row settled before V153 on ~3 days of
        after-data (and priced at 4 instead of 3.68) keeps its stored VERIFIED_USD; this column shows the
        full-window figure beside it.
      VOLUME_RATIO / VOLUME_CONFOUNDED — per-day query volume after vs the 14-day baseline, and whether it
        sits outside 0.7-1.3x (the proc's VOLUME_CONFOUNDED note; dollars are never adjusted for it).
    The window function evaluates before ORDER BY / LIMIT, so the RN ranks the whole ledger, not the
    capped page."""
    limit_clause = f"\nLIMIT {int(limit)}" if limit is not None else ""
    # mirrors the V153 SP_LEDGER_AUTOBOOK settle gate on the app clock (account_today_sql, the TZ standard)
    _closed = ("r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')\n"
               f"           AND {account_today_sql()} > r.TRACKING_UNTIL\n"
               "           AND r.AFTER_CREDITS_PER_DAY IS NOT NULL")
    _usd = "(COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0)) * px.RATE * 30"
    _vol = "(r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0)) / NULLIF(r.BASELINE_QUERIES / 14.0, 0)"
    return f"""
WITH {_ledger_twin_cte()}
SELECT l.ITEM_ID, l.ACTION_ID, l.CREATED_AT, l.DESCRIPTION, l.STATE, l.ESTIMATED_USD, l.VERIFIED_USD,
       l.VERIFIED_AT, l.VERIFIED_BY, l.PROOF_SQL, l.NOTES,
       -- The dominant autobook path leaves FINDING_TYPE NULL and encodes the lever only in the
       -- source registry SETTING; recover it (SIZE -> RESIZE, matching the app RESIZE bucket) so
       -- the by-lever rollup isn't one big 'unclassified' pile. Mirrors verified_wins; a no-op
       -- once V145 stamps/backfills the stored column (COALESCE takes the real value first).
       COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''),
                CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END,
                'unclassified') AS FINDING_TYPE,
       IFF(l.SOURCE_CHANGE_ID IS NULL, 'manual', 'auto') AS SOURCE,
       t.TWIN_CHANGE_ID AS SUPERSEDED_BY_CHANGE_ID,
       r.AFTER_DAYS AS MEASURED_AFTER_DAYS,
       IFF({_closed},
           IFF({_usd} >= 5,
               IFF(ROW_NUMBER() OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1,
                   ROUND({_usd}, 2), 0),
               NULL),
           NULL) AS REMEASURED_14D_MONTHLY_USD,
       IFF({_closed},
           ROUND({_vol}, 2), NULL) AS VOLUME_RATIO,
       IFF({_closed},
           {_vol} NOT BETWEEN 0.7 AND 1.3, NULL) AS VOLUME_CONFOUNDED
FROM {core_object("SAVINGS_LEDGER")} l
LEFT JOIN {core_object("WAREHOUSE_CHANGE_REGISTRY")} r ON l.SOURCE_CHANGE_ID = r.CHANGE_ID
LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID
CROSS JOIN (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS RATE
            FROM {core_object("SETTINGS")}) px
ORDER BY l.CREATED_AT DESC{limit_clause}
"""


def verified_wins(company: str = "ALL") -> str:
    """VERIFIED savings rows typed to (fix, warehouse) for the proven-fix transfer
    engine. The app-guarded remediation path stamps FINDING_TYPE/TARGET_OBJECT
    (V053), but the dominant autobook path (V038) leaves them NULL and encodes the
    fix only in free text — recover the type + warehouse by joining
    WAREHOUSE_CHANGE_REGISTRY on SOURCE_CHANGE_ID (SETTING -> fix type,
    WAREHOUSE_NAME -> target). VERIFIED-only, realized dollars only (never ESTIMATED).
    Scoped to the page's company by COMPANY_FOR_WAREHOUSE on the recovered target, so
    evidence never leaks across a company boundary; company='ALL' is account-wide."""
    from app import companies
    _target = "COALESCE(NULLIF(TRIM(l.TARGET_OBJECT), ''), r.WAREHOUSE_NAME)"
    _scope = ("" if str(company or "ALL").upper() == "ALL"
              else f"\n  AND {companies.company_case_sql(_target)} = {sql_literal(str(company))}")
    return f"""
WITH {_ledger_twin_cte()}
SELECT
    l.ITEM_ID,
    l.CREATED_AT,
    l.VERIFIED_AT,
    l.VERIFIED_USD,
    COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''), r.SETTING, 'unclassified') AS FIX_TYPE,
    {_target} AS TARGET_WAREHOUSE,
    r.OLD_VALUE,
    r.NEW_VALUE
FROM {core_object("SAVINGS_LEDGER")} l
LEFT JOIN {core_object("WAREHOUSE_CHANGE_REGISTRY")} r ON l.SOURCE_CHANGE_ID = r.CHANGE_ID
LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID
WHERE l.STATE = 'VERIFIED'
  AND COALESCE(l.VERIFIED_USD, 0) > 0
  AND t.TWIN_ITEM_ID IS NULL{_scope}
ORDER BY l.VERIFIED_USD DESC
LIMIT 200
"""


def supersede_ledger_twins_sql(actor_sql: str) -> str:
    """Idempotent cleanup: REJECT every manual ledger row the autobook's settled row supersedes
    (see _ledger_twin_select), stamping the linked CHANGE_ID + auto ITEM_ID + the viewer in NOTES.
    `actor_sql` is app.core.identity.identity_sql() (a quoted literal or CURRENT_USER()). REJECTED rows
    leave every total already; the stamp makes the dedupe durable and stops the monthly verifier
    re-proposing on an ESTIMATED twin. VERIFIED_USD is left as-is for the audit trail."""
    return f"""UPDATE {core_object('SAVINGS_LEDGER')} l
SET STATE = 'REJECTED',
    NOTES = LEFT(COALESCE(l.NOTES, '') || ' | superseded by auto-measured change ' || t.TWIN_CHANGE_ID
                 || ' (ledger row ' || t.TWIN_AUTO_ITEM_ID || '); double-booking cleanup by ' || {actor_sql}, 2000)
FROM ({_ledger_twin_select()}) t
WHERE l.ITEM_ID = t.TWIN_ITEM_ID
  AND l.STATE <> 'REJECTED';"""


def latest_digest() -> str:
    return f"""
SELECT DIGEST_DATE, MODEL, BODY, CREATED_AT
FROM {core_object("DAILY_DIGEST")}
ORDER BY DIGEST_DATE DESC
LIMIT 1
"""


def savings_verification_runs() -> str:
    """The monthly verifier's latest proposal per ledger item. Next-Fifty #5: hides proposals for
    autobook rows (they settle themselves; the verifier proposes $0 for them) and for a manual row an
    autobook row supersedes (applying one would verify the same saving twice). A proposal whose ledger
    row is gone (L NULL) still shows."""
    return f"""
WITH {_ledger_twin_cte()}
SELECT V.RUN_AT, V.ITEM_ID, V.WAREHOUSE_NAME, V.BASELINE_EST_USD,
       V.MEASURED_IDLE_USD_30D, V.PROPOSED_VERIFIED_USD, L.STATE
FROM {core_object("SAVINGS_VERIFICATION_RUNS")} V
LEFT JOIN {core_object("SAVINGS_LEDGER")} L ON L.ITEM_ID = V.ITEM_ID
LEFT JOIN twin t ON t.TWIN_ITEM_ID = V.ITEM_ID
WHERE L.SOURCE_CHANGE_ID IS NULL AND t.TWIN_ITEM_ID IS NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY V.ITEM_ID ORDER BY V.RUN_AT DESC) = 1
ORDER BY V.PROPOSED_VERIFIED_USD DESC
LIMIT 200
"""


def settings() -> str:
    return f"SELECT KEY, VALUE, UPDATED_AT, UPDATED_BY FROM {core_object('SETTINGS')} ORDER BY KEY"


def schema_version() -> str:
    return f"SELECT VERSION, DESCRIPTION, APPLIED_AT FROM {core_object('SCHEMA_VERSION')} ORDER BY VERSION"


def app_error_log(limit: int = 100) -> str:
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT LOGGED_AT, PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME
FROM {core_object("APP_ERROR_LOG")}
ORDER BY LOGGED_AT DESC
LIMIT {limit}
"""


# Next-Fifty #7: the shared-warehouse self-cost split, keyed on the QUERY_TAG Streamlit-in-Snowflake stamps on
# every app statement (common.app_self_sql; config.APP_SIS_QUERY_TAG_FRAGMENT). INTERACTIVE APP = every
# statement the app ran, including the connector's result_scan fetches. APP RUNTIME (SiS) = the Streamlit
# session statement itself (`execute streamlit ... OVERWATCH_APP()`), which carries the same app tag, so
# that arm is tested FIRST. Everything else - the loader tasks, the native email alerts and ad-hoc use of
# the warehouse - is APP_OTHER_WORKLOAD. SiS has stamped this tag all along, so the split holds for history.
APP_RUNTIME_WORKLOAD = "APP RUNTIME (SiS)"
APP_OTHER_WORKLOAD = "TASKS / ALERTS / OTHER"


def _self_workload_sql() -> str:
    """The WORKLOAD CASE both self-cost builders share (they already read QUERY_TEXT via app_self_sql).
    The runtime arm comes FIRST: the EXECUTE STREAMLIT statement carries the same SiS app tag as the
    app's own statements. STARTSWITH keeps each builder's own statement (starts with SELECT) out of it."""
    from app.config import APP_STREAMLIT_NAME
    return (f"CASE WHEN STARTSWITH(UPPER(COALESCE(QUERY_TEXT, '')), 'EXECUTE STREAMLIT') "
            f"AND CONTAINS(UPPER(QUERY_TEXT), '{APP_STREAMLIT_NAME}') THEN '{APP_RUNTIME_WORKLOAD}' "
            f"WHEN {app_self_sql()} THEN 'INTERACTIVE APP' ELSE '{APP_OTHER_WORKLOAD}' END")


def app_self_cost(days: int) -> str:
    """What OVERWATCH itself spends on the shared warehouse, split by tag/marker (common.app_self_sql):
    INTERACTIVE APP = every statement SiS ran for the app (its own app tag), APP RUNTIME (SiS) = the Streamlit
    session statement, everything else is APP_OTHER_WORKLOAD (see _self_workload_sql)."""
    from app.config import APP_WAREHOUSE

    days = bounded_days(days, maximum=30)
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    {_self_workload_sql()} AS WORKLOAD,
    COUNT(*) AS APP_QUERIES,
    SUM(TOTAL_ELAPSED_TIME) / 1000.0 AS ELAPSED_SEC,
    SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
  AND WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
GROUP BY 1, 2
ORDER BY DAY, WORKLOAD
"""


def app_self_cost_usd(days: int = 30) -> str:
    """OVERWATCH run-cost basis for trailing COMPLETE days, mart-only (no ACCOUNT_USAGE):
    per-pipeline ATTRIBUTED compute (MART_TASK_GRAPH_DAILY.WH_CREDITS = QUERY_ATTRIBUTION_HISTORY,
    excl. idle) for DBA_MAINT_DB.OVERWATCH task graphs, beside the app warehouse's METERED
    compute. TOTAL_ATTRIBUTED via SUM() OVER () (uncapped-aggregate rule). No $ in SQL."""
    from app.config import APP_WAREHOUSE
    days = bounded_days(days, 90)
    win = f"DAY >= DATEADD('day', -{days}, CURRENT_DATE()) AND DAY < CURRENT_DATE()"
    return f"""
WITH pipes AS (
    SELECT PIPELINE, SUM(GRAPH_RUNS) AS GRAPH_RUNS, SUM(RUNS_WITH_FAILURES) AS RUNS_WITH_FAILURES,
           SUM(WH_CREDITS) AS ATTRIBUTED_CREDITS
    FROM {mart_object("MART_TASK_GRAPH_DAILY")}
    WHERE {win}
      AND UPPER(DATABASE_NAME) = {sql_literal(OVERWATCH_DB)}
      AND UPPER(SCHEMA_NAME) = {sql_literal(CORE_SCHEMA)}
    GROUP BY PIPELINE
),
metered AS (
    SELECT COALESCE(SUM(CREDITS_COMPUTE), 0) AS METERED_CREDITS
    FROM {mart_object("FACT_WAREHOUSE_DAILY")}
    WHERE WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)} AND {win}
)
SELECT p.PIPELINE, p.GRAPH_RUNS, p.RUNS_WITH_FAILURES,
       ROUND(p.ATTRIBUTED_CREDITS, 4) AS ATTRIBUTED_CREDITS,
       ROUND(SUM(p.ATTRIBUTED_CREDITS) OVER (), 4) AS TOTAL_ATTRIBUTED_CREDITS,
       ROUND(m.METERED_CREDITS, 4) AS METERED_CREDITS
FROM metered m
LEFT JOIN pipes p ON 1 = 1
ORDER BY p.ATTRIBUTED_CREDITS DESC NULLS LAST
LIMIT 500
"""


def app_cortex_self_cost(days: int = 30) -> str:
    """Next-Fifty #7: the app's OWN Cortex AI spend, by function and model. CORTEX_AI_FUNCTIONS_USAGE_HISTORY
    rows (plain SUM(CREDITS) per query - no METRICS fan-out here) joined to the QUERY_HISTORY rows that carry
    the app's tag (SiS stamps it on every app statement, all history). Tag-only (text=False): the join never
    reads QUERY_TEXT. SiS's tag has no page, so the split is by function/model. Window totals via
    SUM() OVER () so the headline is never derived from the capped rows. Priced in the page, never in SQL.
    The app issues one COMPLETE per query, so the per-model DISTINCT query counts sum without overlap."""
    days = bounded_days(days, 30)
    return f"""
WITH ai AS (
    SELECT F.QUERY_ID, F.FUNCTION_NAME, COALESCE(NULLIF(F.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
           SUM(COALESCE(F.CREDITS, 0)) AS AI_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY F
    WHERE F.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
    GROUP BY 1, 2, 3
)
SELECT ai.FUNCTION_NAME,
       ai.MODEL_NAME,
       COUNT(DISTINCT ai.QUERY_ID) AS REQUESTS,
       ROUND(SUM(ai.AI_CREDITS), 6) AS AI_CREDITS,
       SUM(COUNT(DISTINCT ai.QUERY_ID)) OVER () AS TOTAL_REQUESTS,
       ROUND(SUM(SUM(ai.AI_CREDITS)) OVER (), 6) AS TOTAL_AI_CREDITS
FROM ai
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY Q ON Q.QUERY_ID = ai.QUERY_ID
WHERE Q.START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
  AND {app_self_sql('Q', text=False)}
GROUP BY 1, 2
ORDER BY AI_CREDITS DESC
LIMIT 50
"""


def app_warehouse_queue_by_hour(days: int = 14) -> str:
    """p95 QUEUED_OVERLOAD_TIME on the shared app warehouse by Central hour-of-day, split
    app vs tasks by the shared marker - the data behind 'is staggering the 06:30-07:20
    crons worth it'. Durations end _SEC so the table machinery humanizes them."""
    from app.config import APP_WAREHOUSE
    from app.logic.formulas import ACCOUNT_TIMEZONE
    days = bounded_days(days, 30)
    return f"""
SELECT
    HOUR(CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', START_TIME)) AS HOUR_OF_DAY,
    {_self_workload_sql()} AS WORKLOAD,
    COUNT(*) AS QUERIES,
    COUNT_IF(COALESCE(QUEUED_OVERLOAD_TIME, 0) > 0) AS QUEUED_QUERIES,
    ROUND(APPROX_PERCENTILE(COALESCE(QUEUED_OVERLOAD_TIME, 0), 0.95) / 1000, 2) AS P95_QUEUED_OVERLOAD_SEC,
    ROUND(MAX(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 2) AS MAX_QUEUED_OVERLOAD_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
GROUP BY 1, 2
ORDER BY 1, 2
"""


# Next-Fifty #4: the opt-in EMAIL path's own objects (snowflake/native_alert_templates.sql).
EMAIL_ALERT_NAMES = ("NATIVE_ALERT_NEW_EVENTS", "NATIVE_ALERT_STALE_FACTS",
                     "NATIVE_ALERT_SCAN_HEARTBEAT", "NATIVE_ALERT_DELIVERY_FAILING")
EMAIL_INTEGRATION = "OVERWATCH_EMAIL"
_ALERT_FAIL_STATES = "('FAILED', 'CONDITION_FAILED', 'ACTION_FAILED')"


def email_alert_objects() -> str:
    """SHOW ALERTS for the email path (metadata; lists only alerts the app role can see —
    an empty answer means not-installed-or-not-visible, never 'down')."""
    return f"SHOW ALERTS LIKE 'NATIVE_ALERT%' IN SCHEMA {OVERWATCH_DB}.{CORE_SCHEMA}"


def email_alert_history(days: int = 3) -> str:
    """Per-alert last OK / last FAILED evaluation (INFORMATION_SCHEMA — no ACCOUNT_USAGE lag).
    Timestamps pinned to the account's Central clock (TIMEZONE STANDARD)."""
    from app.logic.formulas import ACCOUNT_TIMEZONE
    days = bounded_days(days, 7)
    names = ", ".join(sql_literal(n) for n in EMAIL_ALERT_NAMES)
    _t = "COALESCE(COMPLETED_TIME, SCHEDULED_TIME)"
    return f"""
SELECT NAME,
       COUNT_IF(STATE = 'TRIGGERED') AS TRIGGERED_N,
       COUNT_IF(STATE IN {_ALERT_FAIL_STATES}) AS FAILED_N,
       CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', MAX(IFF(STATE IN ('TRIGGERED', 'CONDITION_FALSE'), {_t}, NULL)))::TIMESTAMP_NTZ AS LAST_OK_AT,
       CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', MAX(IFF(STATE IN {_ALERT_FAIL_STATES}, {_t}, NULL)))::TIMESTAMP_NTZ AS LAST_FAIL_AT,
       MAX_BY(SQL_ERROR_MESSAGE, IFF(STATE IN {_ALERT_FAIL_STATES}, {_t}, NULL)) AS LAST_FAIL_ERROR
FROM TABLE({OVERWATCH_DB}.INFORMATION_SCHEMA.ALERT_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('day', -{days}, CURRENT_TIMESTAMP()),
         RESULT_LIMIT => 1000))
WHERE DATABASE_NAME = '{OVERWATCH_DB}' AND SCHEMA_NAME = '{CORE_SCHEMA}' AND NAME IN ({names})
GROUP BY NAME
"""


def email_notification_history(days: int = 7) -> str:
    """OVERWATCH_EMAIL send outcomes (one aggregate row; zero sends = readable + quiet).
    NOTIFICATION_HISTORY takes START_TIME => (owner probe 2026-09-24: it rejects the
    START_TIME_RANGE_START argument ALERT_HISTORY / TASK_HISTORY use)."""
    from app.logic.formulas import ACCOUNT_TIMEZONE
    days = bounded_days(days, 14)
    _fail = "UPPER(STATUS) LIKE 'FAIL%'"
    return f"""
SELECT COUNT_IF(UPPER(STATUS) = 'SUCCESS') AS SENT_N,
       COUNT_IF({_fail}) AS FAILED_N,
       CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', MAX(IFF(UPPER(STATUS) = 'SUCCESS', CREATED, NULL)))::TIMESTAMP_NTZ AS LAST_SENT_AT,
       CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', MAX(IFF({_fail}, CREATED, NULL)))::TIMESTAMP_NTZ AS LAST_FAILED_AT,
       MAX_BY(ERROR_MESSAGE, IFF({_fail}, CREATED, NULL)) AS LAST_ERROR
FROM TABLE({OVERWATCH_DB}.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
         START_TIME => DATEADD('day', -{days}, CURRENT_TIMESTAMP()),
         INTEGRATION_NAME => {sql_literal(EMAIL_INTEGRATION)},
         RESULT_LIMIT => 1000))
"""


def alert_routes() -> str:
    return f"""
SELECT ROUTE_ID, FAMILY, MIN_SEVERITY, INTEGRATION_NAME, ENABLED, CREATED_BY, CREATED_AT
FROM {core_object("ALERT_ROUTES")}
ORDER BY FAMILY, MIN_SEVERITY
"""


def remediation_log(limit: int = 100) -> str:
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, EST_MONTHLY_SAVINGS_USD,
       EXECUTED_BY, EXECUTED_AT, STATUS, RESULT_NOTE
FROM {core_object("REMEDIATION_LOG")}
ORDER BY EXECUTED_AT DESC
LIMIT {limit}
"""


def health_strip() -> str:
    """Three always-on sidebar badges in one cached statement: open
    criticals, stalest telemetry source, month-to-date billed credits."""
    # N14/N15/C3: freshness is judged RELATIVE TO EACH SOURCE'S OWN CADENCE — a
    # daily fact at 7h is on time, an hourly fact at 7h is not — and a
    # never-loaded source (NULL ts) is maximally urgent (finite 1e9 sentinel so
    # it wins the pick without NULL-poisoning the aggregates).
    _age = "DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0"
    _lim = ("IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', "
            f"{THRESHOLDS['stale_daily_fact_hours']}, {THRESHOLDS['stale_fact_hours']})")
    _urgency = f"IFF(LAST_LOAD_TS IS NULL, 1e9, {_age} / {_lim})"
    _not_ai = not_ai_service_predicate()
    # P1 (perf): this statement runs on the SHELL of every page, so it was the
    # app's most-repeated read — and it used to be nine UNION ALL arms that
    # re-scanned ALERT_EVENTS twice, SOURCE_FRESHNESS_STATE three times and
    # FACT_METERING_DAILY three times (2.4s per shell render). Now: ONE scan per
    # source table, conditional aggregation inside it, the three single-row CTEs
    # cross-joined into one row, and that row unpivoted into the metric rows by
    # FLATTEN. The FLATTEN is deliberate — Snowflake does not guarantee CTE
    # materialization, so eight `... FROM strip` UNION arms could quietly
    # re-execute each CTE and undo the whole fix; one row fanned out cannot.
    #
    # The OUTPUT CONTRACT IS UNCHANGED and must stay that way: main.py parses
    # these rows BY METRIC NAME (see _health_values), so every METRIC/VALUE/STATE
    # expression below is the byte-equivalent of the arm it replaces.
    # OBJECT_CONSTRUCT drops NULL values, which would silently delete a metric —
    # every VALUE expression is COALESCEd to a non-NULL scalar for that reason.
    return f"""
WITH und_crit AS (
    -- Alert-hunt #6: a CRITICAL is UNDELIVERED when an ELIGIBLE enabled route (the
    -- sender's family/company/severity-floor predicate, as in route_backlog) has no
    -- delivery for it -- NOT merely when the event has no delivery row at all. The old
    -- event-level "any delivery" check let a CRITICAL that reached a sibling info-route
    -- but FAILED its paging route read as delivered (green banner while on-call was never
    -- paged). This is DELIBERATELY unbounded in time (unlike route_backlog's 7d send
    -- window): the always-on banner must keep flagging any still-OPEN undelivered critical,
    -- even one past the sender's send window. JOIN anti-join per (event, eligible route).
    SELECT DISTINCT e.EVENT_ID
    FROM {core_object("ALERT_EVENTS")} e
    LEFT JOIN {core_object("ALERT_CONFIG")} c ON c.RULE_ID = e.RULE_ID
    JOIN {core_object("ALERT_ROUTES")} r
      ON r.ENABLED
     AND (r.FAMILY = 'ALL' OR c.FAMILY = r.FAMILY)
     AND (COALESCE(r.COMPANY_FILTER, 'ALL') = 'ALL'
          OR e.COMPANY = r.COMPANY_FILTER OR UPPER(e.COMPANY) = 'ALL')
    LEFT JOIN {core_object("ALERT_DELIVERIES")} d
      ON d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r.ROUTE_ID
    WHERE UPPER(e.SEVERITY) = 'CRITICAL' AND e.STATUS = 'OPEN' AND d.EVENT_ID IS NULL
    UNION
    -- config-gap safety: a CRITICAL matching NO route can never be delivered, so keep
    -- flagging any critical with zero delivery rows (the pre-#6 event-level behavior).
    SELECT e.EVENT_ID
    FROM {core_object("ALERT_EVENTS")} e
    WHERE UPPER(e.SEVERITY) = 'CRITICAL' AND e.STATUS = 'OPEN'
      AND NOT EXISTS (SELECT 1 FROM {core_object("ALERT_DELIVERIES")} d0
                      WHERE d0.EVENT_ID = e.EVENT_ID)
),
crit AS (
    -- One pass over the open (OPEN or ACK) criticals. rec #3: OPEN_CRITICAL_N
    -- counts STATUS IN ('OPEN','ACK') to match open_alert_severity_counts (and
    -- the Brief/Overview/Control-Room tiles + the platform score), which all
    -- treat ACK as "still open, being worked". Counting OPEN-only made an
    -- acknowledged critical vanish from the sidebar while every page still
    -- showed it. Undelivered membership is the und_crit anti-join above (one row
    -- per undelivered critical), LEFT-joined so it cannot multiply the open count.
    SELECT
        COUNT_IF(e.STATUS IN ('OPEN', 'ACK')) AS OPEN_CRITICAL_N,
        -- N2: OPEN (not yet acknowledged) criticals 30+ min old that are UNDELIVERED
        -- (und_crit: missing delivery to an eligible route) — a page that reached
        -- nobody. Kept OPEN-only: an ACK'd critical was seen in-app, so it is not a
        -- silent routing failure. Always-on so the morning surfaces cannot show green
        -- while a critical failed to route.
        COUNT_IF(e.STATUS = 'OPEN'
                 AND e.RAISED_AT <= DATEADD('minute', -30, CURRENT_TIMESTAMP())
                 AND u.EVENT_ID IS NOT NULL) AS UNDELIVERED_N,
        -- CoCo CR#4: age (minutes) of the OLDEST undelivered critical, so the banner
        -- can show how long nobody has been paged — a count hides a 4h silent failure.
        MAX(IFF(e.STATUS = 'OPEN'
                AND e.RAISED_AT <= DATEADD('minute', -30, CURRENT_TIMESTAMP())
                AND u.EVENT_ID IS NOT NULL,
                DATEDIFF('minute', e.RAISED_AT, CURRENT_TIMESTAMP()), NULL)) AS UNDELIVERED_OLDEST_MIN
    FROM {core_object("ALERT_EVENTS")} e
    LEFT JOIN und_crit u ON u.EVENT_ID = e.EVENT_ID
    WHERE e.STATUS IN ('OPEN', 'ACK') AND UPPER(e.SEVERITY) = 'CRITICAL'
),
fresh AS (
    -- STALEST: the single worst source by cadence-ratio (never-loaded ranks above
    -- everything). AGE_H is that source's age in hours (-1 when it has never
    -- loaded, so 'BAD' + -1 reads as "the worst source has no data").
    -- STALE_N (triage #3) is the COUNT the MAX-age arm cannot express: DAILY/
    -- METERING sources stale past {THRESHOLDS["stale_daily_fact_hours"]}h, hourly past {THRESHOLDS["stale_fact_hours"]}h. C3: a never-loaded
    -- source counts as stale — no data at all is worse than late, not better.
    SELECT
        COUNT(*)                                              AS SOURCE_N,
        COALESCE(ROUND(MAX_BY(f.AGE_H, f.URGENCY), 1), -1)    AS WORST_AGE_H,
        COALESCE(MAX_BY(f.SOURCE_NAME, f.URGENCY), 'none')    AS WORST_NAME,
        MAX(f.URGENCY)                                        AS MAX_URGENCY,
        COUNT_IF(f.LAST_LOAD_TS IS NULL OR f.AGE_H > f.LIM)   AS STALE_N
    FROM (SELECT SOURCE_NAME, LAST_LOAD_TS, {_age} AS AGE_H,
                 {_lim} AS LIM, {_urgency} AS URGENCY
          FROM {core_object("SOURCE_FRESHNESS_STATE")}) f
),
mtd AS (
    -- C1: MTD credits split AI vs compute so the Brief prices AI credits at the
    -- AI rate (blended_billed_usd), not the flat compute rate. The two
    -- partitions sum to the total; NULL SERVICE_TYPE falls to the OTHER arm
    -- (IFF on a NULL predicate takes the else branch, exactly as the old
    -- WHERE-filtered arms did).
    SELECT
        ROUND(COALESCE(SUM(CREDITS_BILLED), 0), 0) AS MTD_ALL,
        -- MTD_AI / MTD_OTHER are blended to dollars downstream (Brief), so keep them RAW and round at
        -- the display edge -- rounding each partition to whole credits first made the Brief's blended
        -- "MTD credit spend" disagree with Overview's raw-credit blend by up to ~$3 (recon-audit
        -- 2026-08-30). MTD_ALL stays whole for the credit-count display.
        COALESCE(SUM(IFF({_AI_SERVICE_PRED}, CREDITS_BILLED, 0)), 0) AS MTD_AI,
        COALESCE(SUM(IFF({_not_ai}, CREDITS_BILLED, 0)), 0) AS MTD_OTHER
    FROM {mart_object("FACT_METERING_DAILY")}
    -- rec #2: anchor the MTD month boundary to the ACCOUNT timezone so this strip
    -- MTD selects the same days as the account_today()-anchored Overview MTD;
    -- CURRENT_DATE() is session-tz and disagreed near midnight.
    WHERE DAY >= {account_month_start_sql()}
),
strip AS (SELECT * FROM crit, fresh, mtd)
SELECT r.value:"m"::VARCHAR AS METRIC,
       r.value:"v"::VARCHAR AS VALUE,
       r.value:"s"::VARCHAR AS STATE
FROM strip s,
     LATERAL FLATTEN(input => ARRAY_CONSTRUCT(
        OBJECT_CONSTRUCT('m', 'OPEN_CRITICAL',
                         'v', TO_VARCHAR(s.OPEN_CRITICAL_N),
                         's', IFF(s.OPEN_CRITICAL_N > 0, 'BAD', 'OK')),
        OBJECT_CONSTRUCT('m', 'UNDELIVERED_CRITICAL',
                         'v', TO_VARCHAR(s.UNDELIVERED_N),
                         's', IFF(s.UNDELIVERED_N > 0, 'BAD', 'OK')),
        OBJECT_CONSTRUCT('m', 'UNDELIVERED_OLDEST_MIN',
                         'v', TO_VARCHAR(COALESCE(s.UNDELIVERED_OLDEST_MIN, 0)), 's', 'INFO'),
        OBJECT_CONSTRUCT('m', 'STALEST_SOURCE_H',
                         'v', TO_VARCHAR(s.WORST_AGE_H),
                         's', CASE WHEN s.SOURCE_N = 0 THEN 'MUTED'
                                   WHEN s.MAX_URGENCY > 1 THEN 'BAD'
                                   WHEN s.MAX_URGENCY > 0.75 THEN 'WARN'
                                   ELSE 'OK' END),
        OBJECT_CONSTRUCT('m', 'STALEST_SOURCE_NAME', 'v', s.WORST_NAME, 's', 'INFO'),
        OBJECT_CONSTRUCT('m', 'MTD_CREDITS', 'v', TO_VARCHAR(s.MTD_ALL), 's', 'INFO'),
        OBJECT_CONSTRUCT('m', 'MTD_CREDITS_AI', 'v', TO_VARCHAR(s.MTD_AI), 's', 'INFO'),
        OBJECT_CONSTRUCT('m', 'MTD_CREDITS_OTHER', 'v', TO_VARCHAR(s.MTD_OTHER), 's', 'INFO'),
        OBJECT_CONSTRUCT('m', 'STALE_SOURCES', 'v', TO_VARCHAR(s.STALE_N), 's', 'INFO')
     )) r
ORDER BY r.INDEX
"""


def incident_timeline(days: int, company: str = "ALL") -> str:
    """One time axis for everything that happened: alerts, task failures, DDL,
    warehouse changes. The 'what else happened around then?' view Datadog does well.

    This is the 7d / live-fallback twin of mart27_sql.incident_timeline (48h, reads
    MART_INCIDENT_TIMELINE). It is kept at PARITY with that mart's V066 loader so the
    replay chart is stable across the window toggle: identical KIND labels
    ('ALERT' / 'TASK_FAIL' / 'DDL' / 'WH_CHANGE'), the same COMPANY + REF_ID columns,
    the same full DDL QUERY_TYPE set, and the warehouse-change arm — before v4.351 this
    path dropped WH_CHANGE + GRANT/REVOKE/RENAME/TRUNCATE DDL, omitted COMPANY/REF_ID,
    and relabelled the lanes, so toggling 48h->7d silently thinned the same window."""
    days = bounded_days(days, 14)
    comp = str(company or "ALL")
    alert_filter = "" if comp.upper() == "ALL" else \
        f"AND COMPANY IN ({sql_literal(comp)}, 'ALL')"
    # C12: label rows by the evidence-based loader UDF, not the pre-V044
    # 'TRXS else ALFA' IFF that mislabeled every non-TRXS (and NULL) database
    # ALFA — so this 7d/fallback path agrees with the 48h MART_INCIDENT_TIMELINE
    # path (COMPANY_FOR_DATABASE, V027 arm [8]) instead of contradicting it.
    entity_filter = "" if comp.upper() == "ALL" else (
        "AND DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')) = " + sql_literal(comp))
    # WAREHOUSE_CHANGE_REGISTRY carries its own COMPANY (like ALERT_EVENTS), so the
    # WH_CHANGE arm scopes the same account-level-rides-along way, not via the DB UDF.
    wh_filter = "" if comp.upper() == "ALL" else \
        f"AND (COMPANY = {sql_literal(comp)} OR UPPER(COMPANY) = 'ALL')"
    return f"""
SELECT 'ALERT' AS EVENT_TYPE, RAISED_AT::TIMESTAMP_NTZ AS AT, SEVERITY,
       LEFT(TITLE, 120) AS LABEL, COMPANY, EVENT_ID AS REF_ID
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()) {alert_filter}
UNION ALL
SELECT 'TASK_FAIL', COMPLETED_TIME::TIMESTAMP_NTZ, 'HIGH',
       LEFT(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME || ': ' ||
            COALESCE(ERROR_MESSAGE, 'failed'), 120),
       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')), NAME
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE COMPLETED_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND STATE = 'FAILED' {entity_filter}
UNION ALL
SELECT 'DDL', START_TIME::TIMESTAMP_NTZ, 'INFO',
       LEFT(USER_NAME || ': ' || QUERY_TEXT, 120),
       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')), QUERY_ID
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND EXECUTION_STATUS = 'SUCCESS'
  AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                     'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
  {entity_filter}
UNION ALL
SELECT 'WH_CHANGE', CHANGE_SEEN_AT::TIMESTAMP_NTZ, 'INFO',
       LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' ||
            COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 120),
       COMPANY, CHANGE_ID
FROM {core_object("WAREHOUSE_CHANGE_REGISTRY")}
WHERE CHANGE_SEEN_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()) {wh_filter}
ORDER BY AT DESC
LIMIT 400
"""


def fact_daily_activity(days: int, company: str = "ALL", database: str = "") -> str:
    """Daily query volume + failures from the hourly fact (sparkline feed).

    Defaults keep the account-wide shape; Control Room passes company +
    database so the sparkline matches the pulse KPIs beside it."""
    from app import companies

    days = bounded_days(days, 30)
    # Day-boundary basis, not a rolling server timestamp: the sparkline's edge day
    # must line up with the CURRENT_DATE()-anchored 'since yesterday' pulse KPIs
    # beside it. CURRENT_TIMESTAMP() here (server tz, not account-aligned under SiS)
    # sliced the window mid-day and could drop/shift the edge day vs the pulse.
    where = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company or "ALL").upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    where.append(companies.database_equals_clause(database, "DATABASE_NAME"))
    return f"""
SELECT DATE_TRUNC('day', HOUR_TS)::DATE AS DAY,
       SUM(QUERY_COUNT) AS QUERIES,
       SUM(FAILED_COUNT) AS FAILS
FROM {mart_object("FACT_QUERY_HOURLY")}
WHERE {and_where(*where)}
GROUP BY 1
ORDER BY 1
"""


def ml_forecast_daily() -> str:
    """Reader for the opt-in SNOWFLAKE.ML.FORECAST output table (see
    snowflake/ml_forecast_option.sql). Absent = engine falls back."""
    return f"""
SELECT TS::DATE AS DAY, FORECAST_CREDITS, LOWER_BOUND, UPPER_BOUND
FROM {core_object("FORECAST_ML_DAILY")}
WHERE TS::DATE > CURRENT_DATE()
ORDER BY DAY
LIMIT 60
"""


def dept_budgets() -> str:
    return f"""
SELECT DEPARTMENT, MONTHLY_BUDGET_USD, UPDATED_AT, UPDATED_BY
FROM {core_object("DEPT_BUDGETS")}
ORDER BY DEPARTMENT
"""


def app_usage_summary(days: int = 30) -> str:
    """Which pages actually get opened — adoption data for curation calls."""
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    return f"""
SELECT PAGE, COUNT(*) AS VISITS, COUNT(DISTINCT USER_NAME) AS USERS,
       COUNT(DISTINCT IFF(AT >= DATEADD('day', -7, CURRENT_TIMESTAMP()),
                          USER_NAME, NULL)) AS WAU,
       MAX(AT) AS LAST_VISIT
FROM {core_object("APP_USAGE")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND COALESCE(EVENT_KIND, 'page_visit') = 'page_visit'
GROUP BY PAGE
ORDER BY VISITS DESC
"""


def app_performance_slo(days: int = 7) -> str:
    """Per-page render/fetch guardrails from already-persisted app evidence."""
    days = bounded_days(days, 30)
    return f"""
WITH target AS (
    SELECT COALESCE(MAX(IFF(RULE_ID = 'OPS_SLOW_RENDER', THRESHOLD_NUM, NULL)), 8) * 1000
             AS RENDER_TARGET_MS
    FROM {core_object('ALERT_CONFIG')}
), renders AS (
    SELECT PAGE, COUNT(*) AS RENDER_SAMPLES,
           ROUND(APPROX_PERCENTILE(RENDER_MS, 0.95), 0) AS P95_RENDER_MS
    FROM {core_object('APP_USAGE')}
    WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND RENDER_MS IS NOT NULL
    GROUP BY PAGE
), fetches AS (
    SELECT PAGE,
           ROUND(SUM(1.0 / COALESCE(SAMPLE_PROB, 1.0))) AS EST_FETCHES,
           ROUND(SUM(IFF(NOT OK, 1.0 / COALESCE(SAMPLE_PROB, 1.0), 0))
                 / NULLIF(SUM(1.0 / COALESCE(SAMPLE_PROB, 1.0)), 0) * 100, 2)
             AS EST_FAILURE_PCT,
           ROUND(
             SUM(IFF(CACHE_HIT IS NULL, 0,
                     IFF(CACHE_HIT, 1, 0) / COALESCE(SAMPLE_PROB, 1.0)))
             / NULLIF(SUM(IFF(CACHE_HIT IS NULL, 0,
                              1.0 / COALESCE(SAMPLE_PROB, 1.0))), 0) * 100,
             1
           ) AS CACHE_HIT_PCT
    FROM {core_object('APP_QUERY_TELEMETRY')}
    WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND NOT STARTSWITH(QUERY_KEY, 'batch_wall:')
    GROUP BY PAGE
), joined AS (
    SELECT COALESCE(r.PAGE, f.PAGE) AS PAGE,
           COALESCE(r.RENDER_SAMPLES, 0) AS RENDER_SAMPLES,
           r.P95_RENDER_MS, t.RENDER_TARGET_MS,
           COALESCE(f.EST_FETCHES, 0) AS EST_FETCHES,
           f.EST_FAILURE_PCT, f.CACHE_HIT_PCT
    FROM renders r
    FULL OUTER JOIN fetches f ON f.PAGE = r.PAGE
    CROSS JOIN target t
)
SELECT *, 1.0 AS FAILURE_TARGET_PCT, 60.0 AS CACHE_TARGET_PCT,
       CASE
         WHEN RENDER_SAMPLES < 20 THEN 'INSUFFICIENT'
         WHEN P95_RENDER_MS > RENDER_TARGET_MS
           OR COALESCE(EST_FAILURE_PCT, 0) > 1.0 THEN 'FAIL'
         WHEN CACHE_HIT_PCT IS NULL OR CACHE_HIT_PCT < 60.0 THEN 'WATCH'
         ELSE 'PASS'
       END AS SLO_STATE
FROM joined
ORDER BY CASE SLO_STATE WHEN 'FAIL' THEN 0 WHEN 'WATCH' THEN 1
                       WHEN 'INSUFFICIENT' THEN 2 ELSE 3 END,
         P95_RENDER_MS DESC NULLS LAST
"""


def contract_exhaustion() -> str:
    """The CIO number: projected contract exhaustion at the canonical trailing-30-
    COMPLETE-days burn (rec 20). DAILY_BURN = SUM(billed) over DAY BETWEEN today-30
    AND today-1 (today's PARTIAL metering excluded) divided by the count of complete
    days actually present — the SAME basis the renewal planner uses (contract.py, N11),
    so the Brief runway can't contradict the Contract page. The old form summed a
    31-date span that INCLUDED today's partial and divided by a literal 30, biasing
    burn low, overstating days-left, and potentially suppressing COST_CONTRACT_BREACH.
    n/a until configured. The COST_CONTRACT_BREACH paging alert (SP_ALERT_SCAN_DAILY)
    was aligned to THIS exact burn in V064 — SUM / NULLIF(COUNT(DISTINCT DAY), 0) over
    DAY BETWEEN today-30 AND today-1 — so the alert and this KPI now byte-match and no
    divergence remains (gap-audit rec #10; the earlier "align it in V065" note was
    stale, and is why the audit re-flagged an already-fixed alert — no V081 needed)."""
    return f"""
SELECT TOTAL, CONSUMED, DAILY_BURN,
       CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
       DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
               {account_today_sql()}) AS EXHAUST_DATE
FROM (
    SELECT
        -- r33: gate TOTAL on a CONFIGURED contract start. CONTRACT_CREDITS and
        -- CONTRACT_START_DATE are independent SETTINGS keys; with credits set but start unset the
        -- CONSUMED sub-select fell back to today and summed ~0, fabricating a healthy runway
        -- (~0% consumed, huge days, green) on the always-on Overview/Brief bars for a possibly-
        -- exhausted contract. TOTAL=0 when start is unset -> contract_runway() (guards TOTAL<=0)
        -- returns None -> the bars render nothing, matching the Contract page's own start gate.
        (SELECT IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,
                    COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0))
         FROM {core_object("SETTINGS")}) AS TOTAL,
        (SELECT COALESCE(SUM(CREDITS_BILLED), 0) FROM {mart_object("FACT_METERING_DAILY")}
         WHERE DAY >= COALESCE((SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                                FROM {core_object("SETTINGS")}), {account_today_sql()})) AS CONSUMED,
        -- r33: the DAILY_BURN window stays session-tz CURRENT_DATE() ON PURPOSE — it must
        -- byte-match the COST_CONTRACT_BREACH paging alert (V064 SP_ALERT_SCAN_DAILY) or the KPI
        -- and the alert diverge (test_rec20_alert_matches_app_mart_window). Realigning both to the
        -- account clock would take an owner-applied migration to alter the alert proc, deferred
        -- until then; the residual ~6h/day account-vs-UTC boundary drift is a rounding-scale bias
        -- on a 30-day mean. (EXHAUST_DATE's anchor above is account-tz — a displayed date, not
        -- part of the alert-matched burn.)
        (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
         FROM {mart_object("FACT_METERING_DAILY")}
         WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                       AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
)
"""


def savings_summary_quarter() -> str:
    """The ROI numerator + the quarter KPI, never mixed with estimates, plus the open estimated
    pipeline labeled separately.

    VERIFIED_ACTIVE_MONTHLY_USD is the ROI numerator (Next-Fifty #3): every item VERIFIED in the
    last SAVINGS_ACTIVE_MONTHS months. Each VERIFIED_USD is a monthly run-rate that keeps saving
    after the quarter it was verified in, so the old quarter-scoped numerator fell to 0x on the
    first day of every quarter while the trailing-30d run cost did not. VERIFIED_QTD_USD stays as
    the separate "verified this quarter" KPI. Reverts are not detected yet, so the 12-month cap is
    the conservative stand-in. (The name is kept: the canary, Brief, DS and tests reference it.)

    Both windows anchor on the ACCOUNT clock (account_today_sql), matching Decision Studio's
    account-time quarter — session-tz DATE_TRUNC('quarter', CURRENT_DATE()) drifted a day at a
    quarter change and disagreed with the DS surface (round-2 bug hunt).

    Next-Fifty #5: every aggregate excludes a manual row the autobook's settled row supersedes (the
    same change booked twice); SUPERSEDED_ITEMS is the UNCAPPED count of such twins still awaiting the
    operator's cleanup (Cost ▸ Optimize ▸ Savings ledger)."""
    _today = account_today_sql()
    _q0 = f"DATE_TRUNC('quarter', {_today})"
    _a0 = f"DATEADD('month', -{int(SAVINGS_ACTIVE_MONTHS)}, {_today})"
    return f"""
WITH {_ledger_twin_cte()}
SELECT
    ROUND(SUM(IFF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL
                  AND l.VERIFIED_AT >= {_q0},
                  COALESCE(l.VERIFIED_USD, 0), 0)), 2) AS VERIFIED_QTD_USD,
    COUNT_IF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL
             AND l.VERIFIED_AT >= {_q0}) AS VERIFIED_ITEMS,
    ROUND(SUM(IFF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL
                  AND l.VERIFIED_AT >= {_a0},
                  COALESCE(l.VERIFIED_USD, 0), 0)), 2) AS VERIFIED_ACTIVE_MONTHLY_USD,
    COUNT_IF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL
             AND l.VERIFIED_AT >= {_a0}) AS VERIFIED_ACTIVE_ITEMS,
    ROUND(SUM(IFF(l.STATE = 'ESTIMATED' AND t.TWIN_ITEM_ID IS NULL,
                  COALESCE(l.ESTIMATED_USD, 0), 0)), 2) AS ESTIMATED_OPEN_USD,
    COUNT_IF(t.TWIN_ITEM_ID IS NOT NULL AND l.STATE <> 'REJECTED') AS SUPERSEDED_ITEMS
FROM {core_object("SAVINGS_LEDGER")} l
LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID
"""


def app_cost_last_30d() -> str:
    """The ROI denominator: what the app + its tasks burned over the trailing 30
    COMPLETE days on their shared warehouse. A trailing-30-day (monthly) window --
    NOT quarter-to-date: the ROI numerator (SAVINGS_LEDGER.VERIFIED_USD) is a
    30-day/monthly-magnitude value, so dividing it by a QTD cumulative cost (up to
    ~90 days) was a time-basis mismatch that flipped the "pays for itself" verdict
    (bug-hunt 2026-08-30). From the fact since r14 #5 -- the Brief was the last
    always-on surface paying a live metering scan.

    WAREHOUSE_NAME comes from config (siblings app_self_cost/app_statement_stats
    do the same); a hardcoded literal would silently zero the ROI denominator if
    the app warehouse is ever renamed."""
    from app.config import APP_WAREHOUSE

    return f"""
SELECT ROUND(COALESCE(SUM(CREDITS_TOTAL), 0), 2) AS APP_CREDITS_30D
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
  AND DAY >= DATEADD('day', -30, CURRENT_DATE())
  AND DAY < CURRENT_DATE()
"""


def ledger_for_event(event_id_prefix: str) -> str:
    """Closed-loop verification chip: ledger items booked from one alert
    event (the drawer writes 'event <id8>' into NOTES)."""
    import re as _re

    prefix = str(event_id_prefix or "").strip().lower()
    if not _re.match(r"^[0-9a-f\-]{8}$", prefix):
        raise ValueError(f"Invalid event id prefix: {event_id_prefix!r}")
    return f"""
SELECT DESCRIPTION, STATE, ESTIMATED_USD, VERIFIED_USD, CREATED_AT
FROM {core_object("SAVINGS_LEDGER")}
WHERE NOTES LIKE {sql_literal('%event ' + prefix + '%')}
ORDER BY CREATED_AT DESC
LIMIT 5
"""


def events_for_rule(rule_id: str, days: int = 90) -> str:
    """Recent events for ONE rule (drawer history). Rule id validated."""
    import re as _re

    rid = str(rule_id or "").strip().upper()
    if not _re.match(r"^[A-Z0-9_]{1,60}$", rid):
        raise ValueError(f"Invalid rule id: {rule_id!r}")
    days = bounded_days(days, 180)
    return f"""
SELECT RAISED_AT, SEVERITY, COMPANY, TITLE, STATUS
FROM {core_object("ALERT_EVENTS")}
WHERE RULE_ID = {sql_literal(rid)}
  AND RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
ORDER BY RAISED_AT DESC
LIMIT 20
"""


def resolutions_for_rule(rule_id: str, days: int = 180, limit: int = 5) -> str:
    """rec26 / CoCo Alerts #26: how the SAME rule was resolved before — the last few
    RESOLVED events for this rule with their resolution kind + note, newest first, so
    the drawer offers a playbook from the account's own history instead of generic
    guidance. SUPERSEDED closes are excluded (they carry no human decision). Rule id
    validated (identifier allowlist)."""
    import re as _re

    rid = str(rule_id or "").strip().upper()
    if not _re.match(r"^[A-Z0-9_]{1,60}$", rid):
        raise ValueError(f"Invalid rule id: {rule_id!r}")
    days = bounded_days(days, 365)
    cap = max(1, min(int(limit), 20))
    # RESOLUTION_NOTE lives in ALERT_AUDIT.NOTE (the resolve/remediate action's note),
    # keyed by EVENT_ID — NOT on ALERT_EVENTS (V074 added RESOLUTION_NOTE to ACTION_QUEUE,
    # a different table). Selecting it from ALERT_EVENTS raised 000904 invalid identifier
    # on every Alerts drawer open (owner error log 2026-08-17). Join the audit note.
    return f"""
SELECT e.RESOLVED_AT, e.RESOLUTION_KIND, e.COMPANY, e.TITLE,
       a.NOTE AS RESOLUTION_NOTE
FROM {core_object("ALERT_EVENTS")} e
LEFT JOIN (
    SELECT EVENT_ID, MAX_BY(NOTE, ACTED_AT) AS NOTE
    FROM {core_object("ALERT_AUDIT")}
    WHERE ACTION IN ('RESOLVE', 'REMEDIATE', 'NOTE')
      AND COALESCE(NOTE, '') <> ''
    GROUP BY EVENT_ID
) a ON a.EVENT_ID = e.EVENT_ID
WHERE e.RULE_ID = {sql_literal(rid)}
  AND e.STATUS = 'RESOLVED'
  AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED')
  AND e.RESOLVED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
ORDER BY e.RESOLVED_AT DESC
LIMIT {cap}
"""


def rule_precision(days: int = 90) -> str:
    """Per-rule alert precision from resolution kinds (V021).

    precision = ACTIONED / (ACTIONED + NOISE); EXPECTED (maintenance/known)
    is excluded from the denominator. UNTAGGED counts resolved events from
    before V021 or closed without a kind — high untagged means the score
    is not yet trustworthy for that rule.
    """
    days = max(7, min(int(days or 90), 365))
    return f"""
SELECT
    RULE_ID,
    -- codex#40 companion: exclude machine SUPERSEDED closes so RESOLVED_EVENTS ties to the
    -- ACTIONED+NOISE+EXPECTED+UNTAGGED buckets (PRECISION_PCT was already unaffected).
    COUNT_IF(COALESCE(RESOLUTION_KIND, '') NOT IN ('SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED')) AS RESOLVED_EVENTS,
    COUNT_IF(RESOLUTION_KIND = 'ACTIONED')            AS ACTIONED,
    COUNT_IF(RESOLUTION_KIND = 'NOISE')               AS NOISE,
    COUNT_IF(RESOLUTION_KIND = 'EXPECTED')            AS EXPECTED,
    COUNT_IF(RESOLUTION_KIND IS NULL)                 AS UNTAGGED,
    -- PRECISION_PCT must repeat the COUNT_IF expressions, NOT reference the ACTIONED /
    -- NOISE aliases: Snowflake forbids lateral column-alias references inside the SELECT
    -- list (compiles to "invalid identifier"), so the panel silently returned ok=False.
    -- Same class as the round-5 tag_coverage fix; missed in that sweep. (bug-hunt round 6)
    ROUND(100 * COUNT_IF(RESOLUTION_KIND = 'ACTIONED')
               / NULLIF(COUNT_IF(RESOLUTION_KIND = 'ACTIONED')
                        + COUNT_IF(RESOLUTION_KIND = 'NOISE'), 0), 1) AS PRECISION_PCT
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS = 'RESOLVED'
  AND RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY RULE_ID
ORDER BY RESOLVED_EVENTS DESC
LIMIT 100
"""


def mart_vs_live_recon() -> str:
    """Mart totals vs live ACCOUNT_USAGE over the same complete window —
    freshness says the loaders RAN; this says the numbers MATCH. Three checks: billed metering,
    warehouse credits (FACT_WAREHOUSE_DAILY vs WAREHOUSE_METERING_HISTORY, Next-Fifty #25) and
    query counts; the AI facts reconcile in the separate mart_vs_live_ai_recon.

    Metering + warehouse compare 28 complete days ending 3 days ago (metering can
    lag 24-72h); query counts compare 7 days ending 2 days ago. DRIFT_PCT
    within ±2% is normal (late-arriving rows); beyond ±5% means a loader gap
    — re-run the backfill for that window.
    """
    return f"""
WITH f_met AS (
    SELECT SUM(CREDITS_BILLED) AS V
    FROM {core_object("FACT_METERING_DAILY")}
    WHERE DAY >= DATEADD('day', -31, CURRENT_DATE())
      AND DAY <  DATEADD('day', -3,  CURRENT_DATE())
),
l_met AS (
    SELECT SUM(CREDITS_BILLED) AS V
    FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -31, CURRENT_DATE())
      AND USAGE_DATE <  DATEADD('day', -3,  CURRENT_DATE())
),
f_wh AS (
    -- Next-Fifty #25: FACT_WAREHOUSE_DAILY vs the metering it is loaded from. Mirrors the V062
    -- loader: DATE(START_TIME) day key, WAREHOUSE_ID > 0 (no CLOUD_SERVICES_ONLY pseudo-row),
    -- CREDITS_TOTAL = SUM(CREDITS_USED). Same lag-safe 28d window as the metering arm.
    SELECT SUM(CREDITS_TOTAL) AS V
    FROM {core_object("FACT_WAREHOUSE_DAILY")}
    WHERE DAY >= DATEADD('day', -31, CURRENT_DATE())
      AND DAY <  DATEADD('day', -3,  CURRENT_DATE())
),
l_wh AS (
    SELECT SUM(CREDITS_USED) AS V
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE START_TIME >= DATEADD('day', -31, CURRENT_DATE())
      AND START_TIME <  DATEADD('day', -3,  CURRENT_DATE())
      AND WAREHOUSE_ID > 0
),
f_q AS (
    -- Warehouse-bound only, to match l_q. The hourly fact retains warehouse-less
    -- USE/SHOW/DESCRIBE/cache-hit rows (NULL warehouse) that the live side's
    -- warehouse-bound filter excludes; without this predicate the two sides
    -- count different populations and read a permanent ~+97% false drift.
    SELECT SUM(QUERY_COUNT) AS V
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -9, CURRENT_DATE())
      AND HOUR_TS <  DATEADD('day', -2, CURRENT_DATE())
      -- Hardened vs BOTH warehouse-less conventions (owner screenshot 2026-08-17:
      -- +93.13% BAD): repo loaders write NULL here (excluded by NOT IN's NULL
      -- semantics, same as the old IS NOT NULL), while sibling facts/backfills
      -- use the STRING 'NONE' — if any such fill ever touched this table, IS NOT
      -- NULL excluded nothing. Root cause of the observed drift is UNCONFIRMED
      -- from repo code alone; if it persists past this hardening, the fact side
      -- is genuinely over-counting (dedup/overlap) — investigate the loader.
      AND UPPER(WAREHOUSE_NAME) NOT IN ('NONE', 'CLOUD_SERVICES_ONLY')
),
l_q AS (
    SELECT COUNT(*) AS V
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -9, CURRENT_DATE())
      AND START_TIME <  DATEADD('day', -2, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
)
SELECT 'Billed credits (28d, mart vs metering-daily)' AS CHECK_NAME,
       'credits' AS UNIT,
       ROUND(f_met.V, 2) AS FACT_VALUE, ROUND(l_met.V, 2) AS LIVE_VALUE,
       ROUND(100 * (f_met.V - l_met.V) / NULLIF(l_met.V, 0), 2) AS DRIFT_PCT
FROM f_met, l_met
UNION ALL
SELECT 'Warehouse credits (28d, mart vs warehouse-metering)',
       'credits',
       ROUND(f_wh.V, 2), ROUND(l_wh.V, 2),
       ROUND(100 * (f_wh.V - l_wh.V) / NULLIF(l_wh.V, 0), 2)
FROM f_wh, l_wh
UNION ALL
SELECT 'Query count (7d, mart vs query-history)',
       'statements',
       f_q.V, l_q.V,
       ROUND(100 * (f_q.V - l_q.V) / NULLIF(l_q.V, 0), 2)
FROM f_q, l_q
"""


def mart_vs_live_ai_recon() -> str:
    """Next-Fifty #25: FACT_AI_USAGE_DAILY vs the Cortex usage views it is loaded from, same 28d
    lag-safe window as mart_vs_live_recon (the DAILY task reloads only 3 days, so days older
    than today-3 are final). Day keys MIRROR THE LOADER byte-for-byte (USAGE_TIME::DATE /
    START_TIME::DATE, session tz = account tz) -- this checks loader fidelity, so it must not
    re-key days differently from V146. The Functions live side is a PLAIN SUM(CREDITS) (one row
    per source row, no FLATTEN): the independent answer the loader's FLATTEN + INDEX=0 dedupe
    must equal. Same columns as mart_vs_live_recon so Admin can concat the frames."""
    return f"""
WITH f_code AS (
    SELECT SUM(CREDITS) AS V
    FROM {core_object("FACT_AI_USAGE_DAILY")}
    WHERE SOURCE IN ('Snowsight', 'CLI')
      AND DAY >= DATEADD('day', -31, CURRENT_DATE())
      AND DAY <  DATEADD('day', -3,  CURRENT_DATE())
),
l_code AS (
    SELECT SUM(COALESCE(TOKEN_CREDITS, 0)) AS V
    FROM (
        SELECT USAGE_TIME, TOKEN_CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
        UNION ALL
        SELECT USAGE_TIME, TOKEN_CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
    )
    WHERE USAGE_TIME::DATE >= DATEADD('day', -31, CURRENT_DATE())
      AND USAGE_TIME::DATE <  DATEADD('day', -3,  CURRENT_DATE())
),
f_fn AS (
    SELECT SUM(CREDITS) AS V
    FROM {core_object("FACT_AI_USAGE_DAILY")}
    WHERE SOURCE = 'Functions'
      AND DAY >= DATEADD('day', -31, CURRENT_DATE())
      AND DAY <  DATEADD('day', -3,  CURRENT_DATE())
),
l_fn AS (
    SELECT SUM(COALESCE(CREDITS, 0)) AS V
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
    WHERE START_TIME >= DATEADD('day', -33, CURRENT_DATE())
      AND START_TIME::DATE >= DATEADD('day', -31, CURRENT_DATE())
      AND START_TIME::DATE <  DATEADD('day', -3,  CURRENT_DATE())
)
SELECT 'AI credits - Cortex Code (28d, mart vs CORTEX_CODE_* views)' AS CHECK_NAME,
       'credits' AS UNIT,
       ROUND(f_code.V, 4) AS FACT_VALUE, ROUND(l_code.V, 4) AS LIVE_VALUE,
       ROUND(100 * (COALESCE(f_code.V, 0) - COALESCE(l_code.V, 0)) / NULLIF(l_code.V, 0), 2) AS DRIFT_PCT
FROM f_code, l_code
UNION ALL
SELECT 'AI credits - Functions (28d, mart vs CORTEX_AI_FUNCTIONS_USAGE_HISTORY)',
       'credits',
       ROUND(f_fn.V, 4), ROUND(l_fn.V, 4),
       ROUND(100 * (COALESCE(f_fn.V, 0) - COALESCE(l_fn.V, 0)) / NULLIF(l_fn.V, 0), 2)
FROM f_fn, l_fn
"""


def fleet_query_stats(days: int = 7, page: str = "") -> str:
    """Slow/failed fetches across ALL viewers (APP_QUERY_TELEMETRY, V021).

    Only rows the app chose to persist land here (>=2s or failed), so this is
    the regression surface, not a complete census — the note on the panel
    says so.

    C6: ``page`` narrows to one page. The unfiltered call is LIMIT 40 by p95,
    so a page can be ranked a top tuning target and still have every one of its
    slow keys sitting below that cut — the drill-down needs to ask for the page
    directly before it can honestly say "nothing slow persisted".
    """
    days = max(1, min(int(days or 7), 90))
    page_filter = (f"\n  AND PAGE = {sql_literal(str(page))}" if str(page or "").strip() else "")
    return f"""
SELECT
    PAGE,
    QUERY_KEY,
    COUNT(*)                                   AS SLOW_OR_FAILED,
    COUNT_IF(NOT OK)                           AS FAILURES,
    ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.5))  AS P50_MS,
    ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.95)) AS P95_MS,
    COUNT(DISTINCT ROLE_NAME)                  AS ROLES_AFFECTED,
    MAX(AT)                                    AS NEWEST,
    -- rec18 (V064): the slowest fetch's QUERY_ID, to deep-link the row to
    -- ACCOUNT_USAGE.QUERY_HISTORY (scan/spill/queue/compile). NULL for pre-V064
    -- rows and cache hits (no server query ran).
    MAX_BY(QUERY_ID, IFF(QUERY_ID IS NOT NULL, ELAPSED_MS, NULL)) AS SLOWEST_QUERY_ID
FROM {core_object("APP_QUERY_TELEMETRY")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()){page_filter}
GROUP BY PAGE, QUERY_KEY
ORDER BY P95_MS DESC NULLS LAST
LIMIT 40
"""

def rule_metric_kinds(days: int = 90) -> str:
    """Raw material for threshold suggestions: each resolved event's metric
    value and its resolution kind (V021). The tuning logic aggregates.

    D7: UNTAGGED_N rides along per rule — the count of that rule's resolved
    events in the SAME window that were closed with no resolution kind. The
    suggestion is computed only from ACTIONED/NOISE rows, so a rule with 4
    tagged and 196 untagged closes produces advice that LOOKS as authoritative
    as one with 200 tagged closes. The column is the denominator that says so;
    the display side decides how to warn."""
    days = max(7, min(int(days or 90), 365))
    return f"""
WITH resolved AS (
    SELECT RULE_ID, METRIC_VALUE, RESOLUTION_KIND
    FROM {core_object("ALERT_EVENTS")}
    WHERE STATUS = 'RESOLVED'
      AND RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
),
untagged AS (
    -- Same "untagged" spelling as alert_fatigue: NULL or empty kind.
    SELECT RULE_ID, COUNT(*) AS UNTAGGED_N
    FROM resolved
    WHERE COALESCE(RESOLUTION_KIND, '') = ''
    GROUP BY RULE_ID
)
SELECT r.RULE_ID, r.METRIC_VALUE, r.RESOLUTION_KIND,
       COALESCE(u.UNTAGGED_N, 0) AS UNTAGGED_N
FROM resolved r
LEFT JOIN untagged u ON u.RULE_ID = r.RULE_ID
WHERE r.RESOLUTION_KIND IN ('ACTIONED', 'NOISE')
  AND r.METRIC_VALUE IS NOT NULL
LIMIT 5000
"""


def fact_warehouse_pressure(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """ops_sql.warehouse_pressure contract from FACT_QUERY_HOURLY (r23 #1:
    the live scan was a top fleet pain key at 17.8s p50). Queued seconds,
    spill and counts are exact sums of the hourly fact; P95_ELAPSED_SEC is
    the PEAK hourly-group p95 — the caller labels it. Live stays as the
    labeled fallback for pre-fact windows."""
    days = max(1, min(int(days or 7), 90))
    where = [scope_window_where("HOUR_TS", days, bounds=bounds),
             "WAREHOUSE_NAME IS NOT NULL"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT
    WAREHOUSE_NAME,
    SUM(QUERY_COUNT) AS QUERY_COUNT,
    ROUND(SUM(COALESCE(QUEUED_SEC_SUM, 0)), 1) AS QUEUED_SEC,
    ROUND(SUM(COALESCE(SPILL_REMOTE_GB, 0)), 2) AS SPILL_REMOTE_GB,
    MAX(COALESCE(P95_ELAPSED_SEC, 0)) AS P95_ELAPSED_SEC
FROM {core_object("FACT_QUERY_HOURLY")}
WHERE {" AND ".join(where)}
GROUP BY 1
HAVING SUM(COALESCE(QUEUED_SEC_SUM, 0)) > 0
    OR SUM(COALESCE(SPILL_REMOTE_GB, 0)) > 0
ORDER BY QUEUED_SEC DESC
LIMIT 50
"""


def warehouse_capacity_daily(days: int = 90, company: str = "ALL") -> str:
    """Complete-day warehouse pressure, workload, spend, and capacity changes.

    This is a mart-only forecast input. It deliberately excludes today's
    partial day and does not fabricate zero rows for missing telemetry.
    """
    days = max(30, min(int(days or 90), 365))
    company_filter = ""
    if str(company or "ALL").upper() != "ALL":
        company_filter = f" AND q.COMPANY = {sql_literal(company)}"
    return f"""
WITH query_daily AS (
    SELECT DATE(q.HOUR_TS) AS DAY,
           q.WAREHOUSE_NAME,
           q.COMPANY,
           SUM(COALESCE(q.QUERY_COUNT, 0)) AS QUERY_COUNT,
           SUM(COALESCE(q.QUEUED_SEC_SUM, 0)) / 60.0 AS QUEUED_MIN,
           SUM(COALESCE(q.SPILL_REMOTE_GB, 0)) AS SPILL_REMOTE_GB,
           MAX(COALESCE(q.P95_ELAPSED_SEC, 0)) AS P95_ELAPSED_SEC
    FROM {core_object("FACT_QUERY_HOURLY")} q
    WHERE q.HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())
      AND DATE(q.HOUR_TS) < CURRENT_DATE()
      AND q.WAREHOUSE_NAME IS NOT NULL{company_filter}
    GROUP BY 1, 2, 3
),
source_freshness AS (
    SELECT MAX(DATE(HOUR_TS)) AS SOURCE_LATEST_DAY
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())
      AND DATE(HOUR_TS) < CURRENT_DATE()
),
warehouse_daily AS (
    SELECT DAY, WAREHOUSE_NAME, COMPANY, SUM(COALESCE(CREDITS_TOTAL, 0)) AS CREDITS_TOTAL
    FROM {core_object("FACT_WAREHOUSE_DAILY")}
    WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
      AND DAY < CURRENT_DATE()
    GROUP BY 1, 2, 3
),
changes AS (
    SELECT DATE(CHANGE_SEEN_AT) AS DAY, WAREHOUSE_NAME, COMPANY, COUNT(*) AS CHANGE_EVENTS
    FROM {core_object("WAREHOUSE_CHANGE_REGISTRY")}
    WHERE CHANGE_SEEN_AT >= DATEADD('day', -{days}, CURRENT_DATE())
      AND SETTING IN ('SIZE', 'MIN_CLUSTERS', 'MAX_CLUSTERS', 'SCALING_POLICY')
    GROUP BY 1, 2, 3
)
SELECT q.DAY, q.WAREHOUSE_NAME, q.COMPANY, q.QUERY_COUNT, q.QUEUED_MIN,
       q.SPILL_REMOTE_GB, q.P95_ELAPSED_SEC,
       COALESCE(w.CREDITS_TOTAL, 0) AS CREDITS_TOTAL,
       COALESCE(c.CHANGE_EVENTS, 0) AS CHANGE_EVENTS,
       s.SOURCE_LATEST_DAY
FROM query_daily q
CROSS JOIN source_freshness s
LEFT JOIN warehouse_daily w
       ON w.DAY = q.DAY AND w.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND w.COMPANY = q.COMPANY
LEFT JOIN changes c
       ON c.DAY = q.DAY AND c.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND c.COMPANY = q.COMPANY
ORDER BY q.WAREHOUSE_NAME, q.DAY
"""


def score_inputs_daily(days: int = 30) -> str:
    """Per-day signals for the RETRO platform-score trend, from facts +
    alert history. The live score adds stale-source/open-action penalties
    that facts don't carry per day — panel labels the difference."""
    days = max(7, min(int(days or 30), 120))
    return f"""
WITH spend AS (
    SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED,
           SUM(CASE WHEN {_AI_SERVICE_PRED} THEN CREDITS_BILLED ELSE 0 END) AS CREDITS_BILLED_AI
    FROM {core_object("FACT_METERING_DAILY")}
    WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY DAY
),
q AS (
    SELECT DATE(HOUR_TS) AS DAY,
           SUM(QUERY_COUNT) AS QUERY_COUNT,
           SUM(FAILED_COUNT) AS FAILED_COUNT,
           SUM(QUEUED_SEC_SUM) AS QUEUED_SEC,
           SUM(SPILL_REMOTE_GB) AS SPILL_GB
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY 1
),
t AS (
    SELECT DAY, SUM(RUNS) AS TASK_RUNS, SUM(FAILED) AS TASK_FAILED
    FROM {core_object("FACT_TASK_DAILY")}
    WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY DAY
),
a AS (
    SELECT DATE(RAISED_AT) AS DAY,
           COUNT_IF(UPPER(SEVERITY) = 'CRITICAL') AS CRIT_RAISED,
           COUNT_IF(UPPER(SEVERITY) = 'HIGH') AS HIGH_RAISED
    FROM {core_object("ALERT_EVENTS")}
    WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY 1
)
SELECT spend.DAY,
       spend.CREDITS_BILLED,
       spend.CREDITS_BILLED_AI,
       COALESCE(q.QUERY_COUNT, 0)  AS QUERY_COUNT,
       COALESCE(q.FAILED_COUNT, 0) AS FAILED_COUNT,
       COALESCE(q.QUEUED_SEC, 0)   AS QUEUED_SEC,
       COALESCE(q.SPILL_GB, 0)     AS SPILL_GB,
       COALESCE(t.TASK_RUNS, 0)    AS TASK_RUNS,
       COALESCE(t.TASK_FAILED, 0)  AS TASK_FAILED,
       COALESCE(a.CRIT_RAISED, 0)  AS CRIT_RAISED,
       COALESCE(a.HIGH_RAISED, 0)  AS HIGH_RAISED
FROM spend
LEFT JOIN q ON q.DAY = spend.DAY
LEFT JOIN t ON t.DAY = spend.DAY
LEFT JOIN a ON a.DAY = spend.DAY
ORDER BY spend.DAY
"""

def day_spend_movers(day: object, company: str = "ALL") -> str:
    """Replay: each warehouse's credits on DAY vs its trailing-14d baseline.
    Company-scoped on BOTH CTEs — Trexis rows must not surface under an
    ALFA replay (live finding, 2026-07-09)."""
    from app.data.common import day_literal

    lit = day_literal(day)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND COMPANY = {sql_literal(company)}")
    return f"""
WITH base AS (
    SELECT WAREHOUSE_NAME, AVG(CREDITS_TOTAL) AS BASELINE_CREDITS
    FROM {core_object("FACT_WAREHOUSE_DAILY")}
    WHERE DAY BETWEEN DATEADD('day', -14, {lit}) AND DATEADD('day', -1, {lit}){comp}
    GROUP BY WAREHOUSE_NAME
),
day_of AS (
    SELECT WAREHOUSE_NAME, COMPANY, SUM(CREDITS_TOTAL) AS CREDITS_TOTAL
    FROM {core_object("FACT_WAREHOUSE_DAILY")}
    WHERE DAY = {lit}{comp}
    GROUP BY WAREHOUSE_NAME, COMPANY
)
SELECT d.WAREHOUSE_NAME, d.COMPANY, d.CREDITS_TOTAL,
       COALESCE(b.BASELINE_CREDITS, 0)                     AS BASELINE_CREDITS,
       d.CREDITS_TOTAL - COALESCE(b.BASELINE_CREDITS, 0)   AS DELTA_CREDITS
FROM day_of d
LEFT JOIN base b ON b.WAREHOUSE_NAME = d.WAREHOUSE_NAME
ORDER BY ABS(DELTA_CREDITS) DESC
LIMIT 40
"""


def day_activity(day: object, company: str = "ALL") -> str:
    """Replay: the day's query totals next to the trailing-14d daily baseline.
    Both the day and its baseline honor the company scope."""
    from app.data.common import day_literal

    lit = day_literal(day)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND COMPANY = {sql_literal(company)}")
    return f"""
WITH day_of AS (
    SELECT SUM(QUERY_COUNT) AS QUERY_COUNT, SUM(FAILED_COUNT) AS FAILED_COUNT,
           SUM(QUEUED_SEC_SUM) AS QUEUED_SEC, SUM(SPILL_REMOTE_GB) AS SPILL_GB
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE DATE(HOUR_TS) = {lit}{comp}
),
base AS (
    -- Divide by days PRESENT in the window: a loader gap or quiet weekend
    -- must not deflate the baseline and over-flag the replay day.
    SELECT SUM(QUERY_COUNT) / NULLIF(COUNT(DISTINCT DATE(HOUR_TS)), 0) AS BASELINE_QUERIES,
           SUM(FAILED_COUNT) / NULLIF(COUNT(DISTINCT DATE(HOUR_TS)), 0) AS BASELINE_FAILED
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE DATE(HOUR_TS) BETWEEN DATEADD('day', -14, {lit}) AND DATEADD('day', -1, {lit}){comp}
)
SELECT day_of.*, base.BASELINE_QUERIES, base.BASELINE_FAILED FROM day_of, base
"""


def day_task_failures(day: object, company: str = "ALL") -> str:
    from app.data.common import day_literal

    lit = day_literal(day)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND COMPANY = {sql_literal(company)}")
    return f"""
SELECT DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, LAST_ERROR,
       -- Uncapped day total (pre-LIMIT): the day-replay headline sums THIS, not the
       -- top-50-by-FAILED display frame, so >50 distinct failing tasks isn't undercounted.
       SUM(FAILED) OVER () AS TOTAL_FAILED_WIN
FROM {core_object("FACT_TASK_DAILY")}
WHERE DAY = {lit} AND FAILED > 0{comp}
ORDER BY FAILED DESC
LIMIT 50
"""


def day_alerts(day: object, company: str = "ALL") -> str:
    """Replay alerts: company rows PLUS account-level (COMPANY='ALL') rows —
    same convention as open_alert_events."""
    from app.data.common import day_literal

    lit = day_literal(day)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND (COMPANY = {sql_literal(company)} OR UPPER(COMPANY) = 'ALL')")
    return f"""
SELECT RAISED_AT, SEVERITY, RULE_ID, COMPANY, TITLE, STATUS
FROM {core_object("ALERT_EVENTS")}
WHERE DATE(RAISED_AT) = {lit}{comp}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END, RAISED_AT
LIMIT 200
"""


def drill_history(months: int = 14) -> str:
    """Fire-drill events (opt-in snowflake/alert_drill.sql), newest first."""
    months = max(3, min(int(months or 14), 36))
    return f"""
SELECT RAISED_AT, NOTIFIED_AT, ACK_AT, STATUS, TITLE
FROM {core_object("ALERT_EVENTS")}
WHERE RULE_ID = 'OPS_ALERT_DRILL'
  AND RAISED_AT >= DATEADD('month', -{months}, CURRENT_TIMESTAMP())
ORDER BY RAISED_AT DESC
LIMIT 40
"""


def metering_restatements(days: int = 60) -> str:
    """Days whose metering row changed >=48h after the day closed — the
    'why did the number we reported move?' detector (LOAD_TS updates on the
    loader MERGE). v1 of the reported-numbers audit: flags restated days;
    first-reported snapshots would need a fact-snapshot migration.
    """
    days = max(14, min(int(days or 60), 180))
    return f"""
SELECT DAY,
       SUM(CREDITS_BILLED) AS CREDITS_BILLED,
       MAX(LOAD_TS)        AS LAST_LOADED_AT,
       DATEDIFF('hour', DATEADD('day', 1, DAY)::TIMESTAMP_NTZ, MAX(LOAD_TS)) AS RESTATED_HOURS_AFTER_CLOSE
FROM {core_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY DAY
HAVING RESTATED_HOURS_AFTER_CLOSE >= 48
ORDER BY DAY DESC
LIMIT 60
"""


# ---------------------------------------------------------------------------
# Wave 2 riders (v4.12.0): delivery SLOs, fatigue, acceptance, app telemetry.
# All read OVERWATCH-owned tables — no ACCOUNT_USAGE.
# ---------------------------------------------------------------------------

# The sender (SP_NOTIFY_WEBHOOK, V064) keeps a CRITICAL event eligible to send for 7
# days and everything else for 24h. The delivery-backlog / eligibility panels must
# mirror that window (alias `e` = ALERT_EVENTS) or they undercount a CRITICAL that is
# starved past 24h — mislabeling it "delivered/quiet/expired" while the drainer is
# still retrying it. Keep this in lockstep with the SP_NOTIFY_WEBHOOK send predicate.
_SEND_ELIGIBLE_SINCE = ("CASE WHEN e.SEVERITY = 'CRITICAL' "
                        "THEN DATEADD('day', -7, CURRENT_TIMESTAMP()) "
                        "ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END")


def last_delivery_health() -> str:
    """ONE ROW PER ENABLED ROUTE answering the question the owner could NOT answer on
    2026-07-31: 'no alerts today — is it QUIET, or is delivery BROKEN?'

    Those are different states and the app never separated them. The sender is a
    per-route DIGEST bounded to a 24h window, and alert scanning dedupes on keys that
    mostly carry the day — so a chronic condition raises ONCE and a genuinely quiet
    stretch produces zero messages. Silence alone proves nothing.

    Per-route, not global (Codex #29): the old builder returned global MAX(SENT_AT) +
    a global fail count, so ONE healthy route hid a dead sibling (a recent send on Slack
    masked PagerDuty going days without one) and ONE bad route reddened a card a dozen
    healthy routes shared. Each enabled route now carries its OWN last send, eligible
    backlog, and failure history; the card aggregates the WORST state for the headline
    and names the stuck/failing route.

    ELIGIBLE_NOW mirrors the sender's own eligibility predicate per route (open, inside
    its 24h window, config-joined, matching THIS route's family/company/severity, not
    already in the ledger for THIS route).

    Recovery-aware failure state (Codex #42): a route is only FAILING_NOW when its
    LATEST failure is newer than its LATEST success — a later successful send clears the
    red, instead of any failure in the rolling window pinning it red for 24h. CONSEC_FAILS
    counts failures since the last success. #15: the LATEST-failure signal (LAST_FAILURE_AT/
    LAST_FAILURE) and CONSEC_FAILS carry NO 24h cutoff, so a route stranded >24h ago (failed,
    then went quiet) still shows its failure instead of reading 'Quiet'.

    STUCK (Codex #41, refined #14) keys on the age of the OLDEST undelivered event
    (OLDEST_ELIGIBLE = MIN(RAISED_AT)), NOT last-sent recency: it fires once that age passes
    one sender cycle + grace (hourly cadence -> 90m). A brand-new event right after a quiet
    week is young and NOT stuck; an old backlog is stuck even when an unrelated recent send
    would have reset a last-sent test. EXPIRED_UNDELIVERED (open, route-matching, undelivered
    events that aged past the sender's 24h window) is folded into OLDEST_ELIGIBLE (#15), so a
    stranded route reads STUCK rather than 'Quiet'.

    Failures are attributed per route from APP_ERROR_LOG.CONTEXT, which the sender writes
    as 'route <route_id> integration <name> - ...' (V064); APP_ERROR_LOG has no route
    column, so SPLIT_PART on that context is the only per-route key available.

    Always returns >=1 row: when zero routes are enabled, a single synthetic row with
    ENABLED_ROUTES = 0 lets the card render the 'nothing can be delivered' state.
    Reads only OVERWATCH-owned tables; no ACCOUNT_USAGE."""
    return f"""
WITH sent AS (
    -- per-route last CONFIRMED send = the success signal (#42)
    SELECT ROUTE_ID, MAX(SENT_AT) AS LAST_SENT_AT
    FROM {core_object("ALERT_DELIVERIES")}
    GROUP BY ROUTE_ID
),
fails AS (
    -- per-route send failures; ROUTE_ID parsed from the CONTEXT the sender writes
    -- ('route <id> integration <name> - ...'). SPLIT_PART token 2 is the id.
    -- #15: NO rolling 24h cutoff on the worst-failure SIGNAL — a route stranded/expired
    -- >24h ago (failed, then went quiet) used to lose its failure entirely and read
    -- 'Quiet'. LAST_FAILURE_AT/LAST_FAILURE are each route's LATEST failure via MAX/MAX_BY
    -- regardless of age; ROUTE_FAILS_24H stays the bounded 24h COUNT for the KPI.
    SELECT SPLIT_PART(CONTEXT, ' ', 2) AS ROUTE_ID,
           COUNT_IF(LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS ROUTE_FAILS_24H,
           MAX(LOGGED_AT) AS LAST_FAILURE_AT,
           MAX_BY(LEFT(ERROR_MESSAGE, 200), LOGGED_AT) AS LAST_FAILURE
    FROM {core_object("APP_ERROR_LOG")}
    WHERE PAGE = 'NotifyWebhook' AND ERROR_TYPE = 'route_send_failed'
    GROUP BY 1
),
consec AS (
    -- consecutive failures SINCE the last success (#42): fails newer than this route's
    -- last confirmed send (all of them when it has never sent). #15: no 24h cutoff here
    -- either — a route failing for days with no success must count them all, so the card's
    -- 'N failure(s) since its last success' matches the un-capped FAILING_NOW signal.
    SELECT SPLIT_PART(a.CONTEXT, ' ', 2) AS ROUTE_ID,
           COUNT(*) AS CONSEC_FAILS
    FROM {core_object("APP_ERROR_LOG")} a
    LEFT JOIN sent s ON s.ROUTE_ID = SPLIT_PART(a.CONTEXT, ' ', 2)
    WHERE a.PAGE = 'NotifyWebhook' AND a.ERROR_TYPE = 'route_send_failed'
      AND a.LOGGED_AT > COALESCE(s.LAST_SENT_AT, '1970-01-01'::TIMESTAMP_NTZ)
    GROUP BY 1
),
eligible AS (
    -- per-route OPEN + undelivered events matching this route's predicate (mirrors the
    -- sender). #15: split into ELIGIBLE_NOW (inside the sender's 24h window) and
    -- EXPIRED_UNDELIVERED (aged PAST the window still open + undelivered — a stranded
    -- backlog ELIGIBLE_NOW alone can't see, which used to make a broken route read
    -- 'Quiet'). #14: OLDEST_ELIGIBLE = MIN(RAISED_AT) over ALL such events is the age the
    -- STUCK test keys on, so a young event is never stuck and an old backlog always is.
    SELECT r.ROUTE_ID,
           COUNT(DISTINCT CASE WHEN e.RAISED_AT >= {_SEND_ELIGIBLE_SINCE}
                               THEN e.EVENT_ID END) AS ELIGIBLE_NOW,
           COUNT(DISTINCT CASE WHEN e.RAISED_AT <  {_SEND_ELIGIBLE_SINCE}
                               THEN e.EVENT_ID END) AS EXPIRED_UNDELIVERED,
           MIN(e.RAISED_AT) AS OLDEST_ELIGIBLE
    FROM {core_object("ALERT_ROUTES")} r
    JOIN {core_object("ALERT_EVENTS")} e
      ON e.STATUS = 'OPEN'
    JOIN {core_object("ALERT_CONFIG")} c
      ON c.RULE_ID = e.RULE_ID
     AND (r.FAMILY = 'ALL' OR c.FAMILY = r.FAMILY)
    WHERE r.ENABLED
      AND (COALESCE(r.COMPANY_FILTER, 'ALL') = 'ALL'
           OR e.COMPANY = r.COMPANY_FILTER OR UPPER(e.COMPANY) = 'ALL')
      AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                          WHEN 'MEDIUM' THEN 2 ELSE 1 END
          >= CASE r.MIN_SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                                 WHEN 'MEDIUM' THEN 2 ELSE 1 END
      AND NOT EXISTS (SELECT 1 FROM {core_object("ALERT_DELIVERIES")} d
                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r.ROUTE_ID)
    GROUP BY r.ROUTE_ID
),
summary AS (
    SELECT COUNT(*) AS ENABLED_ROUTES FROM {core_object("ALERT_ROUTES")} WHERE ENABLED
),
dist_eligible AS (
    -- Account-wide DISTINCT eligible events inside the sender's 24h window. The card's
    -- 'Eligible to send now' headline is a count of DISTINCT events that will actually
    -- be delivered — NOT the sum of per-route ELIGIBLE_NOW, which double-counts one
    -- event that matches several enabled routes. Same eligibility predicate as the
    -- per-route `eligible` CTE (open, config-joined, family/company/severity, not yet
    -- delivered to that route), collapsed to DISTINCT e.EVENT_ID across all routes.
    SELECT COUNT(DISTINCT e.EVENT_ID) AS DISTINCT_ELIGIBLE_NOW
    FROM {core_object("ALERT_ROUTES")} r
    JOIN {core_object("ALERT_EVENTS")} e
      ON e.STATUS = 'OPEN'
    JOIN {core_object("ALERT_CONFIG")} c
      ON c.RULE_ID = e.RULE_ID
     AND (r.FAMILY = 'ALL' OR c.FAMILY = r.FAMILY)
    WHERE r.ENABLED
      AND e.RAISED_AT >= {_SEND_ELIGIBLE_SINCE}
      AND (COALESCE(r.COMPANY_FILTER, 'ALL') = 'ALL'
           OR e.COMPANY = r.COMPANY_FILTER OR UPPER(e.COMPANY) = 'ALL')
      AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                          WHEN 'MEDIUM' THEN 2 ELSE 1 END
          >= CASE r.MIN_SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                                 WHEN 'MEDIUM' THEN 2 ELSE 1 END
      AND NOT EXISTS (SELECT 1 FROM {core_object("ALERT_DELIVERIES")} d
                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r.ROUTE_ID)
)
SELECT r.ROUTE_ID,
       r.INTEGRATION_NAME,
       s.LAST_SENT_AT,
       DATEDIFF('minute', s.LAST_SENT_AT, CURRENT_TIMESTAMP()) AS MINUTES_SINCE,
       COALESCE(el.ELIGIBLE_NOW, 0) AS ELIGIBLE_NOW,
       COALESCE(el.EXPIRED_UNDELIVERED, 0) AS EXPIRED_UNDELIVERED,
       COALESCE(f.ROUTE_FAILS_24H, 0) AS ROUTE_FAILS_24H,
       f.LAST_FAILURE_AT,
       s.LAST_SENT_AT AS LAST_SUCCESS_AT,
       COALESCE(cf.CONSEC_FAILS, 0) AS CONSEC_FAILS,
       f.LAST_FAILURE,
       -- #42: red ONLY when the latest failure is newer than the latest success
       (f.LAST_FAILURE_AT IS NOT NULL
        AND (s.LAST_SENT_AT IS NULL OR f.LAST_FAILURE_AT > s.LAST_SENT_AT)) AS FAILING_NOW,
       -- #14: STUCK keys on the OLDEST undelivered event's age, NOT last-sent recency.
       -- Sender cadence is hourly (TASK_ALERT_SCAN -> TASK_ALERT_NOTIFY off TASK_LOAD_HOURLY),
       -- so the bar is one cycle + a 30m grace = 90m. A brand-new event right after a quiet
       -- week is young -> not stuck (the old last-sent test wrongly reddened it); an old
       -- backlog is stuck even if an unrelated send just landed (which used to mask it).
       -- OLDEST_ELIGIBLE spans expired (>24h) undelivered events too (#15), so a stranded
       -- route is stuck, not 'Quiet'.
       (el.OLDEST_ELIGIBLE IS NOT NULL
        AND DATEDIFF('minute', el.OLDEST_ELIGIBLE, CURRENT_TIMESTAMP()) >= 90) AS STUCK,
       el.OLDEST_ELIGIBLE,
       sm.ENABLED_ROUTES,
       -- Same on every row (account-wide): the card reads this once for the
       -- 'Eligible to send now' headline instead of SUM(ELIGIBLE_NOW).
       de.DISTINCT_ELIGIBLE_NOW
FROM {core_object("ALERT_ROUTES")} r
CROSS JOIN summary sm
CROSS JOIN dist_eligible de
LEFT JOIN sent s      ON s.ROUTE_ID = r.ROUTE_ID
LEFT JOIN fails f     ON f.ROUTE_ID = r.ROUTE_ID
LEFT JOIN consec cf   ON cf.ROUTE_ID = r.ROUTE_ID
LEFT JOIN eligible el ON el.ROUTE_ID = r.ROUTE_ID
WHERE r.ENABLED
UNION ALL
-- no enabled route: one synthetic row so the card can render 'nothing can be delivered'
SELECT NULL, NULL, NULL, NULL, 0, 0, 0, NULL, NULL, 0, NULL, FALSE, FALSE, NULL, sm.ENABLED_ROUTES, 0
FROM summary sm
WHERE sm.ENABLED_ROUTES = 0
"""


def delivery_slo_summary(days: int = 30) -> str:
    """One row: did alerts leave the building, how fast, which criticals
    never did (30+ min old, zero delivery rows), and route failures."""
    days = bounded_days(days, 90)
    return f"""
WITH d AS (
    SELECT EVENT_ID, MIN(SENT_AT) AS FIRST_SENT
    FROM {core_object("ALERT_DELIVERIES")}
    WHERE SENT_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
    GROUP BY EVENT_ID
),
e AS (
    SELECT EVENT_ID, RAISED_AT, SEVERITY, STATUS, COMPANY, RULE_ID
    FROM {core_object("ALERT_EVENTS")}
    WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
),
undc AS (
    -- Alert-hunt #6: the windowed critical-undelivered set, ROUTE-level (an eligible
    -- enabled route with no delivery), mirroring health_strip.und_crit but bounded to
    -- this report window. Route-level so a CRIT delivered to a sibling info-route but
    -- failing its paging route still counts as undelivered. JOIN anti-join (no nested
    -- correlated subquery); the UNION arm keeps the config-gap (no-route) critical too.
    SELECT DISTINCT e.EVENT_ID
    FROM e
    LEFT JOIN {core_object("ALERT_CONFIG")} c ON c.RULE_ID = e.RULE_ID
    JOIN {core_object("ALERT_ROUTES")} r
      ON r.ENABLED
     AND (r.FAMILY = 'ALL' OR c.FAMILY = r.FAMILY)
     AND (COALESCE(r.COMPANY_FILTER, 'ALL') = 'ALL'
          OR e.COMPANY = r.COMPANY_FILTER OR UPPER(e.COMPANY) = 'ALL')
    LEFT JOIN {core_object("ALERT_DELIVERIES")} dd
      ON dd.EVENT_ID = e.EVENT_ID AND dd.ROUTE_ID = r.ROUTE_ID
    WHERE UPPER(e.SEVERITY) = 'CRITICAL' AND e.STATUS = 'OPEN'
      AND e.RAISED_AT <= DATEADD('minute', -30, CURRENT_TIMESTAMP())
      AND dd.EVENT_ID IS NULL
    UNION
    SELECT e.EVENT_ID
    FROM e
    WHERE UPPER(e.SEVERITY) = 'CRITICAL' AND e.STATUS = 'OPEN'
      AND e.RAISED_AT <= DATEADD('minute', -30, CURRENT_TIMESTAMP())
      AND NOT EXISTS (SELECT 1 FROM {core_object("ALERT_DELIVERIES")} d0
                      WHERE d0.EVENT_ID = e.EVENT_ID)
)
SELECT
    (SELECT COUNT(*) FROM e) AS EVENTS_RAISED,
    (SELECT COUNT(*) FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS EVENTS_DELIVERED,
    (SELECT ROUND(MEDIAN(DATEDIFF('second', e.RAISED_AT, d.FIRST_SENT)) / 60.0, 1)
       FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS MEDIAN_MIN,
    (SELECT ROUND(APPROX_PERCENTILE(DATEDIFF('second', e.RAISED_AT, d.FIRST_SENT), 0.95) / 60.0, 1)
       FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS P95_MIN,
    -- Same route-level membership as health_strip's UNDELIVERED_N (see undc), but this is
    -- a WINDOWED SLO report: e is bounded to the last {days}d, so a critical still OPEN and
    -- undelivered past the window drops out HERE while health_strip's always-on banner
    -- (unbounded) still shows it red. That divergence is intended — the banner is the
    -- authoritative "is anything broken right now" view; this card is the N-day report.
    (SELECT COUNT(*) FROM undc) AS UNDELIVERED_CRITICALS_30M,
    -- Alert-hunt fix: a plain COUNT(*) counted every hourly retry row, so one route
    -- broken for a month read as ~720 "failures". SP_NOTIFY_WEBHOOK logs one
    -- route_send_failed row per failing route PER RUN, with the route id + integration
    -- in CONTEXT; collapse to distinct (route, day) so a persistent outage counts as
    -- route-days, not runs (mirrors the undelivered_expired once-per-24h grain).
    (SELECT COUNT(DISTINCT CONTEXT || '|' || TO_VARCHAR(DATE_TRUNC('day', LOGGED_AT)))
       FROM {core_object("APP_ERROR_LOG")}
      WHERE ERROR_TYPE = 'route_send_failed'
        AND LOGGED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())) AS ROUTE_FAILURES,
    -- rec19 (V064): the loud signal SP_NOTIFY_WEBHOOK itself raises when an OPEN
    -- eligible event ages past the 24h delivery window with no successful send.
    -- The app previously ignored this proc-emitted row and re-derived only its own
    -- 30-min critical count; surfacing it closes the loop with the drainer.
    (SELECT COUNT(*) FROM {core_object("APP_ERROR_LOG")}
      WHERE ERROR_TYPE = 'undelivered_expired'
        AND LOGGED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())) AS EXPIRED_UNDELIVERED
"""


def route_backlog() -> str:
    """rec19 (V064): per enabled route, how many OPEN events are eligible-but-
    UNDELIVERED right now (the backlog SP_NOTIFY_WEBHOOK will drain next) and the
    age of the oldest one. Eligibility mirrors the proc's SEND predicate exactly
    (open, within the sender's severity-aware window — 7d for CRITICAL, 24h otherwise —
    family+company+severity match, not yet delivered to THIS route) so the panel and
    the drainer agree on 'what's pending'. A high OLDEST_MIN with the drain running is
    the starvation signal rec8 fixes."""
    return f"""
WITH ev AS (
    SELECT e.EVENT_ID, e.RAISED_AT, e.COMPANY, c.FAMILY,
           CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                           WHEN 'MEDIUM' THEN 2 ELSE 1 END AS SEV_RANK
    FROM {core_object("ALERT_EVENTS")} e
    JOIN {core_object("ALERT_CONFIG")} c ON c.RULE_ID = e.RULE_ID
    WHERE e.STATUS = 'OPEN'
      AND e.RAISED_AT >= {_SEND_ELIGIBLE_SINCE}
)
SELECT r.ROUTE_ID, r.INTEGRATION_NAME,
       COUNT(ev.EVENT_ID) AS BACKLOG,
       IFF(COUNT(ev.EVENT_ID) = 0, NULL,
           ROUND(DATEDIFF('second', MIN(ev.RAISED_AT), CURRENT_TIMESTAMP()) / 60.0, 1)) AS OLDEST_MIN
FROM {core_object("ALERT_ROUTES")} r
LEFT JOIN ev ON
        (r.FAMILY = 'ALL' OR ev.FAMILY = r.FAMILY)
    AND (COALESCE(r.COMPANY_FILTER, 'ALL') = 'ALL'
         OR ev.COMPANY = r.COMPANY_FILTER OR UPPER(ev.COMPANY) = 'ALL')
    AND ev.SEV_RANK >= CASE r.MIN_SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                                           WHEN 'MEDIUM' THEN 2 ELSE 1 END
    AND NOT EXISTS (SELECT 1 FROM {core_object("ALERT_DELIVERIES")} d
                    WHERE d.EVENT_ID = ev.EVENT_ID AND d.ROUTE_ID = r.ROUTE_ID)
WHERE r.ENABLED
GROUP BY r.ROUTE_ID, r.INTEGRATION_NAME
ORDER BY BACKLOG DESC, OLDEST_MIN DESC NULLS LAST
LIMIT 50
"""


def delivery_by_route(days: int = 30) -> str:
    days = bounded_days(days, 90)
    return f"""
SELECT ROUTE_ID, COUNT(*) AS SENDS, COUNT(DISTINCT EVENT_ID) AS EVENTS,
       MAX(SENT_AT) AS LAST_SENT
FROM {core_object("ALERT_DELIVERIES")}
WHERE SENT_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY ROUTE_ID
ORDER BY SENDS DESC
LIMIT 50
"""


def deliveries_for_event(event_id: str) -> str:
    """rec38: the per-route delivery rows for ONE event — 'did THIS page reach
    anyone, on which integration, when'. LEFT JOIN routes so an unmatched route
    id still shows; an EMPTY result means the event has not been delivered yet
    (which is normal for a severity/family no enabled route matches)."""
    return f"""
SELECT COALESCE(r.INTEGRATION_NAME, '(route ' || d.ROUTE_ID || ')') AS INTEGRATION_NAME,
       d.SENT_AT
FROM {core_object("ALERT_DELIVERIES")} d
LEFT JOIN {core_object("ALERT_ROUTES")} r ON r.ROUTE_ID = d.ROUTE_ID
WHERE d.EVENT_ID = {sql_literal(str(event_id or ''))}
ORDER BY d.SENT_AT
LIMIT 50
"""


def alert_fatigue(days: int = 30) -> str:
    """Per rule: volume, weekly rate, resolution-kind mix, untagged closes,
    and dedupe repeats — the attention-cost sheet (Codex r6 #10)."""
    days = bounded_days(days, 180)
    return f"""
SELECT RULE_ID,
       COUNT(*) AS EVENTS,
       ROUND(COUNT(*) / ({days} / 7.0), 1) AS PER_WEEK,
       COUNT_IF(UPPER(COALESCE(RESOLUTION_KIND, '')) = 'ACTIONED') AS ACTIONED,
       COUNT_IF(UPPER(COALESCE(RESOLUTION_KIND, '')) = 'NOISE') AS NOISE,
       COUNT_IF(UPPER(COALESCE(RESOLUTION_KIND, '')) = 'EXPECTED') AS EXPECTED,
       COUNT_IF(STATUS = 'RESOLVED' AND COALESCE(RESOLUTION_KIND, '') = '') AS UNTAGGED,
       COUNT(*) - COUNT(DISTINCT COALESCE(DEDUPE_KEY, EVENT_ID)) AS REPEAT_EVENTS,
       MAX(RAISED_AT) AS LAST_RAISED
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY RULE_ID
ORDER BY EVENTS DESC
LIMIT 100
"""


def acceptance_funnel(days: int = 90) -> str:
    """Generated -> executed -> verified, from audit rows (honest subset of
    Codex r5 #4 / r6 #12 — no impression tracking, Streamlit cannot measure
    'viewed' truthfully)."""
    days = bounded_days(days, 365)
    return f"""
SELECT
    (SELECT COUNT(*) FROM {core_object("REMEDIATION_LOG")}
      WHERE EXECUTED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATUS = 'EXECUTED') AS FIXES_EXECUTED,
    (SELECT COUNT(*) FROM {core_object("REMEDIATION_LOG")}
      WHERE EXECUTED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATUS = 'COPIED') AS FIXES_COPIED,
    (SELECT COUNT(*) FROM {core_object("REMEDIATION_LOG")}
      WHERE EXECUTED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATUS = 'FAILED') AS FIXES_FAILED,
    -- Funnel TOP = every item booked with an estimate in the window, regardless of its CURRENT state.
    -- STATE is a snapshot, so counting only STATE='ESTIMATED' excluded items that have since been
    -- verified/rejected and let the "N estimated -> M verified" funnel show verified > estimated. Every
    -- ledger item enters as an estimate, so all CREATED_AT-in-window rows are the true entry (ds-hunt).
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND ITEM_ID NOT IN (SELECT TWIN_ITEM_ID FROM ({_ledger_twin_select()}))) AS SAVINGS_ESTIMATED,
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'VERIFIED'
        AND ITEM_ID NOT IN (SELECT TWIN_ITEM_ID FROM ({_ledger_twin_select()}))) AS SAVINGS_VERIFIED,
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'REJECTED'
        AND ITEM_ID NOT IN (SELECT TWIN_ITEM_ID FROM ({_ledger_twin_select()}))) AS SAVINGS_REJECTED,
    (SELECT ROUND(COALESCE(SUM(VERIFIED_USD), 0), 2) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'VERIFIED'
        AND ITEM_ID NOT IN (SELECT TWIN_ITEM_ID FROM ({_ledger_twin_select()}))) AS VERIFIED_USD
"""


def action_acceptance(days: int = 90) -> str:
    """Action acceptance: of the recommendations the team DECIDED on in the window, how
    many were acted on (DONE) vs dismissed (DROPPED) — the honest 'does the team act on
    its advice' rate the acceptance_funnel couldn't give (no impression denominator, but
    a decided-then-DONE vs decided-then-DROPPED ratio is real). Terminal decisions are
    dated by COMPLETED_AT (set once on close by SP_ACTION_LIFECYCLE / SP_VERIFY_EXPERIMENT, kept
    through later comments), falling back to UPDATED_AT for rows closed before V074 or by a path that
    never stamps it — so a comment on an old closed item no longer re-enters the window (Next-Fifty
    #20). OPEN/IN_PROGRESS is the current still-undecided backlog. Account-wide (ACTION_QUEUE is the
    account queue)."""
    days = bounded_days(days, 365)
    since = f"DATEADD('day', -{days}, CURRENT_TIMESTAMP())"
    decided = "COALESCE(COMPLETED_AT, UPDATED_AT)"
    return f"""
SELECT
    COUNT_IF(UPPER(STATUS) = 'DONE'    AND {decided} >= {since}) AS DONE_N,
    COUNT_IF(UPPER(STATUS) = 'DROPPED' AND {decided} >= {since}) AS DROPPED_N,
    COUNT_IF(UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS'))           AS OPEN_N,
    ROUND(SUM(IFF(UPPER(STATUS) = 'DONE' AND {decided} >= {since},
                  COALESCE(ESTIMATED_USD, 0), 0)), 2)             AS DONE_USD
FROM {core_object("ACTION_QUEUE")}
"""


def telemetry_by_page(days: int = 7) -> str:
    """Per-page fetch health from the V027 telemetry rider. Only slow/failed +
    a ~2% healthy sample are persisted, so FETCHES is a PERSISTED count, not a
    census; EST_TRUE_FETCHES re-weights each row by 1/SAMPLE_PROB (rec18, V064) so
    the healthy baseline the sampler undercounts ~50x is restored.

    Sampling bias (Codex #44): the persisted set keeps EVERY slow/failed fetch but
    only ~2% of healthy ones, so any UNWEIGHTED statistic over that set is skewed to
    the tail. Only some columns are safe to read raw:
      - SLOW_2S / FAILED are honest COUNTS — every >=2s or failed row persists at
        prob 1.0, so the count is complete (not a rate over a biased denominator).
      - P95_S is a TAIL-SAMPLE percentile, NOT the fleet p95: the healthy body is
        2%-sampled while the whole tail is kept, so the persisted distribution sits
        far above the real one and this reads HIGH. Keep it only as a "how bad does
        it get" lens; do not present it as an unbiased fleet quantile. (Weighting a
        percentile needs per-row frequency expansion APPROX_PERCENTILE can't take,
        so it stays a tail lens rather than a lie dressed as a fleet number.)
      - CACHE_HIT_PCT is now WEIGHTED by 1/SAMPLE_PROB. Unweighted it collapsed
        toward 0: the must-persist stream is almost all cache MISSES (a hit is fast,
        so it rarely crosses the 2s persist bar), and those swamp the 2%-sampled
        healthy hits. Weighting each non-null row by 1/prob restores the true rate.
    Pre-V064 rows read SAMPLE_PROB NULL -> weight 1 (safe).

    D5: EST_WAIT_S is the RANKING column for tuning targets — the estimated total
    seconds the fleet actually spent waiting on this page, each row re-weighted by
    1/SAMPLE_PROB like EST_TRUE_FETCHES. p95 x slow-count could not see the pain
    that matters most in practice: 40,000 sub-2s fetches at 1.4s never enter
    SLOW_2S and barely move an exception-weighted p95, yet they are most of the
    wall-clock a viewer experiences. p95 stays as a column (it is the right lens
    for "how bad does this get"), just not as the rank."""
    days = bounded_days(days, 90)
    return f"""
SELECT PAGE,
       COUNT(*) AS FETCHES,
       ROUND(SUM(1.0 / COALESCE(SAMPLE_PROB, 1.0))) AS EST_TRUE_FETCHES,
       -- P3 note: 'batch_wall:%' rows are the whole batch's wall clock, a SUPERSET
       -- of the per-member 'batch:%' rows written beside them. Summing both would
       -- count one batch's seconds twice, so the wall rows are excluded here and
       -- kept only for the eyeball view of end-to-end batch cost.
       ROUND(SUM(IFF(STARTSWITH(QUERY_KEY, 'batch_wall:'), 0,
                     ELAPSED_MS / 1000.0 / COALESCE(SAMPLE_PROB, 1.0))), 1) AS EST_WAIT_S,
       -- #44: tail-sample percentile over the biased persisted set (reads HIGH) —
       -- kept as a severity lens only, NOT the fleet p95. See the docstring.
       ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.95) / 1000, 2) AS P95_S,
       -- #44: WEIGHTED cache-hit rate. Each non-null CACHE_HIT row counts 1/SAMPLE_PROB
       -- so the 2%-sampled healthy hits are not drowned by the must-persist (slow=miss)
       -- stream. NULL cache_hit rows (pre-rider) leave both sums, so the rate is over
       -- rows that actually carry the flag. NULLIF guards a page with no such rows.
       ROUND(
           SUM(IFF(CACHE_HIT IS NULL, 0, IFF(CACHE_HIT, 1, 0) / COALESCE(SAMPLE_PROB, 1.0)))
           / NULLIF(SUM(IFF(CACHE_HIT IS NULL, 0, 1.0 / COALESCE(SAMPLE_PROB, 1.0))), 0)
           * 100, 1) AS CACHE_HIT_PCT,
       COUNT_IF(NOT OK) AS FAILED,
       COUNT_IF(ELAPSED_MS >= 2000) AS SLOW_2S,
       ROUND(AVG(COALESCE(BATCH_SIZE, 1)), 1) AS AVG_BATCH,
       COUNT_IF(COALESCE(TRUNCATED, FALSE)) AS TRUNCATED_N
FROM {core_object("APP_QUERY_TELEMETRY")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY PAGE
ORDER BY EST_WAIT_S DESC NULLS LAST
LIMIT 50
"""


def usage_event_summary(days: int = 30) -> str:
    """What operators actually do, by EVENT_KIND (V027 rider) — curation
    calls follow this table, not opinions (Codex r6 #19)."""
    days = bounded_days(days, 365)
    return f"""
SELECT COALESCE(EVENT_KIND, 'page_visit') AS EVENT_KIND,
       COUNT(*) AS EVENTS,
       COUNT(DISTINCT USER_NAME) AS USERS,
       MAX(AT) AS LAST_SEEN
FROM {core_object("APP_USAGE")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY EVENTS DESC
LIMIT 40
"""


# ---------------------------------------------------------------------------
# V032 incident object — readers (tiny operator-curated tables, live tier).
# ---------------------------------------------------------------------------

def open_incidents(limit: int = 50, company: str = "ALL") -> str:
    """Company keeps that company's rows PLUS account-level (COMPANY='ALL')
    incidents — the open_alert_events convention (live round 8: the panel
    ignored the triage filter and showed both companies under ALFA)."""
    limit = max(1, min(int(limit or 50), 200))
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND (i.COMPANY = {sql_literal(company)} OR UPPER(i.COMPANY) = 'ALL')")
    return f"""
SELECT i.INCIDENT_ID, i.SEVERITY, i.STATUS, i.COMPANY, i.TITLE,
       i.DETECTED_AT, i.STARTED_AT, i.DECLARED_BY,
       (SELECT COUNT(*) FROM {core_object("INCIDENT_MEMBERS")} m
         WHERE m.INCIDENT_ID = i.INCIDENT_ID) AS MEMBERS
FROM {core_object("INCIDENTS")} i
WHERE i.STATUS IN ('OPEN', 'MITIGATED'){comp}
ORDER BY CASE UPPER(i.SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,
         i.DETECTED_AT DESC
LIMIT {limit}
"""


def incident_members_detail(incident_id: str) -> str:
    from app.core.sqlsafe import sql_literal
    lit = sql_literal(str(incident_id or "").strip())
    return f"""
SELECT m.MEMBER_KIND, m.REF_ID, m.EVIDENCE_TS, m.AUTO_LINKED, m.LINKED_BY, m.LINKED_AT,
       COALESCE(e.TITLE, '') AS ALERT_TITLE, COALESCE(e.STATUS, '') AS ALERT_STATUS
FROM {core_object("INCIDENT_MEMBERS")} m
LEFT JOIN {core_object("ALERT_EVENTS")} e
       ON m.MEMBER_KIND = 'ALERT' AND e.EVENT_ID = m.REF_ID
WHERE m.INCIDENT_ID = {lit}
ORDER BY m.EVIDENCE_TS
LIMIT 500
"""


def incident_proposals(limit: int = 20, company: str = "ALL") -> str:
    """Same company convention as open_incidents — proposals for the other
    company must not surface under an ALFA scope (live round 8)."""
    limit = max(1, min(int(limit or 20), 100))
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f"WHERE (COMPANY = {sql_literal(company)} OR UPPER(COMPANY) = 'ALL')\n")
    return f"""
SELECT *
FROM {core_object("INCIDENT_PROPOSALS")}
{comp}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,
         LAST_TS DESC
LIMIT {limit}
"""


def incident_gantt(days: int = 14, company: str = "ALL") -> str:
    """CR5: per-incident lifecycle spans for a Gantt view — DETECTED_AT to
    RESOLVED_AT (or to now for an open incident). Includes RESOLVED incidents so
    completed spans render, not just the open queue. ACK/MITIGATE timestamps are
    not consistently written, so the bar is the detected->resolved span.

    The SQL is intentionally 'now'-free — it uses CURRENT_TIMESTAMP() (a stable
    SQL token, not a baked datetime literal), so run()'s (sql,scope) memo is shared
    across renders. The old form interpolated the caller's minute-rounded account
    'now' as a literal, which still churned the cache key every minute (a fresh miss
    + INCIDENTS re-scan whenever a render crossed a minute boundary, and two viewers
    never shared the memo unless within the same minute).

    DETECTED_AT is written in account time while the SiS CURRENT_TIMESTAMP() is
    server/UTC (ALTER SESSION TIMEZONE is a no-op), so an OPEN incident's server-UTC
    end would overshoot account time by the offset (~5-6h). IS_OPEN (RESOLVED_AT IS
    NULL) is returned so the reader (charts.incident_gantt) re-anchors exactly those
    bars' end/duration to account time — precise, not inferred from the STATUS text.
    ENDED stays non-null (COALESCE to now) so open bars are never dropped by the
    reader's dropna."""
    days = bounded_days(days, 90)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND (COMPANY = {sql_literal(company)} OR UPPER(COMPANY) = 'ALL')")
    return f"""
SELECT
    INCIDENT_ID,
    LEFT(COALESCE(TITLE, 'incident ' || INCIDENT_ID), 60) AS TITLE,
    UPPER(COALESCE(SEVERITY, 'INFO')) AS SEVERITY,
    STATUS,
    (RESOLVED_AT IS NULL) AS IS_OPEN,
    DETECTED_AT::TIMESTAMP_NTZ AS STARTED,
    COALESCE(RESOLVED_AT, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ AS ENDED,
    DATEDIFF('minute', DETECTED_AT, COALESCE(RESOLVED_AT, CURRENT_TIMESTAMP())) AS DURATION_MIN
FROM {core_object("INCIDENTS")}
WHERE DETECTED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()){comp}
ORDER BY DETECTED_AT DESC
LIMIT 60
"""


def incident_metrics(days: int = 90, company: str = "ALL") -> str:
    """One row of lifecycle truth: TTD/MTTR medians and storm compression
    (alerts absorbed per incident).

    Two structurally-dead metrics removed (round-4 hunt), each following the
    change-correlated % precedent from v4.351 — all three counted a column no
    writer ever persists, so each was a permanent misleading value:
      * MTTA_MIN (ALC-2): DATEDIFF to INCIDENTS.ACK_AT, but ACK_AT is an
        ALERT_EVENTS lifecycle field — no incident writer ever sets it, so the
        median was always NULL. The real detected->ack median is alert-grain.
      * REOPEN_PCT (ALC-1): counted INCIDENTS.REOPENED_FROM parents, but no
        writer ever populates REOPENED_FROM (declare/autodeclare insert neither
        it nor a reopen child), so it was a permanent 0.0%.
      * Change-correlated % (v4.351): INCIDENT_MEMBERS kind WH_CHANGE/DEPLOY,
        never persisted (only 'ALERT'). The RCA auto-investigation
        (control_room) is the real change-correlation surface."""
    days = bounded_days(days, 365)
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f" AND (COMPANY = {sql_literal(company)} OR UPPER(COMPANY) = 'ALL')")
    # COMPRESSION previously nested (SELECT COUNT(*) FROM w) inside NULLIF inside a scalar
    # subquery — Snowflake 002031 "Unsupported subquery type cannot be evaluated" (owner
    # error log 2026-08-17). Hoist the incident count and the numerator into single-row
    # CTEs and CROSS JOIN, so the ratio is plain column arithmetic with no nested subquery.
    # The other single-scalar subqueries (OPEN_NOW, the MEDIAN aggregates over w)
    # are uncorrelated and fine.
    return f"""
WITH w AS (
    SELECT * FROM {core_object("INCIDENTS")}
    WHERE DETECTED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()){comp}
),
wn AS (SELECT COUNT(*) AS N FROM w),
compression AS (
    SELECT COUNT(*) AS ALERT_MEMBERS
    FROM {core_object("INCIDENT_MEMBERS")} m
    JOIN w ON w.INCIDENT_ID = m.INCIDENT_ID
    WHERE m.MEMBER_KIND = 'ALERT'
)
SELECT
    (SELECT COUNT(*) FROM {core_object("INCIDENTS")}
      WHERE STATUS IN ('OPEN', 'MITIGATED'){comp}) AS OPEN_NOW,
    wn.N AS DECLARED_N,
    (SELECT ROUND(MEDIAN(DATEDIFF('minute', STARTED_AT, DETECTED_AT)), 1) FROM w
      WHERE STARTED_AT IS NOT NULL AND STARTED_AT < DETECTED_AT) AS TTD_MIN,
    (SELECT ROUND(MEDIAN(DATEDIFF('minute', DETECTED_AT, RESOLVED_AT)), 1) FROM w
      WHERE RESOLVED_AT IS NOT NULL) AS MTTR_MIN,
    ROUND(compression.ALERT_MEMBERS / NULLIF(wn.N, 0), 1) AS COMPRESSION
FROM wn CROSS JOIN compression
"""


def flyway_history(limit: int = 50) -> str:
    """Flyway's own ledger, when present. Quoted lowercase — Flyway creates
    the table case-sensitive. Deliberately NOT canaried: the table is
    legitimately absent until Flyway is adopted, and an hourly canary
    failure would be pure error-log noise (the Admin panel degrades to a
    caption instead)."""
    limit = max(1, min(int(limit or 50), 200))
    return f"""
SELECT "installed_rank" AS INSTALLED_RANK, "version" AS VERSION,
       "description" AS DESCRIPTION, "installed_by" AS INSTALLED_BY,
       "installed_on" AS INSTALLED_ON, "execution_time" AS EXECUTION_MS,
       "success" AS SUCCESS
FROM {core_object('"flyway_schema_history"')}
ORDER BY "installed_rank" DESC
LIMIT {limit}
"""


def source_freshness_state() -> str:
    """Freshness as a lookup (V040): the 10-minute snapshot table, staleness
    computed from LAST_LOAD_TS at read — same contract as source_freshness()
    so every board renders unchanged whichever path serves."""
    return f"""
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT,
       DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0 AS HOURS_SINCE_LOAD
FROM {core_object("SOURCE_FRESHNESS_STATE")}
ORDER BY HOURS_SINCE_LOAD DESC
"""


def fact_contract_consumed(start_iso: str) -> str:
    """Contract-period billed credits from the daily fact (r13 #7) — the live
    METERING_DAILY_HISTORY rescan becomes the coverage-guarded fallback.

    FACT_FIRST_DAY is the fact's OWN earliest day, computed WITHOUT the
    contract filter (Codex r14 #8: MIN(DAY) inside WHERE DAY >= start made a
    quiet contract-start day read as "no coverage" forever). The caller
    trusts the sum only when FACT_FIRST_DAY <= contract start."""
    from datetime import date
    start = date.fromisoformat(str(start_iso)).isoformat()
    return f"""
SELECT SUM(IFF(DAY >= '{start}', CREDITS_BILLED, 0)) AS CREDITS_BILLED_TO_DATE,
       MIN(DAY) AS FACT_FIRST_DAY
FROM {mart_object("FACT_METERING_DAILY")}
"""


def fact_cortex_daily_spend(days: int, *, bounds: tuple | None = None) -> str:
    """AI/Cortex service credits by day from the daily fact (Codex r16 #7) —
    same SERVICE_TYPE predicate and billed basis as the live builder it
    replaces; the fact carries DAY, SERVICE_TYPE, and CREDITS_BILLED."""
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    scope = scope_window_where("DAY", days, bounds=bounds)
    return f"""
SELECT
    DAY,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
    SUM(CREDITS_BILLED) AS CREDITS_BILLED
FROM {mart_object("FACT_METERING_DAILY")}
WHERE {scope}
  AND {_AI_SERVICE_PRED}
GROUP BY 1, 2
ORDER BY DAY
"""

def unmapped_entities(days: int = 7) -> str:
    """V044 (#18): everything the loaders stamped UNKNOWN — the explicit-
    classification worklist behind honest chargeback. Mart-only (zero
    ACCOUNT_USAGE): rows appear as facts re-stamp (trailing 3d nightly,
    go-forward hourly). Fix = a COMPANY_SCOPE mapping row; the panel
    prints the exact INSERT."""
    days = bounded_days(days, 30)
    # r7 uncapped-aggregate: only the WAREHOUSE grain carries credits, and it sorts LAST
    # alphabetically (DATABASE < USER < WAREHOUSE), so >300 unmapped DB+USER rows would
    # evict every credit-bearing warehouse row past the LIMIT — the "billed blind" $ then
    # reads $0 in exactly the worst case. Wrap the union so the page can read the uncapped
    # blind-$ (SUM of credit VALUE) and entity count from a pre-LIMIT window total.
    return f"""
WITH unm AS (
    SELECT 'WAREHOUSE' AS GRAIN, WAREHOUSE_NAME AS ENTITY,
           'credits' AS MEASURE, ROUND(SUM(CREDITS_TOTAL), 2) AS VALUE,
           MAX(DAY) AS LAST_SEEN
    FROM {core_object("FACT_WAREHOUSE_DAILY")}
    WHERE COMPANY = 'UNKNOWN' AND DAY >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY 2
    UNION ALL
    SELECT 'DATABASE', DATABASE_NAME, 'queries', SUM(QUERIES), MAX(DATE(HOUR_TS))
    FROM {core_object("FACT_QUERY_SCHEMA_HOURLY")}
    WHERE COMPANY = 'UNKNOWN' AND HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY 2
    UNION ALL
    SELECT 'USER', USER_NAME, 'logins', SUM(LOGINS), MAX(DAY)
    FROM {core_object("FACT_LOGIN_DAILY")}
    WHERE COMPANY = 'UNKNOWN' AND DAY >= DATEADD('day', -{days}, CURRENT_DATE())
    GROUP BY 2
)
SELECT GRAIN, ENTITY, MEASURE, VALUE, LAST_SEEN,
       COUNT(*) OVER () AS TOTAL_ENTITIES_WIN,
       SUM(CASE WHEN MEASURE = 'credits' THEN VALUE ELSE 0 END) OVER () AS TOTAL_CREDITS_WIN
FROM unm
ORDER BY GRAIN, VALUE DESC
LIMIT 300
"""


# ---------------------------------------------------------------------------
# Per-table storage mart readers (V124). Serve insights_sql.storage_waste /
# table_storage_breakdown from MART_TABLE_STORAGE_DAILY's latest snapshot so the
# Spend + Optimize storage panels stop re-scanning TABLE_STORAGE_METRICS /
# TABLE_DML_HISTORY live. Output columns/filters/order match the live builders
# BYTE-for-byte so run_mart_first can swap them in; COMPANY is the pre-stamped
# COMPANY_FOR_DATABASE column (the same axis the live scope filters by), so ALL =
# no filter (the mart holds real per-table company stamps, not an 'ALL' rollup).
# ---------------------------------------------------------------------------

def _storage_mart_company(company: str) -> str:
    return "" if str(company or "ALL").upper() == "ALL" else f"COMPANY = {sql_literal(company)}"


def table_storage_waste_mart(company: str = "ALL", min_gb: float = 1.0) -> str:
    """Mart twin of insights_sql.storage_waste (heavy time-travel/fail-safe, STALE = no
    DML in 90d) — reads the latest MART_TABLE_STORAGE_DAILY snapshot."""
    _tbl = mart_object("MART_TABLE_STORAGE_DAILY")
    min_bytes = int(max(0.1, float(min_gb)) * 1024 ** 3)
    where = and_where(
        f"DAY = (SELECT MAX(DAY) FROM {_tbl})",
        f"ACTIVE_BYTES + TIME_TRAVEL_BYTES + FAILSAFE_BYTES >= {min_bytes}",
        _storage_mart_company(company),
    )
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    TABLE_NAME,
    ROUND(ACTIVE_BYTES / POWER(1024, 3), 2) AS ACTIVE_GB,
    ROUND(TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(FAILSAFE_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
    RETENTION_DAYS,
    IFF(RETENTION_DAYS IS NULL, FALSE, TRUE) AS RETENTION_KNOWN,
    LAST_DML,
    IFF(LAST_DML IS NULL, 'STALE', 'ACTIVE') AS STATUS,
    -- r36 follow-up: expose the snapshot day (latest loaded MART_TABLE_STORAGE_DAILY DAY) so the
    -- caller's mart_accept can reject a multi-day-stale snapshot (a stalled daily loader) and fall to
    -- the live TABLE_STORAGE_METRICS scan instead of serving out-of-date bytes as current.
    (SELECT MAX(DAY) FROM {_tbl}) AS SNAPSHOT_DAY
FROM {_tbl}
WHERE {where}
ORDER BY TIME_TRAVEL_BYTES + FAILSAFE_BYTES DESC
LIMIT 50
"""


def table_storage_breakdown_mart(company: str = "ALL", database: str = "", limit: int = 50) -> str:
    """Mart twin of insights_sql.table_storage_breakdown (per-table active/time-travel/
    fail-safe/clone split, ordered by total on-disk bytes) — latest snapshot."""
    from app import companies
    _tbl = mart_object("MART_TABLE_STORAGE_DAILY")
    limit = max(5, min(int(limit or 50), 500))
    where = and_where(
        f"DAY = (SELECT MAX(DAY) FROM {_tbl})",
        "ACTIVE_BYTES + TIME_TRAVEL_BYTES + FAILSAFE_BYTES > 0",
        _storage_mart_company(company),
        companies.database_equals_clause(database, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    TABLE_NAME,
    ROUND(ACTIVE_BYTES / POWER(1024, 3), 2) AS ACTIVE_GB,
    ROUND(TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(FAILSAFE_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
    ROUND(RETAINED_FOR_CLONE_BYTES / POWER(1024, 3), 2) AS CLONE_GB,
    ROUND((ACTIVE_BYTES + TIME_TRAVEL_BYTES + FAILSAFE_BYTES + RETAINED_FOR_CLONE_BYTES)
          / POWER(1024, 3), 2) AS TOTAL_GB,
    RETENTION_DAYS,
    LAST_DML,
    IFF(LAST_DML IS NULL, 'STALE', 'ACTIVE') AS STATUS,
    IFF(RETENTION_DAYS IS NULL, TRUE, FALSE) AS DROPPED,
    -- r36 follow-up: snapshot-day scalar for the caller's freshness mart_accept (see waste_mart).
    (SELECT MAX(DAY) FROM {_tbl}) AS SNAPSHOT_DAY
FROM {_tbl}
WHERE {where}
ORDER BY ACTIVE_BYTES + TIME_TRAVEL_BYTES + FAILSAFE_BYTES + RETAINED_FOR_CLONE_BYTES DESC
LIMIT {limit}
"""
