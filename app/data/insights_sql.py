"""Insight builders ported from the original OVERWATCH (features 1-7).

Idle warehouse analysis, repeat-query fingerprints, storage growth, release
window compares, pipeline SLA readers, dormant users.
Pure SQL strings: bounded windows, company scoping, no dollar rates.
"""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.data.common import and_where, bounded_days


def _iso_date(value: str, name: str) -> str:
    text = str(value or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-" or not text.replace("-", "").isdigit():
        raise ValueError(f"{name} must be YYYY-MM-DD, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# 1. Idle warehouse analysis: metered warehouse-hours with zero query activity
# ---------------------------------------------------------------------------

# D2 (audit 2026-07-31): the ONE definition of "a warehouse-hour in which a
# query was actually running", shared by the idle advisor and the sizing
# profile so the two can never disagree about the same warehouse's idle share.
#
# The old form was `SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour',
# START_TIME)` — the query's START hour only. A query that begins at 10:59 and
# runs three hours marked hour 10 active and left hours 11 and 12 looking IDLE,
# so long-running warehouses' idle credits (and IDLE_PCT, and every dollar
# derived from them) were systematically overstated. Expanding each query
# across the hours it SPANS is the fix. The 25-row generator bounds the
# expansion; a query running past 24h keeps only its first 25 hours, which can
# only under-mark activity (the conservative direction for an idle claim).
_ACTIVE_HOUR_SPAN = 25


def _active_hours_cte(days: int, company: str) -> str:
    """CTE pair (`q_spans`, `query_hours`) yielding one row per active
    warehouse-hour. Callers LEFT JOIN metering to `query_hours`."""
    return f"""q_spans AS (
    SELECT WAREHOUSE_NAME,
           DATE_TRUNC('hour', START_TIME) AS H0,
           DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
      AND {companies.warehouse_clause(company) or "1 = 1"}
),
query_hours AS (
    SELECT DISTINCT s.WAREHOUSE_NAME, DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS
    FROM q_spans s
    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => {_ACTIVE_HOUR_SPAN}))) g
      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1
)"""


def idle_warehouse_analysis(days: int, company: str = "ALL") -> str:
    """Per warehouse: total vs idle credits (hour slices with no queries).

    WAREHOUSE_METERING_HISTORY bills by hour slice; joining each slice to
    query activity in the same warehouse-hour isolates credits burned while
    nothing ran — the auto-suspend opportunity. "Active" is span-based (see
    _active_hours_cte), not start-hour-based.
    """
    days = bounded_days(days)
    where = and_where(
        f"M.START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
    )
    return f"""
WITH {_active_hours_cte(days, company)}
SELECT
    M.WAREHOUSE_NAME,
    {companies.company_case_sql("M.WAREHOUSE_NAME")} AS COMPANY,
    COUNT(*) AS METERED_HOURS,
    SUM(IFF(Q.HOUR_TS IS NULL, 1, 0)) AS IDLE_HOURS,
    SUM(COALESCE(M.CREDITS_USED, 0)) AS TOTAL_CREDITS,
    SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
LEFT JOIN query_hours Q
       ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
      AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
WHERE {where}
  AND M.WAREHOUSE_ID > 0
GROUP BY 1, 2
HAVING SUM(COALESCE(M.CREDITS_USED, 0)) > 0
ORDER BY IDLE_CREDITS DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 2. Repeat-query fingerprints (cache/materialization candidates)
# ---------------------------------------------------------------------------

# D3 (audit 2026-07-31): credit weight per warehouse-hour by size. Snowflake's
# ladder doubles the credit rate per step (X-Small = 1 credit/hour), so
# EXECUTION_TIME x this factor is a size-aware credit ESTIMATE — enough to rank
# fingerprints by money instead of by wall-clock hours, without paying for the
# hour-share allocation join. Rows with no warehouse (result-cache/metadata
# answers) carry no compute credits, hence ELSE 0.
_SIZE_CREDIT_FACTOR_SQL = """CASE UPPER(COALESCE(WAREHOUSE_SIZE, ''))
             WHEN 'X-SMALL'  THEN 1   WHEN 'SMALL'    THEN 2
             WHEN 'MEDIUM'   THEN 4   WHEN 'LARGE'    THEN 8
             WHEN 'X-LARGE'  THEN 16  WHEN '2X-LARGE' THEN 32
             WHEN '3X-LARGE' THEN 64  WHEN '4X-LARGE' THEN 128
             WHEN '5X-LARGE' THEN 256 WHEN '6X-LARGE' THEN 512
             ELSE 0 END"""


def repeat_query_fingerprints(days: int, company: str = "ALL", min_runs: int = 10,
                              database: str = "", schema_contains: str = "") -> str:
    """Repeated query shapes ranked by ESTIMATED credits x cache-miss.

    D3 (audit 2026-07-31): the old ORDER BY was TOTAL_ELAPSED_HOURS — wall
    clock, which counts queue and compile time and treats an X-Small hour as
    equal to a 4X-Large hour (128x the money). Ranking now uses EST_CREDITS
    (execution time x the size factor) weighted by the cache MISS share, so the
    top of the list is where caching/materialization actually pays. Cache % is
    BYTES_SCANNED-weighted and ignores zero-scan runs (a run answered from the
    result cache scans nothing — averaging its 0% "local cache" reading in made
    well-cached families look cache-poor); a family whose runs ALL scan zero
    bytes is fully cached already, hence the COALESCE to 100.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    min_runs = max(2, min(int(min_runs), 1000))
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        "QUERY_TYPE = 'SELECT'",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
        # C10 doctrine (ops_sql._query_scope): scope QUERY_HISTORY by WAREHOUSE
        # company only — NOT warehouse AND user. The old intersection dropped
        # cross-company activity (a Trexis/UNKNOWN principal on an ALFA warehouse
        # vanished from BOTH scoped views, appearing only under ALL), so the scoped
        # repeat-query list under-reported. Matches the V082 mart twin.
        companies.warehouse_clause(company),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH AS FINGERPRINT,
    COUNT(*) AS RUNS,
    COUNT(DISTINCT USER_NAME) AS USERS,
    COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 3600000.0 AS TOTAL_ELAPSED_HOURS,
    AVG(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS AVG_ELAPSED_SEC,
    SUM(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 4) AS TOTAL_TB_SCANNED,
    COALESCE(
        SUM(IFF(COALESCE(BYTES_SCANNED, 0) > 0,
                COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0) * BYTES_SCANNED, 0))
        / NULLIF(SUM(IFF(COALESCE(BYTES_SCANNED, 0) > 0, BYTES_SCANNED, 0)), 0) * 100,
        100) AS AVG_CACHE_PCT,
    SUM(COALESCE(EXECUTION_TIME, 0) / 3600000.0 * {_SIZE_CREDIT_FACTOR_SQL}) AS EST_CREDITS,
    ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS QUERY_PREVIEW,
    MAX(START_TIME) AS LAST_RUN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY QUERY_PARAMETERIZED_HASH
HAVING COUNT(*) >= {min_runs}
ORDER BY EST_CREDITS * (1 - AVG_CACHE_PCT / 100) DESC, TOTAL_ELAPSED_HOURS DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 3. Storage growth movers
# ---------------------------------------------------------------------------

def storage_growth_by_database(days: int, company: str = "ALL") -> str:
    """Per-database storage endpoints PLUS a least-squares slope.

    D4 (audit 2026-07-31): (LAST - FIRST) / SPAN is an endpoint diff — one
    reload, clone, or purge on either endpoint day becomes the whole trend and
    the x30 projection multiplies it. REGR_SLOPE over every observed day is the
    resistant answer; DAYS_OBSERVED lets the caller mark a short/sparse series
    LOW CONFIDENCE instead of projecting it as fact. Endpoints stay in the
    output because the current/first sizes are what a reader recognises.
    """
    days = bounded_days(days)
    where = and_where(
        f"USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company),
    )
    return f"""
WITH daily AS (
    SELECT
        DATABASE_NAME,
        USAGE_DATE,
        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE {where}
    GROUP BY 1, 2
)
SELECT
    DATABASE_NAME,
    {companies.database_case_sql("DATABASE_NAME")} AS COMPANY,
    MIN(USAGE_DATE) AS FIRST_DAY,
    MAX(USAGE_DATE) AS LAST_DAY,
    MIN_BY(DB_BYTES, USAGE_DATE) AS FIRST_BYTES,
    MAX_BY(DB_BYTES, USAGE_DATE) AS LAST_BYTES,
    MAX_BY(FAILSAFE_BYTES, USAGE_DATE) AS FAILSAFE_BYTES,
    DATEDIFF('day', MIN(USAGE_DATE), MAX(USAGE_DATE)) AS SPAN_DAYS,
    COUNT(*) AS DAYS_OBSERVED,
    REGR_SLOPE(DB_BYTES, DATEDIFF('day', DATE '1970-01-01', USAGE_DATE)) AS SLOPE_BYTES_PER_DAY
FROM daily
GROUP BY 1, 2
HAVING MAX_BY(DB_BYTES, USAGE_DATE) > 0 OR MIN_BY(DB_BYTES, USAGE_DATE) > 0
ORDER BY (MAX_BY(DB_BYTES, USAGE_DATE) - MIN_BY(DB_BYTES, USAGE_DATE)) DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# 4. Release window compare (before vs after a deploy date)
# ---------------------------------------------------------------------------

def release_query_compare(release_date: str, window_days: int, company: str = "ALL") -> str:
    """Overall query health in the N days before vs after a release date."""
    release = _iso_date(release_date, "release_date")
    window = max(1, min(int(window_days), 14))
    where = and_where(
        f"START_TIME >= DATEADD('day', -{window}, DATE '{release}')",
        f"START_TIME < DATEADD('day', {window}, DATE '{release}')",
        # C10 doctrine: QUERY_HISTORY scopes by WAREHOUSE company only, never
        # warehouse AND user — the intersection dropped cross-company rows from both
        # scoped views (see repeat_query_fingerprints / ops_sql._query_scope).
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    IFF(START_TIME < DATE '{release}', 'BEFORE', 'AFTER') AS PERIOD,
    COUNT(*) AS QUERY_COUNT,
    SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
    -- PER-QUERY, not window totals: both scale with query VOLUME, and a deploy day
    -- routinely changes traffic, so raw totals flagged a pure volume swing as a release
    -- regression. Dividing by QUERY_COUNT makes the equal-width before/after windows
    -- comparable on per-query health (bug-hunt 2026-08-30). FAIL_PCT/p95 already are.
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0
        / NULLIF(COUNT(*), 0) AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3)
        / NULLIF(COUNT(*), 0) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
"""



def release_task_compare(release_date: str, window_days: int, company: str = "ALL") -> str:
    """Per-task runs/failures/runtime before vs after a release date."""
    release = _iso_date(release_date, "release_date")
    window = max(1, min(int(window_days), 14))
    where = and_where(
        f"QUERY_START_TIME >= DATEADD('day', -{window}, DATE '{release}')",
        f"QUERY_START_TIME < DATEADD('day', {window}, DATE '{release}')",
        companies.database_clause(company, "DATABASE_NAME"),
    )
    return f"""
WITH runs AS (
    SELECT
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME,
        QUERY_START_TIME,
        COMPLETED_TIME,
        STATE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
    -- Collapse auto-retry attempts (they share one SCHEDULED_TIME) to the terminal
    -- attempt so before/after counts reflect scheduled RUNS, not attempts — otherwise a
    -- retried task inflates RUNS/FAILED and a healthy deploy looks like it broke tasks.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME AS TASK_NAME,
    IFF(QUERY_START_TIME < DATE '{release}', 'BEFORE', 'AFTER') AS PERIOD,
    COUNT(*) AS RUNS,
    SUM(IFF(STATE = 'FAILED', 1, 0)) AS FAILED,
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)) AS AVG_SEC
FROM runs
GROUP BY 1, 2, 3, 4
ORDER BY DATABASE_NAME, SCHEMA_NAME, TASK_NAME, PERIOD
LIMIT 1000
"""


# O16: a release date is guesswork unless the owner remembers the deploy. A
# release shows up in ACCOUNT_USAGE as a burst of schema-change DDL, so counting
# that DDL per day surfaces the likely deploy days for the compare picker.
_RELEASE_DDL_PREDICATE = (
    "(QUERY_TYPE ILIKE 'CREATE%' OR QUERY_TYPE ILIKE 'ALTER%' "
    "OR QUERY_TYPE ILIKE 'DROP%')"
)
# CREATE_TABLE_AS_SELECT is ETL data-loading and ALTER_SESSION is per-connection
# setup (SET QUERY_TAG/timezone) — both are high-volume non-deploys that would
# swamp the deploy signal, so they are excluded from the count.
_RELEASE_DDL_NOISE = "QUERY_TYPE NOT IN ('CREATE_TABLE_AS_SELECT', 'ALTER_SESSION')"


def detect_release_days(days: int, company: str = "ALL") -> str:
    """Candidate deploy days for the Release compare auto-detect (O16).

    Counts successful schema-change DDL (CREATE/ALTER/DROP of tables, views,
    procs, tasks, ...) per day; the biggest days are the likely deploys.
    Excludes CTAS / session ops (ETL & connection noise, not deploys) and
    OVERWATCH's own maintenance DDL (its QUERY_TAG), and scopes by the touched
    DATABASE_NAME. A heuristic — evidence to pre-fill the picker, never a
    definitive release log.
    """
    days = bounded_days(days, 180)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        _RELEASE_DDL_PREDICATE,
        _RELEASE_DDL_NOISE,
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
        companies.database_clause(company, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATE_TRUNC('day', START_TIME)::DATE AS DAY,
    COUNT(*) AS DDL_COUNT,
    COUNT(DISTINCT USER_NAME) AS ACTORS,
    MODE(USER_NAME) AS TOP_ACTOR,
    COUNT(DISTINCT DATABASE_NAME) AS DATABASES
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY DAY DESC
LIMIT 200
"""


# ---------------------------------------------------------------------------
# 5. Task failure detail (root-cause timeline)
# ---------------------------------------------------------------------------

def task_failure_details(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    from app.core.sqlsafe import contains_filter

    # Cap 30 (was 14) so the incident RCA auto-investigation feed, which asks for the incident's
    # onset-covering window (up to 30 days), is not silently clamped shorter than the sibling RCA
    # feeds (change/grant/warehouse registries, spend anomaly) and cannot omit an onset-time task
    # failure from the ranked cause (incident-hunt 2026-08-30). SCHEDULED_TIME pruning keeps the scan
    # bounded; the Operations 7d caller is unaffected.
    days = bounded_days(days, maximum=30)
    # r23 #3: TASK_HISTORY prunes on SCHEDULED_TIME (the V031 builders bound
    # BOTH columns for exactly this reason); without it the RCA read scanned
    # the whole view — 33s on the fleet board. +1 day covers runs scheduled
    # before the window that started inside it.
    # STATE = 'FAILED' moves to the OUTER select AFTER the retry dedup below, so a scheduled run whose
    # FIRST attempt failed but a later auto-retry SUCCEEDED is not counted as a failure.
    where = and_where(
        f"SCHEDULED_TIME >= DATEADD('day', -{days + 1}, CURRENT_DATE())",
        f"QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH terminal AS (
    SELECT
        DATABASE_NAME, SCHEMA_NAME, NAME, ROOT_TASK_ID, GRAPH_RUN_GROUP_ID,
        QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
    -- Retry dedup: keep only the TERMINAL attempt of each scheduled run (matches task_runs,
    -- task_recent_states, release_task_compare). Without it a TASK_AUTO_RETRY_ATTEMPTS task that
    -- flapped on attempt 1 then succeeded surfaces its FAILED attempt as a standalone root-cause,
    -- inflating the Failures KPI and paging on-call for a run whose final state SUCCEEDED (ops-hunt).
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME AS TASK_NAME,
    ROOT_TASK_ID,
    GRAPH_RUN_GROUP_ID,
    QUERY_START_TIME,
    DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SEC,
    COALESCE(ERROR_CODE::VARCHAR, '') AS ERROR_CODE,
    LEFT(COALESCE(ERROR_MESSAGE, ''), 300) AS ERROR_MESSAGE
FROM terminal
WHERE STATE = 'FAILED'
ORDER BY QUERY_START_TIME DESC
LIMIT 500
"""


# ---------------------------------------------------------------------------
# Warehouse sizing profile (credits + load stats + idle share per warehouse)
# ---------------------------------------------------------------------------

def warehouse_sizing_profile(days: int, company: str = "ALL") -> str:
    days = bounded_days(days)
    where_m = and_where(
        f"M.START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "M.WAREHOUSE_ID > 0",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
    )
    return f"""
WITH query_stats AS (
    SELECT
        WAREHOUSE_NAME,
        COUNT(*) AS QUERY_COUNT,
        COUNT(DISTINCT DATE(START_TIME)) AS ACTIVE_QUERY_DAYS,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000.0 AS QUEUED_SEC,
        SUM(COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_PROVISIONING_SEC,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
      AND {companies.warehouse_clause(company) or "1 = 1"}
    GROUP BY WAREHOUSE_NAME
),
{_active_hours_cte(days, company)}
SELECT
    M.WAREHOUSE_NAME,
    {companies.company_case_sql("M.WAREHOUSE_NAME")} AS COMPANY,
    SUM(COALESCE(M.CREDITS_USED, 0)) AS CREDITS_TOTAL,
    ROUND(SUM(IFF(H.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0))
          / NULLIF(SUM(COALESCE(M.CREDITS_USED, 0)), 0) * 100, 1) AS IDLE_PCT,
    COALESCE(MAX(Q.QUERY_COUNT), 0) AS QUERY_COUNT,
    COALESCE(MAX(Q.ACTIVE_QUERY_DAYS), 0) AS ACTIVE_QUERY_DAYS,
    COALESCE(MAX(Q.P95_ELAPSED_SEC), 0) AS P95_ELAPSED_SEC,
    COALESCE(MAX(Q.QUEUED_SEC), 0) AS QUEUED_SEC,
    COALESCE(MAX(Q.QUEUED_PROVISIONING_SEC), 0) AS QUEUED_PROVISIONING_SEC,
    COALESCE(MAX(Q.SPILL_REMOTE_GB), 0) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
LEFT JOIN query_hours H
       ON H.WAREHOUSE_NAME = M.WAREHOUSE_NAME
      AND H.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
LEFT JOIN query_stats Q ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
WHERE {where_m}
GROUP BY 1, 2
HAVING SUM(COALESCE(M.CREDITS_USED, 0)) > 0
ORDER BY CREDITS_TOTAL DESC
LIMIT 100
"""


# ---------------------------------------------------------------------------
# Query drill-through
# ---------------------------------------------------------------------------

_QUERY_ID_RE_HINT = "Snowflake query IDs are UUID-like hex strings"


def query_detail(query_id: str, start_hint: str = "") -> str:
    """Full detail for one query; the ID is validated before embedding.

    r22 #17: ``start_hint`` (an ISO date from the clicked row's START_TIME)
    bounds the scan to a +/-1-day window instead of the full 365-day
    retention. The manually-pasted-ID path passes no hint and keeps the
    broad scan — that reach is its purpose."""
    import re as _re

    qid = str(query_id or "").strip()
    if not _re.match(r"^[0-9a-fA-F-]{16,64}$", qid):
        raise ValueError(f"Invalid query id ({_QUERY_ID_RE_HINT}): {qid!r}")
    from app.core.sqlsafe import sql_literal as _lit

    window = ""
    if str(start_hint or "").strip():
        from datetime import date as _date
        anchor = _date.fromisoformat(str(start_hint).strip()[:10]).isoformat()
        window = (f"\n  AND START_TIME >= DATEADD('day', -1, '{anchor}'::DATE)"
                  f"\n  AND START_TIME < DATEADD('day', 2, '{anchor}'::DATE)")

    return f"""
SELECT
    QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE,
    DATABASE_NAME, SCHEMA_NAME, QUERY_TYPE, EXECUTION_STATUS,
    ERROR_CODE, ERROR_MESSAGE,
    START_TIME, END_TIME,
    TOTAL_ELAPSED_TIME / 1000.0 AS ELAPSED_SEC,
    COMPILATION_TIME / 1000.0 AS COMPILE_SEC,
    EXECUTION_TIME / 1000.0 AS EXECUTION_SEC,
    (COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    BYTES_SCANNED / POWER(1024, 3) AS GB_SCANNED,
    PERCENTAGE_SCANNED_FROM_CACHE * 100 AS CACHE_PCT,   -- R3-2: 0-1 fraction -> percent at read
    COALESCE(BYTES_SPILLED_TO_LOCAL_STORAGE, 0) / POWER(1024, 3) AS LOCAL_SPILL_GB,
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3) AS REMOTE_SPILL_GB,
    ROWS_PRODUCED,
    PARTITIONS_SCANNED, PARTITIONS_TOTAL,
    QUERY_TEXT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_ID = {_lit(qid)}{window}
LIMIT 1
"""


# ---------------------------------------------------------------------------
# 6. Pipeline SLA (config in V006; status computed against table freshness)
# ---------------------------------------------------------------------------

def pipeline_sla_status() -> str:
    return f"""
SELECT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, OWNER, MAX_AGE_HOURS,
       LAST_ALTERED, HOURS_SINCE, SLA_MET
FROM {core_object("PIPELINE_SLA_STATUS")}
ORDER BY SLA_MET, HOURS_SINCE DESC
"""


def pipeline_sla_config() -> str:
    return f"""
SELECT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, MAX_AGE_HOURS, OWNER, ENABLED, UPDATED_AT
FROM {core_object("PIPELINE_SLA_CONFIG")}
ORDER BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME
"""


def pipeline_sla_forecast(days: int = 14) -> str:
    """Registered freshness SLAs + each table's refresh cadence (O10 predictor).

    pipeline_sla_status is purely reactive — SLA_MET is a right-now verdict. This
    joins every registered table to its typical refresh cadence: the median gap
    (minutes) between successive DML events over the window, from
    TABLE_DML_HISTORY. Paired with the runway to the deadline (MAX_AGE_HOURS -
    HOURS_SINCE), the pure scorer (`logic.insights.pipeline_sla_forecast`) can
    then forecast the tables trending toward a miss — 'overdue vs its own
    cadence' or 'deadline within one refresh cycle' — instead of only the ones
    that already missed. Tables never observed refreshing in the window carry a
    NULL cadence and fall back to a runway-proximity check downstream.
    """
    days = max(3, min(int(days or 14), 90))
    return f"""
WITH intervals AS (
    SELECT UPPER(DATABASE_NAME) AS DB, UPPER(SCHEMA_NAME) AS SCH,
           UPPER(TABLE_NAME) AS TBL,
           DATEDIFF('minute',
                    LAG(START_TIME) OVER (
                        PARTITION BY UPPER(DATABASE_NAME), UPPER(SCHEMA_NAME),
                                     UPPER(TABLE_NAME)
                        ORDER BY START_TIME),
                    START_TIME) AS GAP_MIN
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
), cadence AS (
    SELECT DB, SCH, TBL,
           MEDIAN(GAP_MIN) AS MEDIAN_GAP_MIN,
           COUNT(*) AS REFRESHES
    FROM intervals
    WHERE GAP_MIN IS NOT NULL AND GAP_MIN > 0
    GROUP BY DB, SCH, TBL
)
SELECT s.DATABASE_NAME, s.SCHEMA_NAME, s.TABLE_NAME, s.OWNER,
       s.MAX_AGE_HOURS, s.HOURS_SINCE, s.SLA_MET,
       c.MEDIAN_GAP_MIN, c.REFRESHES,
       ROUND(s.MAX_AGE_HOURS - s.HOURS_SINCE, 2) AS RUNWAY_HOURS
FROM {core_object("PIPELINE_SLA_STATUS")} s
LEFT JOIN cadence c
  ON c.DB = UPPER(s.DATABASE_NAME) AND c.SCH = UPPER(s.SCHEMA_NAME)
 AND c.TBL = UPPER(s.TABLE_NAME)
ORDER BY s.SLA_MET, RUNWAY_HOURS
"""


# ---------------------------------------------------------------------------
# 7. Dormant users & grant review
# ---------------------------------------------------------------------------

def dormant_users(dormant_days: int = 90, company: str = "ALL") -> str:
    dormant_days = max(30, min(int(dormant_days), 365))
    where = and_where(
        "U.DELETED_ON IS NULL",
        "COALESCE(U.DISABLED, FALSE) = FALSE",
        f"(U.LAST_SUCCESS_LOGIN IS NULL OR U.LAST_SUCCESS_LOGIN < DATEADD('day', -{dormant_days}, CURRENT_TIMESTAMP()))",
        f"U.CREATED_ON < DATEADD('day', -{dormant_days}, CURRENT_TIMESTAMP())",
        companies.user_clause(company, "U.NAME"),
    )
    return f"""
WITH role_counts AS (
    SELECT GRANTEE_NAME, COUNT(*) AS ROLE_COUNT,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
    GROUP BY GRANTEE_NAME
)
SELECT
    U.NAME AS USER_NAME,
    U.EMAIL,
    U.CREATED_ON,
    U.LAST_SUCCESS_LOGIN,
    COALESCE(DATEDIFF('day', U.LAST_SUCCESS_LOGIN, CURRENT_TIMESTAMP()), 9999) AS DAYS_DORMANT,
    COALESCE(R.ROLE_COUNT, 0) AS ROLE_COUNT,
    LEFT(COALESCE(R.ROLES, ''), 300) AS ROLES
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
LEFT JOIN role_counts R ON R.GRANTEE_NAME = U.NAME
WHERE {where}
ORDER BY DAYS_DORMANT DESC, ROLE_COUNT DESC
LIMIT 300
"""


# B4 (audit 2026-07-31): the retention-fix estimator needs the table's CURRENT
# DATA_RETENTION_TIME_IN_DAYS to answer "how much of the time-travel bytes does
# cutting retention to N actually free" — without it the page priced the whole
# time-travel + failsafe pile as recoverable. ACCOUNT_USAGE.TABLES carries it
# keyed by the same TABLE_ID the DML join already uses.
_RETENTION_JOIN_SQL = """LEFT JOIN (
    SELECT TABLE_ID, MAX(RETENTION_TIME) AS RETENTION_DAYS
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
    WHERE DELETED IS NULL
    GROUP BY 1
) rt ON rt.TABLE_ID = m.ID"""


def storage_waste(company: str = "ALL", min_gb: float = 1.0) -> str:
    """Tables carrying heavy Time-Travel/failsafe bytes, flagged STALE when
    no DML touched them in 90 days — retention money for nothing."""
    min_bytes = int(max(0.1, float(min_gb)) * 1024 ** 3)
    where = and_where(
        "m.DELETED = FALSE",
        f"m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES >= {min_bytes}",
        companies.database_clause(company, "m.TABLE_CATALOG"),
    )
    return f"""
SELECT
    m.TABLE_CATALOG AS DATABASE_NAME,
    m.TABLE_SCHEMA AS SCHEMA_NAME,
    m.TABLE_NAME,
    ROUND(m.ACTIVE_BYTES / POWER(1024, 3), 2) AS ACTIVE_GB,
    ROUND(m.TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(m.FAILSAFE_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
    rt.RETENTION_DAYS,
    IFF(rt.RETENTION_DAYS IS NULL, FALSE, TRUE) AS RETENTION_KNOWN,
    d.LAST_DML,
    IFF(d.LAST_DML IS NULL, 'STALE', 'ACTIVE') AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
LEFT JOIN (
    SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1
) d ON d.TABLE_ID = m.ID
{_RETENTION_JOIN_SQL}
WHERE {where}
ORDER BY m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES DESC
LIMIT 50
"""


def table_storage_breakdown(company: str = "ALL", database: str = "", limit: int = 50) -> str:
    """Per-table storage split (active / time-travel / fail-safe bytes) for the
    Storage-tab drill (owner ask 2026-08-17: "drill down to the table level and
    calculate"). Ordered by TOTAL on-disk bytes so the biggest cost drivers lead
    (storage_waste orders by time-travel+fail-safe for the waste lens — a different
    question). STALE = no DML in 90 days; RETENTION_DAYS is the current
    DATA_RETENTION_TIME_IN_DAYS so a reduce-retention decision is one glance away.
    Dollars are applied in the UI at the configured $/TiB."""
    limit = max(5, min(int(limit or 50), 500))
    where = and_where(
        "m.DELETED = FALSE",
        "m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES > 0",
        companies.database_clause(company, "m.TABLE_CATALOG"),
        companies.database_equals_clause(database, "m.TABLE_CATALOG"),
    )
    return f"""
SELECT
    m.TABLE_CATALOG AS DATABASE_NAME,
    m.TABLE_SCHEMA AS SCHEMA_NAME,
    m.TABLE_NAME,
    ROUND(m.ACTIVE_BYTES / POWER(1024, 3), 2) AS ACTIVE_GB,
    ROUND(m.TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(m.FAILSAFE_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
    -- clone-retained bytes still bill after the source rows are deleted; the
    -- per-database card includes them, so the drill must too or it under-sums
    -- and hides a real driver (reviewer 2026-08-17).
    ROUND(COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) / POWER(1024, 3), 2) AS CLONE_GB,
    ROUND((m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES
           + COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0)) / POWER(1024, 3), 2) AS TOTAL_GB,
    rt.RETENTION_DAYS,
    d.LAST_DML,
    IFF(d.LAST_DML IS NULL, 'STALE', 'ACTIVE') AS STATUS,
    -- a dropped-but-still-retained table keeps billing until it ages out; the
    -- retention ALTER can't target it, so flag it so the UI drops it as a candidate.
    IFF(rt.RETENTION_DAYS IS NULL, TRUE, FALSE) AS DROPPED
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
LEFT JOIN (
    SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1
) d ON d.TABLE_ID = m.ID
{_RETENTION_JOIN_SQL}
WHERE {where}
ORDER BY m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES
         + COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) DESC
LIMIT {limit}
"""


def warehouse_hourly_activity(days: int, company: str = "ALL") -> str:
    """Hour-of-day credits vs query activity per warehouse — the input to
    the off-hours schedule advisor (credits with no queries = waste).

    A2 (audit 2026-07-31): BOTH CTEs must report the hour in ACCOUNT time. The
    metering side reads ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY, whose START_TIME
    is TIMESTAMP_LTZ and therefore renders in the SESSION timezone (UTC on a
    non-SiS runtime), while FACT_QUERY_HOURLY.HOUR_TS is naive account time. Joining
    those two hour keys silently mixed timezones, and the winning "quiet" hours were
    then pasted into an America/Chicago CRON by the schedule advisor — proposing a
    SUSPEND up to 5-6 hours early, into the morning ramp. CONVERT_TIMEZONE pins the
    metering side to account time so the two sides mean the same thing.

    B1 (audit 2026-07-31): AVG_CREDITS/AVG_QUERIES are now per CALENDAR day of the
    window, not per METERED day. DAYS_SEEN counts only the days that
    (warehouse, hour-of-day) actually metered, so a warehouse that burns at 02:00
    on 4 of 14 days reported its 4-day average as if it happened nightly — and the
    schedule advisor then monetized that x30 CALENDAR days, overstating the saving
    ~3.5x. Dividing by the full window makes the number mean "credits this hour
    costs on an average day", which is exactly what the x30 monthly claim assumes.
    DAYS_METERED stays in the output so a reader can see how sparse the hour is.
    """
    days = bounded_days(days, 30)
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH m AS (
    SELECT WAREHOUSE_NAME,
           HOUR(CONVERT_TIMEZONE('America/Chicago', START_TIME)) AS HR,
           SUM(CREDITS_USED) AS CR,
           COUNT(DISTINCT DATE(CONVERT_TIMEZONE('America/Chicago', START_TIME))) AS DAYS_SEEN
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
      AND WAREHOUSE_ID > 0
    GROUP BY 1, 2
),
q AS (
    SELECT WAREHOUSE_NAME, HOUR(HOUR_TS) AS HR, SUM(QUERY_COUNT) AS QC
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND {companies.warehouse_clause(company) or "1 = 1"}
    GROUP BY 1, 2
)
SELECT m.WAREHOUSE_NAME, m.HR AS HOUR_OF_DAY,
       ROUND(m.CR / {days}, 3) AS AVG_CREDITS,
       ROUND(COALESCE(q.QC, 0) / {days}, 1) AS AVG_QUERIES,
       m.DAYS_SEEN AS DAYS_METERED
FROM m
LEFT JOIN q ON q.WAREHOUSE_NAME = m.WAREHOUSE_NAME AND q.HR = m.HR
ORDER BY m.WAREHOUSE_NAME, m.HR
"""


def anomaly_evidence(event_date: str, warehouse_contains: str = "") -> str:
    """Evidence pack for one anomalous day: which query families' elapsed
    hours moved vs their prior-7-day average. Feeds the grounded AI
    explanation — never shown as fact, always as ranked evidence."""
    import datetime as _dt

    from app.core.sqlsafe import contains_filter

    day = _dt.date.fromisoformat(str(event_date).strip()).isoformat()  # validates
    where = and_where(
        f"START_TIME >= DATEADD('day', -7, DATE '{day}')",
        f"START_TIME < DATEADD('day', 1, DATE '{day}')",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
        contains_filter("WAREHOUSE_NAME", warehouse_contains),
    )
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
    ANY_VALUE(WAREHOUSE_NAME) AS WAREHOUSE_NAME,
    COUNT_IF(DATE(START_TIME) = DATE '{day}') AS RUNS_DAY,
    ROUND(SUM(IFF(DATE(START_TIME) = DATE '{day}', TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS ELAPSED_H_DAY,
    ROUND(SUM(IFF(DATE(START_TIME) < DATE '{day}', TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS ELAPSED_H_PRIOR_AVG
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
HAVING ELAPSED_H_DAY > 0
ORDER BY ELAPSED_H_DAY - ELAPSED_H_PRIOR_AVG DESC
LIMIT 15
"""


def metering_service_history(days: int, service: str) -> str:
    """Daily billed credits for ONE metering SERVICE_TYPE — the evidence behind a
    COST_ANOMALY_SWEEP (which day spiked) or COST_SERVERLESS_CREEP (week-over-week
    growth) alert. Scoped to the named service so the AI reasons about the metric
    that fired, not warehouse query latency."""
    from app.core.sqlsafe import sql_literal

    days = bounded_days(days)
    svc = str(service or "").strip().upper()
    return f"""
SELECT
    USAGE_DATE AS DAY,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
    ROUND(SUM(COALESCE(CREDITS_USED, 0)), 2) AS CREDITS_USED,
    ROUND(SUM(COALESCE(CREDITS_USED_COMPUTE, 0)), 2) AS CREDITS_COMPUTE,
    ROUND(SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)), 2) AS CREDITS_CLOUD_SERVICES
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
  AND UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) = {sql_literal(svc)}
GROUP BY 1, 2
ORDER BY DAY
"""


def query_family_drift_history(days: int, family_text: str, warehouse: str = "") -> str:
    """Daily runs and p50/p95 latency for the query family behind a
    PERF_FINGERPRINT_DRIFT alert. Matched by the statement sample the alert
    title carries so the drift the alert reported is visible over time — not
    whatever family is heaviest account-wide.

    The sample is real SQL (it starts with CALL/SELECT/...), so it can't go
    through the UI contains-filter sanitizer (that strips SQL keywords). It is
    matched as a literal LIKE instead: sql_literal quotes it safely and ~ escapes
    LIKE metacharacters so an underscore in a proc name stays literal.
    """
    from app.core.sqlsafe import sql_literal

    days = bounded_days(days)
    clauses = [
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "QUERY_PARAMETERIZED_HASH IS NOT NULL",
    ]
    like = str(family_text or "").strip()
    if like:
        esc = like.replace("~", "~~").replace("%", "~%").replace("_", "~_")
        clauses.append(
            f"UPPER(QUERY_TEXT) LIKE UPPER({sql_literal('%' + esc + '%', 300)}) ESCAPE '~'")
    wh = str(warehouse or "").strip()
    if wh:
        clauses.append(f"WAREHOUSE_NAME = {sql_literal(wh)}")
    where = and_where(*clauses)
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    COUNT(*) AS RUNS,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.5), 1) AS P50_SEC,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95), 1) AS P95_SEC,
    ANY_VALUE(WAREHOUSE_NAME) AS WAREHOUSE_NAME,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1
HAVING COUNT(*) > 0
ORDER BY DAY
"""


def warehouse_queue_by_hour(days: int, warehouse: str) -> str:
    """The worst queueing hours on ONE warehouse — the evidence behind a
    PERF_QUEUED_MINUTES alert (when it queues, how busy, and p95 latency), so
    the AI can point at concurrency windows rather than generic latency."""
    from app.core.sqlsafe import sql_literal

    days = bounded_days(days)
    wh = str(warehouse or "").strip()
    return f"""
SELECT
    DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
    COUNT(*) AS RUNS,
    ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)
              + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 60000.0, 1) AS QUEUED_MIN,
    ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95), 1) AS P95_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
  AND WAREHOUSE_NAME = {sql_literal(wh)}
GROUP BY 1
HAVING SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) > 0
ORDER BY QUEUED_MIN DESC
LIMIT 24
"""


def expensive_queries_usd(days: int, company: str = "ALL", limit: int = 50,
                          database: str = "", schema_contains: str = "") -> str:
    """Top queries by ALLOCATED credits — warehouse-hour credits split across
    that hour's queries by execution-time share.

    Labeled *allocated*, not billed: Snowflake bills the warehouse, not the
    query. Bucketing is by the query's start hour (a long query's later hours
    are attributed to its first — documented approximation). Idle credits in
    an hour with queries are carried pro-rata; fully idle hours are excluded
    (the idle advisor owns those).

    Attribution law (v4.34.1): the database/schema filter picks DISPLAY rows
    in the outer query only — the warehouse-hour denominator always counts
    every query in the hour. The old form filtered the q CTE that also built
    the denominator, inflating each surviving query's share.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where_q = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        companies.warehouse_clause(company),
    )
    vis = and_where(
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
    )
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH q AS (
    SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, QUERY_TYPE, EXECUTION_STATUS,
           DATABASE_NAME, SCHEMA_NAME,
           START_TIME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
           COALESCE(EXECUTION_TIME, 0) AS EXEC_MS,
           COALESCE(TOTAL_ELAPSED_TIME, 0) / 1000.0 AS ELAPSED_SEC,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where_q}
),
m AS (
    SELECT WAREHOUSE_NAME, START_TIME AS HOUR_TS, SUM(CREDITS_USED) AS HOUR_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
t AS (
    SELECT WAREHOUSE_NAME, HOUR_TS, SUM(EXEC_MS) AS TOTAL_EXEC_MS
    FROM q GROUP BY 1, 2
)
SELECT
    q.QUERY_ID,
    MAX(q.USER_NAME)        AS USER_NAME,
    MAX(q.WAREHOUSE_NAME)   AS WAREHOUSE_NAME,
    MAX(q.QUERY_TYPE)       AS QUERY_TYPE,
    MAX(q.EXECUTION_STATUS) AS EXECUTION_STATUS,
    MIN(q.START_TIME)       AS START_TIME,
    MAX(q.ELAPSED_SEC)      AS ELAPSED_SEC,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) AS ALLOCATED_CREDITS,
    MAX(q.QUERY_SNIPPET)    AS QUERY_SNIPPET
FROM q
JOIN t ON t.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND t.HOUR_TS = q.HOUR_TS
JOIN m ON m.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND m.HOUR_TS = q.HOUR_TS
WHERE {vis}
GROUP BY q.QUERY_ID
HAVING ALLOCATED_CREDITS > 0
ORDER BY ALLOCATED_CREDITS DESC
LIMIT {limit}
"""


def wasted_query_spend_usd(days: int, company: str = "ALL", warehouse_contains: str = "",
                           user_contains: str = "", database: str = "", schema_contains: str = "",
                           limit: int = 50) -> str:
    """rec#17: allocated compute burned on non-success queries — money spent on runs
    that produced nothing.

    Same hour-share allocation as expensive_queries_usd (a query's execution-time
    share of its warehouse-hour credits; the denominator ``t`` counts EVERY query in
    the hour, success or not, so a failed query's share is not inflated), then filtered
    to EXECUTION_STATUS <> 'SUCCESS' and rolled up by parameterized fingerprint so a
    broken query on a retry loop surfaces as repeat $ waste. Warehouse metering is
    compute — the caller prices at the compute rate. Fingerprint-less queries (DDL,
    etc.) fall back to QUERY_ID so they stay one row each, not one merged bucket.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where_q = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        companies.warehouse_clause(company),
    )
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    vis = and_where(
        "a.EXECUTION_STATUS <> 'SUCCESS'",
        companies.database_equals_clause(database, "a.DATABASE_NAME"),
        contains_filter("a.SCHEMA_NAME", schema_contains),
        contains_filter("a.WAREHOUSE_NAME", warehouse_contains),
        contains_filter("a.USER_NAME", user_contains),
    )
    return f"""
WITH q AS (
    SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, QUERY_TYPE, EXECUTION_STATUS,
           DATABASE_NAME, SCHEMA_NAME, ERROR_CODE,
           QUERY_PARAMETERIZED_HASH AS FINGERPRINT,
           DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
           COALESCE(EXECUTION_TIME, 0) AS EXEC_MS,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where_q}
),
m AS (
    SELECT WAREHOUSE_NAME, START_TIME AS HOUR_TS, SUM(CREDITS_USED) AS HOUR_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
t AS (
    SELECT WAREHOUSE_NAME, HOUR_TS, SUM(EXEC_MS) AS TOTAL_EXEC_MS
    FROM q GROUP BY 1, 2
),
a AS (
    SELECT q.*, m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0) AS ALLOC_CREDITS
    FROM q
    JOIN t ON t.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND t.HOUR_TS = q.HOUR_TS
    JOIN m ON m.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND m.HOUR_TS = q.HOUR_TS
)
SELECT
    COALESCE(a.FINGERPRINT, a.QUERY_ID) AS FINGERPRINT,
    MAX(a.WAREHOUSE_NAME)   AS WAREHOUSE_NAME,
    MODE(a.USER_NAME)       AS USER_NAME,
    MAX(a.QUERY_TYPE)       AS QUERY_TYPE,
    MAX(a.EXECUTION_STATUS) AS EXECUTION_STATUS,
    MAX(a.ERROR_CODE)       AS ERROR_CODE,
    COUNT(DISTINCT a.QUERY_ID) AS FAILED_RUNS,
    ROUND(SUM(a.ALLOC_CREDITS), 4) AS WASTED_CREDITS,
    MAX(a.QUERY_SNIPPET)    AS QUERY_SNIPPET
FROM a
WHERE {vis}
GROUP BY 1
HAVING SUM(a.ALLOC_CREDITS) > 0
ORDER BY WASTED_CREDITS DESC
LIMIT {limit}
"""


def storage_reclaim(company: str = "ALL", min_gb: float = 1.0, read_days: int = 90) -> str:
    """storage_waste + read evidence: LAST_READ from ACCESS_HISTORY so a table
    can be STALE (no DML) *and* NEVER_READ — the safe-to-archive shortlist.

    ACCESS_HISTORY needs Enterprise edition; the page degrades to
    storage_waste() when this errors. Deliberately NOT in the canary registry:
    on Standard edition it would be a permanently red row (alert noise), and
    the panel already labels the degraded state.

    D4 (audit 2026-07-31): reads join on objectId, not on the assembled
    "DB.SCHEMA.TABLE" string. ACCESS_HISTORY reports objectName exactly as the
    identifier was created, so any quoted/mixed-case or special-character
    identifier never matched TABLE_STORAGE_METRICS' columns and the table came
    back NEVER_READ = TRUE — a "safe to archive" verdict on a table that is
    read every day. objectId is the same TABLE_ID the DML leg already joins on.
    """
    read_days = bounded_days(read_days, 90)
    min_bytes = int(max(0.1, float(min_gb)) * 1024 ** 3)
    where = and_where(
        "m.DELETED = FALSE",
        f"m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES >= {min_bytes}",
        companies.database_clause(company, "m.TABLE_CATALOG"),
    )
    return f"""
WITH reads AS (
    SELECT f.value:"objectId"::NUMBER AS TABLE_ID, MAX(a.QUERY_START_TIME) AS LAST_READ
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{read_days}, CURRENT_TIMESTAMP())
      AND f.value:"objectDomain"::STRING = 'Table'
      AND f.value:"objectId" IS NOT NULL
    GROUP BY 1
)
SELECT
    m.TABLE_CATALOG AS DATABASE_NAME,
    m.TABLE_SCHEMA  AS SCHEMA_NAME,
    m.TABLE_NAME,
    ROUND(m.ACTIVE_BYTES / POWER(1024, 3), 2)      AS ACTIVE_GB,
    ROUND(m.TIME_TRAVEL_BYTES / POWER(1024, 3), 2) AS TIME_TRAVEL_GB,
    ROUND(m.FAILSAFE_BYTES / POWER(1024, 3), 2)    AS FAILSAFE_GB,
    ROUND(m.RETAINED_FOR_CLONE_BYTES / POWER(1024, 3), 2) AS CLONE_RETAINED_GB,
    rt.RETENTION_DAYS,
    IFF(rt.RETENTION_DAYS IS NULL, FALSE, TRUE) AS RETENTION_KNOWN,
    d.LAST_DML,
    r.LAST_READ,
    IFF(d.LAST_DML IS NULL, 'STALE', 'ACTIVE') AS DML_STATUS,
    IFF(r.LAST_READ IS NULL, TRUE, FALSE)      AS NEVER_READ
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
LEFT JOIN (
    SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1
) d ON d.TABLE_ID = m.ID
{_RETENTION_JOIN_SQL}
LEFT JOIN reads r ON r.TABLE_ID = m.ID
WHERE {where}
ORDER BY (m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES + m.RETAINED_FOR_CLONE_BYTES) DESC
LIMIT 50
"""

def expensive_patterns_usd(days: int, company: str = "ALL", limit: int = 30) -> str:
    """Recurring cost patterns: the SAME hour-share allocation as
    expensive_queries_usd, grouped by QUERY_PARAMETERIZED_HASH.

    One $9 query run 400x/day outranks a single $300 one — this is where
    caching/materialization actually pays. USD_PER_DAY = allocated / window.

    Attribution law (C3, audit 2026-07-31): the QUERY_PARAMETERIZED_HASH filter
    picks DISPLAY rows in the OUTER query only. It used to sit in the q CTE that
    also builds the `t` denominator, so a warehouse-hour's credits were divided
    across only the hashed queries in that hour — every pattern's $ (and the
    $/run the pricing panel feeds to price_per_run_bounds) was inflated by the
    unhashed share. Same fix, same reason, as expensive_queries_usd's `vis`.
    """
    days = bounded_days(days)
    min_runs = max(2, (5 * days + 29) // 30)
    limit = max(5, min(int(limit or 30), 100))
    where_q = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "WAREHOUSE_NAME IS NOT NULL",
        "COALESCE(EXECUTION_TIME, 0) > 0",
        companies.warehouse_clause(company),
    )
    where_m = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
WITH q AS (
    SELECT QUERY_PARAMETERIZED_HASH AS PATTERN_HASH,
           QUERY_ID, USER_NAME, WAREHOUSE_NAME,
           DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
           COALESCE(EXECUTION_TIME, 0) AS EXEC_MS,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where_q}
),
m AS (
    SELECT WAREHOUSE_NAME, START_TIME AS HOUR_TS, SUM(CREDITS_USED) AS HOUR_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
    WHERE {where_m}
    GROUP BY 1, 2
),
t AS (
    SELECT WAREHOUSE_NAME, HOUR_TS, SUM(EXEC_MS) AS TOTAL_EXEC_MS
    FROM q GROUP BY 1, 2
)
SELECT
    q.PATTERN_HASH,
    COUNT(DISTINCT q.QUERY_ID)  AS RUNS,
    COUNT(DISTINCT q.USER_NAME) AS USERS,
    COUNT(DISTINCT q.WAREHOUSE_NAME) AS WAREHOUSES,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) AS ALLOCATED_CREDITS,
    SUM(m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)) / {days} AS CREDITS_PER_DAY,
    ANY_VALUE(q.QUERY_SNIPPET)  AS QUERY_SNIPPET
FROM q
JOIN t ON t.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND t.HOUR_TS = q.HOUR_TS
JOIN m ON m.WAREHOUSE_NAME = q.WAREHOUSE_NAME AND m.HOUR_TS = q.HOUR_TS
WHERE q.PATTERN_HASH IS NOT NULL
GROUP BY q.PATTERN_HASH
HAVING RUNS >= {min_runs} AND ALLOCATED_CREDITS > 0
ORDER BY ALLOCATED_CREDITS DESC
LIMIT {limit}
"""

def table_tco(database: str, schema: str, table: str, days: int = 30) -> str:
    """Object-level cost evidence: reads, writers, last touch from
    ACCESS_HISTORY for ONE table (Enterprise edition; the page degrades).
    Storage dollars come from the reclaim row the caller already has."""
    from app.core.sqlsafe import safe_identifier

    fqn = ".".join(safe_identifier(part) for part in (database, schema, table)).upper()
    days = bounded_days(days, 90)
    return f"""
WITH touches AS (
    SELECT a.QUERY_START_TIME, a.USER_NAME,
           f.value:"objectName"::STRING AS FQN, 'READ' AS KIND
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectName"::STRING = '{fqn}'
    UNION ALL
    SELECT a.QUERY_START_TIME, a.USER_NAME,
           f.value:"objectName"::STRING, 'WRITE'
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.OBJECTS_MODIFIED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectName"::STRING = '{fqn}'
)
SELECT KIND,
       COUNT(*)                  AS TOUCHES,
       COUNT(DISTINCT USER_NAME) AS DISTINCT_USERS,
       MAX(QUERY_START_TIME)     AS LAST_TOUCH,
       ANY_VALUE(USER_NAME)      AS SAMPLE_USER
FROM touches
GROUP BY KIND
"""


def measured_query_costs(days: int, company: str = "ALL", database: str = "",
                         schema_contains: str = "", warehouse_contains: str = "",
                         user_contains: str = "", limit: int = 50) -> str:
    """Top queries by MEASURED compute credits (QUERY_ATTRIBUTION_HISTORY).

    Attribution excludes warehouse idle time: this answers "what did running
    THIS query cost" — the complement of expensive_queries_usd's allocated
    lens (which spreads the whole warehouse-hour bill, idle included).
    ~8h view lag; rows without attributed credits are omitted.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "q.DATABASE_NAME"),
        contains_filter("q.SCHEMA_NAME", schema_contains),
        contains_filter("q.WAREHOUSE_NAME", warehouse_contains),
        contains_filter("q.USER_NAME", user_contains),
    )
    return f"""
WITH q AS (
    SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, DATABASE_NAME, SCHEMA_NAME,
           QUERY_TYPE, EXECUTION_STATUS, START_TIME,
           COALESCE(TOTAL_ELAPSED_TIME, 0) AS ELAPSED_MS,
           LEFT(QUERY_TEXT, 140) AS QUERY_SNIPPET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
)
-- Join the FILTERED window first, THEN aggregate credits: pre-aggregating
-- the entire attribution view was the 139s-family pattern (Codex #11 —
-- the same fix the graph and procedure builders already got).
SELECT
    q.QUERY_ID, q.USER_NAME, q.WAREHOUSE_NAME, q.DATABASE_NAME, q.SCHEMA_NAME,
    q.QUERY_TYPE, q.EXECUTION_STATUS, q.START_TIME,
    ROUND(q.ELAPSED_MS / 1000.0, 1) AS ELAPSED_SEC,
    ROUND(SUM(a.CREDITS_ATTRIBUTED_COMPUTE + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)), 6) AS CREDITS,
    q.QUERY_SNIPPET
FROM q
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
  ON a.QUERY_ID = q.QUERY_ID
 AND a.START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
GROUP BY q.QUERY_ID, q.USER_NAME, q.WAREHOUSE_NAME, q.DATABASE_NAME, q.SCHEMA_NAME,
         q.QUERY_TYPE, q.EXECUTION_STATUS, q.START_TIME, q.ELAPSED_MS, q.QUERY_SNIPPET
HAVING SUM(a.CREDITS_ATTRIBUTED_COMPUTE + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
ORDER BY CREDITS DESC
LIMIT {limit}
"""


def procedure_costs_usd(days: int, company: str = "ALL", database: str = "",
                        schema_contains: str = "", limit: int = 50) -> str:
    """$/call leaderboard for EVERY stored procedure (measured credits).

    Child statements roll up to the CALL via ROOT_QUERY_ID, so a procedure's
    cost includes everything it ran. DATABASE/SCHEMA are the CALL's session
    context (the proc may read other databases — labeled in the UI).
    Complements change-impact, which prices only changed objects.
    """
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where = and_where(
        f"c.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "c.QUERY_TYPE = 'CALL'",
        companies.warehouse_clause(company, "c.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "c.DATABASE_NAME"),
        contains_filter("c.SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH att AS (
    SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
           SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
      -- Prune before the GROUP BY: only rows rolling up to a CALL matter
      -- (children carry the CALL's id as ROOT_QUERY_ID). Perf pass #9.
      AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
          SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
          WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
            AND QUERY_TYPE = 'CALL'
      )
    GROUP BY 1
),
calls AS (
    SELECT c.QUERY_ID, c.DATABASE_NAME, c.SCHEMA_NAME, c.EXECUTION_STATUS,
           COALESCE(c.TOTAL_ELAPSED_TIME, 0) AS ELAPSED_MS,
           REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY c
    WHERE {where}
)
SELECT
    calls.PROC_NAME, calls.DATABASE_NAME, calls.SCHEMA_NAME,
    COUNT(*) AS CALLS,
    ROUND(100 * COUNT_IF(calls.EXECUTION_STATUS <> 'SUCCESS') / COUNT(*), 1) AS FAIL_PCT,
    ROUND(APPROX_PERCENTILE(calls.ELAPSED_MS, 0.95) / 1000, 1) AS P95_S,
    ROUND(SUM(COALESCE(att.CREDITS, 0)), 4) AS TOTAL_CREDITS,
    ROUND(SUM(COALESCE(att.CREDITS, 0)) / COUNT(*), 6) AS CREDITS_PER_CALL,
    COUNT(att.CREDITS) AS ATTRIBUTED_CALLS
FROM calls
LEFT JOIN att ON att.RID = calls.QUERY_ID
WHERE calls.PROC_NAME IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY TOTAL_CREDITS DESC
LIMIT {limit}
"""


def call_cost_lookup(ident: str, days: int = 7) -> str:
    """Measured $ per stored-proc CALL: pass a CALL's QUERY_ID or a
    SESSION_ID (owner question 2026-07-10: 'three procs in one session, no
    graph id'). No task graph needed — children roll up to their CALL via
    QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID for ad-hoc sessions too.
    ~6h attribution lag; idle time excluded; children that ran without a
    warehouse don't appear (same caveats as the proc leaderboard)."""
    from app.core.sqlsafe import sql_literal
    days = bounded_days(days, 30)
    lit = sql_literal(str(ident or "").strip())
    return f"""
WITH calls AS (
    SELECT QUERY_ID, SESSION_ID, START_TIME, LEFT(QUERY_TEXT, 80) AS CALL_TEXT
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND QUERY_TYPE = 'CALL'
      AND (QUERY_ID = {lit} OR SESSION_ID::VARCHAR = {lit})
)
SELECT c.QUERY_ID, c.START_TIME, c.CALL_TEXT,
       COUNT(a.QUERY_ID) AS ATTRIBUTED_QUERIES,
       ROUND(SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0)
                 + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)), 6) AS CREDITS
FROM calls c
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
       ON a.ROOT_QUERY_ID = c.QUERY_ID OR a.QUERY_ID = c.QUERY_ID
GROUP BY c.QUERY_ID, c.START_TIME, c.CALL_TEXT
ORDER BY c.START_TIME
LIMIT 100
"""


def call_children_costs(call_query_id: str, days: int = 7) -> str:
    """The child breakdown for ONE CALL: every attributed statement under
    ROOT_QUERY_ID with its own credits — 'where inside the proc did the
    money go'. The CALL's own row is included and labeled."""
    from app.core.sqlsafe import sql_literal
    days = bounded_days(days, 30)
    lit = sql_literal(str(call_query_id or "").strip())
    return f"""
SELECT a.QUERY_ID,
       a.PARENT_QUERY_ID,
       a.ROOT_QUERY_ID,
       IFF(a.QUERY_ID = {lit}, 'CALL (own time)', COALESCE(q.QUERY_TYPE, '?')) AS STEP_TYPE,
       LEFT(COALESCE(q.QUERY_TEXT, '(history pruned)'), 120) AS STEP_PREVIEW,
       q.START_TIME AS STEP_START,
       ROUND(COALESCE(q.TOTAL_ELAPSED_TIME, 0) / 1000.0, 1) AS ELAPSED_SEC,
       ROUND(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0)
             + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0), 6) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
       ON q.QUERY_ID = a.QUERY_ID
      AND q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
WHERE a.ROOT_QUERY_ID = {lit} OR a.QUERY_ID = {lit}
ORDER BY STEP_START, a.QUERY_ID
LIMIT 500
"""


def proc_cost_trend(proc_name: str, days: int, company: str = "ALL",
                    database: str = "", schema_contains: str = "") -> str:
    """Daily measured $ for ONE named procedure (owner ask 2026-07-11:
    "can I enter it myself" — yes, this is the type-the-name panel).

    Same extraction and ROOT_QUERY_ID rollup as the $/call leaderboard, so
    the two always agree; a bare name matches qualified CALLs via the
    suffix arm. Attribution lags ~6h; idle excluded; DATABASE/SCHEMA are
    the CALL's session context. POSIX classes only.
    """
    from app.core.sqlsafe import contains_filter, sql_literal

    days = bounded_days(days)
    name = str(proc_name or "").strip().upper().rstrip("(")
    lit = sql_literal(name)
    # Escape LIKE metacharacters so an underscore in a proc name stays literal (else
    # '%.SP_LOAD' also matches SPZLOAD) — mirrors query_family_drift_history + ESCAPE '~'.
    _esc = name.replace("~", "~~").replace("%", "~%").replace("_", "~_")
    suffix = sql_literal("%." + _esc)
    where = and_where(
        f"c.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "c.QUERY_TYPE = 'CALL'",
        companies.warehouse_clause(company, "c.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "c.DATABASE_NAME"),
        contains_filter("c.SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH calls AS (
    SELECT c.QUERY_ID, DATE(c.START_TIME) AS DAY, c.EXECUTION_STATUS,
           REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY c
    WHERE {where}
),
named AS (
    SELECT * FROM calls
    WHERE PROC_NAME = {lit} OR PROC_NAME LIKE {suffix} ESCAPE '~'
),
att AS (
    SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
           SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
      AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (SELECT QUERY_ID FROM named)
    GROUP BY 1
)
SELECT n.DAY,
       COUNT(*) AS CALLS,
       COUNT_IF(n.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
       ROUND(SUM(COALESCE(a.CREDITS, 0)), 6) AS CREDITS,
       ROUND(SUM(COALESCE(a.CREDITS, 0)) / NULLIF(COUNT(*), 0), 6) AS CREDITS_PER_CALL,
       COUNT(a.CREDITS) AS ATTRIBUTED_CALLS
FROM named n
LEFT JOIN att a ON a.RID = n.QUERY_ID
GROUP BY n.DAY
ORDER BY n.DAY
LIMIT 400
"""


def clustering_by_table(days: int = 30, company: str = "ALL") -> str:
    """Automatic-clustering spend per table (COST_DB recon R7) — serverless
    reclustering credits are the classic silent burner; a table rewriting
    itself daily shows up here long before anyone looks for it."""
    days = bounded_days(days, 90)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "CREDITS_USED > 0",
        companies.database_clause(company, "DATABASE_NAME"),
    )
    return f"""
SELECT
    DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TABLE_NAME AS TABLE_FQN,
    ROUND(SUM(CREDITS_USED), 4) AS CREDITS,
    ROUND(SUM(COALESCE(NUM_BYTES_RECLUSTERED, 0)) / POWER(1024, 4), 3) AS TB_RECLUSTERED,
    COUNT(*) AS RECLUSTER_RUNS
FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY CREDITS DESC
LIMIT 25
"""


def query_insights_feed(days: int = 7) -> str:
    """Snowflake's OWN per-query improvement suggestions (repo review wave 2:
    QUERY_INSIGHTS) rolled up by insight type — free, vendor-authored fixes for
    repeated anti-patterns, beside our home-grown family heuristics.

    OPTIONAL view (newer accounts/editions; schema may drift) — callers MUST
    pass probe=True and degrade honestly. Columns kept minimal on purpose."""
    days = bounded_days(days)
    return f"""
SELECT
    INSIGHT_TYPE_ID AS INSIGHT_TYPE,
    COUNT(*) AS OCCURRENCES,
    COUNT(DISTINCT QUERY_ID) AS QUERIES,
    ANY_VALUE(MESSAGE) AS SAMPLE_MESSAGE,
    MAX(START_TIME) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_INSIGHTS
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY OCCURRENCES DESC
LIMIT 50
"""
