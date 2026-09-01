"""Operations SQL builders: queries, tasks, warehouses, contention."""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.core.sqlsafe import contains_filter, sql_literal
from app.data.common import and_where, bounded_days, scope_window_where


def _query_scope(days: int, company: str, warehouse_contains: str = "", user_contains: str = "",
                 database: str = "", schema_contains: str = "", *,
                 bounds: tuple | None = None) -> str:
    # C10: scope QUERY_HISTORY by WAREHOUSE company only — COMPANY_FOR_WAREHOUSE, the
    # exact stamp the FACT_QUERY_HOURLY loader and the Cost pages use — NOT warehouse
    # AND user. The old intersection dropped all cross-company activity (a Trexis user
    # on an ALFA warehouse appeared only under ALL, and ALFA+Trexis+UNKNOWN did not
    # partition ALL) and could never reconcile against warehouse-grain spend. The
    # user_contains free-text filter still applies within the scope.
    comp = str(company or "ALL")
    company_scope = ("" if comp.upper() == "ALL"
                     else f"{companies.company_case_sql('WAREHOUSE_NAME')} = {sql_literal(comp)}")
    return and_where(
        scope_window_where("START_TIME", days, bounds=bounds),
        company_scope,
        companies.database_equals_clause(database),
        contains_filter("WAREHOUSE_NAME", warehouse_contains),
        contains_filter("USER_NAME", user_contains),
        contains_filter("SCHEMA_NAME", schema_contains),
    )


def query_window_summary(days: int, company: str = "ALL", warehouse_contains: str = "", user_contains: str = "",
                         database: str = "", schema_contains: str = "", *,
                         bounds: tuple | None = None) -> str:
    """One-row query health summary for the window."""
    days = bounded_days(days)
    return f"""
SELECT
    COUNT(*) AS QUERY_COUNT,
    SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC,
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {_query_scope(days, company, warehouse_contains, user_contains, database, schema_contains, bounds=bounds)}
"""


def top_queries_by_elapsed(days: int, company: str = "ALL", limit: int = 50,
                           warehouse_contains: str = "", user_contains: str = "",
                           database: str = "", schema_contains: str = "", *,
                           bounds: tuple | None = None) -> str:
    """Heaviest queries in the window (elapsed basis; cost labeled estimate)."""
    days = bounded_days(days)
    limit = max(1, min(int(limit), 500))
    return f"""
SELECT
    QUERY_ID,
    START_TIME,
    USER_NAME,
    WAREHOUSE_NAME,
    WAREHOUSE_SIZE,
    DATABASE_NAME,
    QUERY_TYPE,
    EXECUTION_STATUS,
    TOTAL_ELAPSED_TIME / 1000.0 AS ELAPSED_SEC,
    (COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3) AS SPILL_REMOTE_GB,
    LEFT(QUERY_TEXT, 180) AS QUERY_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {_query_scope(days, company, warehouse_contains, user_contains, database, schema_contains, bounds=bounds)}
ORDER BY TOTAL_ELAPSED_TIME DESC
LIMIT {limit}
"""


def failures_by_error(days: int, company: str = "ALL", warehouse_contains: str = "",
                      user_contains: str = "", database: str = "", schema_contains: str = "", *,
                      bounds: tuple | None = None) -> str:
    """Failed queries grouped by error code/message family.

    v4.157.0: honors the warehouse/user contains filters too — the Queries
    section's filter contract advertises them, but this panel used to scope by
    company/db/schema only, so 'Warehouse contains' silently didn't narrow the
    failure taxonomy.
    """
    days = bounded_days(days)
    where = and_where(
        _query_scope(days, company, warehouse_contains=warehouse_contains,
                     user_contains=user_contains, database=database, schema_contains=schema_contains,
                     bounds=bounds),
        "EXECUTION_STATUS <> 'SUCCESS'",
    )
    return f"""
SELECT
    COALESCE(ERROR_CODE::VARCHAR, 'UNKNOWN') AS ERROR_CODE,
    LEFT(COALESCE(ERROR_MESSAGE, 'Unknown error'), 140) AS ERROR_MESSAGE,
    COUNT(*) AS FAILURES,
    COUNT(DISTINCT USER_NAME) AS USERS_AFFECTED,
    MAX(START_TIME) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2
ORDER BY FAILURES DESC
LIMIT 50
"""


def task_runs(days: int, company: str = "ALL", database: str = "", schema_contains: str = "", *,
              bounds: tuple | None = None) -> str:
    """Task run outcomes grouped by task, newest failures surfaced."""
    days = bounded_days(days)
    where = and_where(
        scope_window_where("QUERY_START_TIME", days, bounds=bounds),
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH runs AS (
    SELECT
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME,
        QUERY_START_TIME,
        COMPLETED_TIME,
        STATE,
        ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
    -- Auto-retries emit multiple rows sharing one SCHEDULED_TIME; collapse each
    -- scheduled run to its TERMINAL attempt (latest COMPLETED_TIME) so RUNS counts
    -- scheduled runs not attempts, and a FAILED attempt that later SUCCEEDED on retry
    -- is not counted as a failure. Mirrors task_recent_states.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME AS TASK_NAME,
    COUNT(*) AS RUNS,
    SUM(IFF(STATE = 'FAILED', 1, 0)) AS FAILED,
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)) AS AVG_SEC,
    MAX(QUERY_START_TIME) AS LAST_RUN,
    MAX_BY(STATE, QUERY_START_TIME) AS LAST_STATE,
    MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 200), QUERY_START_TIME) AS LAST_ERROR
FROM runs
GROUP BY 1, 2, 3
ORDER BY FAILED DESC, LAST_RUN DESC
LIMIT 200
"""


def task_recent_states(days: int, company: str = "ALL", database: str = "",
                       schema_contains: str = "", per_task: int = 12) -> str:
    """Recent terminal runs per task (newest first), for the failure-streak fold.

    ``task_runs`` collapses each task with ``MAX_BY(STATE, ...)`` — it sees only the
    newest state, so it can't tell a one-off failure from a task stuck failing every
    run. This returns the last ``per_task`` SUCCEEDED/FAILED runs per task so
    ``logic.insights.task_failure_streaks`` can count the leading FAILED streak
    (consecutive failures since the last success). SKIPPED/CANCELLED are excluded so
    the streak measures real success-vs-failure, not scheduler skips. TASK_HISTORY
    prunes on SCHEDULED_TIME, so the window bounds that column."""
    days = bounded_days(days)
    per_task = max(2, min(int(per_task), 50))
    where = and_where(
        f"SCHEDULED_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "STATE IN ('SUCCEEDED', 'FAILED')",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH attempts AS (
    SELECT
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME AS TASK_NAME,
        SCHEDULED_TIME,
        STATE,
        LEFT(COALESCE(ERROR_MESSAGE, ''), 200) AS ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
    -- Auto-retries emit multiple rows sharing one SCHEDULED_TIME; collapse each
    -- scheduled run to its TERMINAL attempt (latest COMPLETED_TIME) so the streak
    -- counts failed RUNS not attempts, and a FAILED retry can never sort ahead of
    -- the run's SUCCEEDED final attempt (a plain SCHEDULED_TIME tie is nondeterministic).
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT DATABASE_NAME, SCHEMA_NAME, TASK_NAME, SCHEDULED_TIME, STATE, ERROR_MESSAGE
FROM attempts
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY DATABASE_NAME, SCHEMA_NAME, TASK_NAME
    ORDER BY SCHEDULED_TIME DESC) <= {per_task}
ORDER BY DATABASE_NAME, SCHEMA_NAME, TASK_NAME, SCHEDULED_TIME DESC
"""


def task_freshness_sla(days: int = 14, company: str = "ALL", database: str = "",
                       schema_contains: str = "") -> str:
    """Per-task refresh cadence + silence, so a task that quietly STOPS being
    scheduled (not failing, just gone) becomes visible.

    Mirrors ``insights_sql.pipeline_sla_forecast``'s LAG->MEDIAN cadence
    construction, retargeted from TABLE_DML_HISTORY to TASK_HISTORY: the median gap
    (minutes) between successive SCHEDULED_TIMEs is the task's expected cadence, and
    MINS_SINCE_SUCCESS is DATEDIFF to now from the last SUCCEEDED run.
    ``logic.insights.task_freshness_status`` then classifies On-time / Late / Stale.
    SCHEDULED_TIME/CURRENT_TIMESTAMP are TIMESTAMP_LTZ (absolute instants), so the
    DATEDIFF is correct regardless of session time zone. Only tasks with >= 3
    observed intervals (a trustworthy cadence) are returned."""
    days = max(3, min(int(days or 14), 90))
    where = and_where(
        f"SCHEDULED_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.database_clause(company, "DATABASE_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH runs AS (
    SELECT DATABASE_NAME, SCHEMA_NAME, NAME AS TASK_NAME, SCHEDULED_TIME,
           DATEDIFF('minute',
                    LAG(SCHEDULED_TIME) OVER (
                        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                        ORDER BY SCHEDULED_TIME),
                    SCHEDULED_TIME) AS GAP_MIN
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
), cadence AS (
    SELECT DATABASE_NAME, SCHEMA_NAME, TASK_NAME,
           -- median over POSITIVE gaps only, so MEDIAN and INTERVALS agree: a
           -- retry-heavy task (GAP_MIN=0 for same-SCHEDULED_TIME retries) can't get
           -- a 0-minute median that would make it structurally unflaggable.
           MEDIAN(IFF(GAP_MIN > 0, GAP_MIN, NULL)) AS MEDIAN_GAP_MIN,
           -- p90 of positive gaps = the LONGEST NORMAL scheduled gap (a weekend for a weekday-only
           -- cron, an overnight for a business-hours cron). The classifier judges silence against
           -- this, not 2x the median, so a scheduled idle window is not misread as 'silently
           -- stopped'. p90 (not MAX) so a one-off maintenance pause does not inflate it (bug-hunt
           -- 2026-08-30).
           APPROX_PERCENTILE(IFF(GAP_MIN > 0, GAP_MIN, NULL), 0.9) AS LONG_GAP_MIN,
           COUNT_IF(GAP_MIN IS NOT NULL AND GAP_MIN > 0) AS INTERVALS
    FROM runs
    GROUP BY 1, 2, 3
), last_run AS (
    SELECT DATABASE_NAME, SCHEMA_NAME, NAME AS TASK_NAME,
           MAX(IFF(STATE = 'SUCCEEDED', SCHEDULED_TIME, NULL)) AS LAST_SUCCESS,
           MAX(SCHEDULED_TIME) AS LAST_SCHEDULED,
           MAX_BY(STATE, SCHEDULED_TIME) AS LAST_STATE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE {where}
    GROUP BY 1, 2, 3
)
SELECT c.DATABASE_NAME, c.SCHEMA_NAME, c.TASK_NAME,
       ROUND(c.MEDIAN_GAP_MIN, 1) AS MEDIAN_GAP_MIN,
       ROUND(c.LONG_GAP_MIN, 1) AS LONG_GAP_MIN,
       c.INTERVALS,
       l.LAST_SUCCESS, l.LAST_SCHEDULED, l.LAST_STATE,
       DATEDIFF('minute', l.LAST_SUCCESS, CURRENT_TIMESTAMP()) AS MINS_SINCE_SUCCESS
FROM cadence c
JOIN last_run l
  ON l.DATABASE_NAME = c.DATABASE_NAME
 AND l.SCHEMA_NAME = c.SCHEMA_NAME
 AND l.TASK_NAME = c.TASK_NAME
WHERE c.MEDIAN_GAP_MIN IS NOT NULL AND c.INTERVALS >= 3
-- NULLS FIRST: a task with no SUCCEEDED run in the window has NULL
-- MINS_SINCE_SUCCESS but is the HIGHEST-severity 'silent' case — keep it under the
-- LIMIT, don't let it be truncated first (the classifier ranks these Stale/High).
ORDER BY MINS_SINCE_SUCCESS DESC NULLS FIRST
LIMIT 200
"""


def warehouse_pressure(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Queue and spill pressure per warehouse for the window."""
    days = bounded_days(days)
    return f"""
SELECT
    WAREHOUSE_NAME,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 AS QUEUED_SEC,
    SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB,
    APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95) AS P95_ELAPSED_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {and_where(scope_window_where("START_TIME", days, bounds=bounds), "WAREHOUSE_NAME IS NOT NULL", companies.warehouse_clause(company))}
GROUP BY 1
HAVING SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) > 0
    OR SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) > 0
ORDER BY QUEUED_SEC DESC
LIMIT 50
"""


def lock_contention(days: int, *, bounds: tuple | None = None) -> str:
    """Lock waits (account-wide; LOCK_WAIT_HISTORY has no warehouse grain).
    Window capped at 7d (was 14): the 14-day scan read ~56GB per run on this
    account (fleet board, 2026-07-10) and lock triage is a this-week
    question — history beyond that lives in the incident timeline."""
    days = bounded_days(days, maximum=7)
    return f"""
SELECT
    DATABASE_NAME,
    SCHEMA_NAME,
    OBJECT_NAME,
    LOCK_TYPE,
    COUNT(*) AS WAIT_EVENTS,
    -- COALESCE(...REQUESTED_AT) zeroed the WORST cases: locks never acquired
    -- (statement aborted/timed out) counted as zero wait (review #12). They
    -- are split out and ranked first instead.
    SUM(IFF(ACQUIRED_AT IS NOT NULL,
            DATEDIFF('second', REQUESTED_AT, ACQUIRED_AT), 0)) AS ACQUIRED_WAIT_SEC,
    COUNT_IF(ACQUIRED_AT IS NULL) AS NEVER_ACQUIRED,
    MAX(REQUESTED_AT) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY
WHERE {scope_window_where("REQUESTED_AT", days, bounds=bounds)}
GROUP BY 1, 2, 3, 4
ORDER BY NEVER_ACQUIRED DESC, ACQUIRED_WAIT_SEC DESC
LIMIT 50
"""


def lock_wait_object_detail(database: str, schema: str, object_name: str,
                            days: int = 2, limit: int = 100) -> str:
    """Per-object lock-wait events for the Control-Room spike drill.
    Account-wide source (LOCK_WAIT_HISTORY has no company grain); the object
    triplet narrows it. Window default 2d (the spike's 'last day' framing),
    capped at 7d -- ACCOUNT_USAGE is not pruned by object name, so the scan
    reads the whole window regardless of the filter (lock_contention notes
    the 14d scan hit ~56GB); keep it short and interaction-gated.

    Sentinel: the parent mart stores COALESCE(<col>,'NONE'), so a clicked
    row's DATABASE_NAME/SCHEMA_NAME/OBJECT_NAME may be the literal 'NONE'
    standing for a SQL NULL on the raw view. Each key emits ``<COL> IS NULL``
    for 'NONE', else an exact ``<COL> = <literal>`` -- interpolating 'NONE'
    as a literal would silently return zero rows for NULL-keyed objects.

    Columns limited to those confirmed present on this account (the
    lock_contention sibling reads DATABASE_NAME/SCHEMA_NAME/OBJECT_NAME/
    LOCK_TYPE/REQUESTED_AT/ACQUIRED_AT) plus QUERY_ID; BLOCKER_QUERIES /
    TRANSACTION_ID / RELEASED_AT are deliberately skipped (unconfirmed here
    -- a missing column is a compile error that breaks the whole drill)."""
    days = bounded_days(days, maximum=7)
    limit = max(1, min(int(limit), 500))

    def _key_predicate(column: str, value: str) -> str:
        return f"{column} IS NULL" if value == "NONE" else f"{column} = {sql_literal(value)}"

    where = and_where(
        f"REQUESTED_AT >= DATEADD('day', -{days}, CURRENT_DATE())",
        _key_predicate("DATABASE_NAME", database),
        _key_predicate("SCHEMA_NAME", schema),
        _key_predicate("OBJECT_NAME", object_name),
    )
    return f"""
SELECT
    REQUESTED_AT,
    ACQUIRED_AT,
    LOCK_TYPE,
    QUERY_ID,
    DATABASE_NAME,
    SCHEMA_NAME,
    OBJECT_NAME,
    DATEDIFF('second', REQUESTED_AT, ACQUIRED_AT) AS WAIT_SEC,
    IFF(ACQUIRED_AT IS NULL, 1, 0) AS NEVER_ACQ
FROM SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY
WHERE {where}
ORDER BY (ACQUIRED_AT IS NULL) DESC, WAIT_SEC DESC
LIMIT {limit}
"""


def poor_pruning_queries(days: int, company: str = "ALL", database: str = "",
                         schema_contains: str = "") -> str:
    """Query families scanning >80% of a 100+-partition table — missing
    clustering keys or unpruned predicates."""
    days = bounded_days(days)
    scope = _query_scope(days, company, "", "", database, schema_contains)
    return f"""
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(LEFT(QUERY_TEXT, 90)) AS SAMPLE_TEXT,
    COUNT(*) AS RUNS,
    ROUND(AVG(PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0)) * 100, 1) AS AVG_SCAN_PCT,
    ROUND(AVG(PARTITIONS_TOTAL), 0) AS AVG_PARTITIONS,
    ROUND(SUM(BYTES_SCANNED) / POWER(1024, 4), 3) AS TB_SCANNED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {scope}
  AND EXECUTION_STATUS = 'SUCCESS'
  AND PARTITIONS_TOTAL >= 100
  AND PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0) > 0.8
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY SUM(BYTES_SCANNED) DESC
LIMIT 25
"""


def query_optimization_triage(days: int, company: str = "ALL", warehouse_contains: str = "",
                              user_contains: str = "", database: str = "",
                              schema_contains: str = "", limit: int = 50, *,
                              bounds: tuple | None = None) -> str:
    """The specific statements an optimizer should fix, ranked by inefficiency:
    remote spill first, then poor partition pruning, then huge cold-cache scans.

    ``top_queries_by_elapsed`` ranks by elapsed time only — the slowest, not the
    most fixable. This keeps QUERY_ID grain but filters to 'real work' (drops CALL
    wrappers, which meter through their children, and the app's own Streamlit /
    OVERWATCH-tagged queries) and requires a genuine inefficiency signal (remote
    spill, or > 50 GB scanned), so every row is actionable. Column names follow
    styled_table's byte/percent auto-humanize convention (SPILL_REMOTE_GB,
    GB_SCANNED, SCAN_PCT, CACHE_PCT)."""
    days = bounded_days(days)
    limit = max(1, min(int(limit), 200))
    where = and_where(
        _query_scope(days, company, warehouse_contains, user_contains, database, schema_contains,
                     bounds=bounds),
        "EXECUTION_STATUS = 'SUCCESS'",
        "QUERY_TYPE <> 'CALL'",
        "UPPER(COALESCE(QUERY_TEXT, '')) NOT LIKE 'EXECUTE STREAMLIT%'",
        "UPPER(COALESCE(QUERY_TEXT, '')) NOT LIKE '%OVERWATCH_APP%'",
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
        "(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0 "
        "OR COALESCE(BYTES_SCANNED, 0) > 50 * POWER(1024, 3))",
    )
    return f"""
SELECT
    QUERY_ID,
    START_TIME,
    USER_NAME,
    WAREHOUSE_NAME,
    WAREHOUSE_SIZE,
    QUERY_TYPE,
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3) AS SPILL_REMOTE_GB,
    BYTES_SCANNED / POWER(1024, 3) AS GB_SCANNED,
    ROUND(PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0) * 100, 1) AS SCAN_PCT,
    ROUND(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0) * 100, 1) AS CACHE_PCT,
    TOTAL_ELAPSED_TIME / 1000.0 AS ELAPSED_SEC,
    CASE
        WHEN COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0 THEN 'Remote spill'
        WHEN PARTITIONS_TOTAL >= 100
             AND PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0) > 0.8 THEN 'Poor pruning'
        ELSE 'Large cold scan'
    END AS REASON,
    LEFT(QUERY_TEXT, 180) AS QUERY_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
ORDER BY
    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) DESC,
    PARTITIONS_SCANNED / NULLIF(PARTITIONS_TOTAL, 0) DESC NULLS LAST,
    BYTES_SCANNED DESC
LIMIT {limit}
"""


def proc_sla_rollup(days: int, company: str = "ALL", warehouse_contains: str = "",
                    user_contains: str = "", database: str = "",
                    schema_contains: str = "", limit: int = 50, *,
                    bounds: tuple | None = None) -> str:
    """Per-stored-procedure runtime rollup, ranked by SLA impact (calls x p95).

    Rolls up QUERY_TYPE='CALL' rows into calls / avg / p95 / max / total-minutes
    per proc. Latency percentiles are computed over SUCCESSFUL calls only — a proc
    that starts failing fast must not read as 'faster' — and the fail rate is
    surfaced separately. Proc identity is REGEXP_SUBSTR'd from QUERY_TEXT (there is
    no proc-name column) and grouped with DB+SCHEMA to partially disambiguate
    same-named procs, mirroring insights_sql.procedure_costs_usd. App-issued CALLs
    (QUERY_TAG 'OVERWATCH%') are dropped as self-noise; the task-issued proc runs a
    DBA cares about are kept. Ranked by frequency x duration so the procs that
    dominate the CALL workload surface first. ~6h ACCOUNT_USAGE latency applies.
    """
    days = bounded_days(days)
    limit = max(5, min(int(limit or 50), 200))
    where = and_where(
        _query_scope(days, company, warehouse_contains, user_contains, database, schema_contains,
                     bounds=bounds),
        "QUERY_TYPE = 'CALL'",
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
    )
    return f"""
WITH calls AS (
    SELECT
        REGEXP_SUBSTR(UPPER(QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME,
        DATABASE_NAME,
        SCHEMA_NAME,
        EXECUTION_STATUS,
        IFF(EXECUTION_STATUS = 'SUCCESS', TOTAL_ELAPSED_TIME, NULL) AS OK_MS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where}
)
SELECT
    PROC_NAME,
    DATABASE_NAME,
    SCHEMA_NAME,
    COUNT(*) AS CALLS,
    ROUND(100 * COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') / COUNT(*), 1) AS FAIL_PCT,
    ROUND(AVG(OK_MS) / 1000, 1) AS AVG_S,
    ROUND(APPROX_PERCENTILE(OK_MS, 0.95) / 1000, 1) AS P95_S,
    ROUND(MAX(OK_MS) / 1000, 1) AS MAX_S,
    ROUND(SUM(COALESCE(OK_MS, 0)) / 60000, 1) AS TOTAL_MIN
FROM calls
WHERE PROC_NAME IS NOT NULL
GROUP BY 1, 2, 3
-- A 100%-failing proc has every OK_MS NULL, so P95_S is NULL and CALLS*P95 collapses to 0,
-- sinking the fully-broken proc below the LIMIT -- exactly the row this panel's FAIL_PCT column
-- exists to surface. Rank those first, then by P95 SLA-impact (bug-hunt 2026-08-30).
ORDER BY CASE WHEN FAIL_PCT = 100 THEN 1 ELSE 0 END DESC,
         CALLS * COALESCE(P95_S, 0) DESC
LIMIT {limit}
"""


def proc_regression(days: int, company: str = "ALL", warehouse_contains: str = "",
                    user_contains: str = "", database: str = "",
                    schema_contains: str = "", min_calls: int = 5, *,
                    bounds: tuple | None = None) -> str:
    """Stored procedures whose runtime crept up vs the prior equal-length window.

    Emits, per proc, this window's vs the previous window's success-only p95/avg
    and the percent change, so a proc that got slower surfaces even if it is cheap.
    Both windows are scoped by ONE _query_scope call (over 2x the days) so current
    and prior are partitioned identically; a proc must clear ``min_calls`` SUCCESSFUL
    calls in BOTH windows before it is compared (a p95 over a handful of successes is
    one slow run, not a trend — and a now-fully-failing proc has no latency signal,
    so it is left to proc_sla_rollup's FAIL_PCT rather than shown as a false regression).
    The percent deltas are computed from the UNROUNDED millisecond percentiles (the
    *_S columns are display-only) so a sub-second proc is not distorted by 0.1s
    rounding. Latency is success-only, and the fail-rate delta is surfaced so 'faster
    because it now errors out' is not mistaken for an improvement. The current window
    is the trailing ``days`` days including today so far; the prior window is exactly
    ``days`` full days before it. Ranked by p95 growth, worst first. ~6h ACCOUNT_USAGE
    latency applies to the current window's most recent hours.
    """
    days = bounded_days(days)
    min_calls = max(1, min(int(min_calls or 5), 10000))
    # Vs-prior: for 'Last month' (bounds) CURRENT is that calendar month and PRIOR is
    # the month before it — scan [prior_start, cur_end) and split the CUR/PRIOR IFF on
    # cur_start. Trailing keeps the 2*days scan split on the -days CURRENT_DATE anchor.
    if bounds is not None:
        cur_start, cur_end = bounds
        prior_start = (cur_start.replace(year=cur_start.year - 1, month=12)
                       if cur_start.month == 1
                       else cur_start.replace(month=cur_start.month - 1))
        outer_bounds = (prior_start, cur_end)
        cur_from = f"START_TIME >= '{cur_start.isoformat()}'"
    else:
        outer_bounds = None
        cur_from = f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())"
    where = and_where(
        _query_scope(2 * days, company, warehouse_contains, user_contains, database,
                     schema_contains, bounds=outer_bounds),
        "QUERY_TYPE = 'CALL'",
        "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'",
    )
    return f"""
WITH calls AS (
    SELECT
        REGEXP_SUBSTR(UPPER(QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1) AS PROC_NAME,
        DATABASE_NAME,
        SCHEMA_NAME,
        EXECUTION_STATUS,
        IFF({cur_from}, 'CUR', 'PRIOR') AS WIN,
        IFF(EXECUTION_STATUS = 'SUCCESS', TOTAL_ELAPSED_TIME, NULL) AS OK_MS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where}
),
agg AS (
    SELECT
        PROC_NAME,
        DATABASE_NAME,
        SCHEMA_NAME,
        COUNT_IF(WIN = 'CUR') AS CUR_CALLS,
        COUNT_IF(WIN = 'PRIOR') AS PRIOR_CALLS,
        -- Unrounded ms percentiles/means drive the delta; the *_S columns below
        -- are display-only. Rounding to 0.1s before the ratio would null a fast
        -- proc's prior p95 to 0.0 and hide a real regression.
        APPROX_PERCENTILE(IFF(WIN = 'CUR', OK_MS, NULL), 0.95) AS CUR_P95_MS,
        APPROX_PERCENTILE(IFF(WIN = 'PRIOR', OK_MS, NULL), 0.95) AS PRIOR_P95_MS,
        AVG(IFF(WIN = 'CUR', OK_MS, NULL)) AS CUR_AVG_MS,
        AVG(IFF(WIN = 'PRIOR', OK_MS, NULL)) AS PRIOR_AVG_MS,
        ROUND(100 * COUNT_IF(WIN = 'CUR' AND EXECUTION_STATUS <> 'SUCCESS')
              / NULLIF(COUNT_IF(WIN = 'CUR'), 0), 1) AS CUR_FAIL_PCT,
        ROUND(100 * COUNT_IF(WIN = 'PRIOR' AND EXECUTION_STATUS <> 'SUCCESS')
              / NULLIF(COUNT_IF(WIN = 'PRIOR'), 0), 1) AS PRIOR_FAIL_PCT
    FROM calls
    WHERE PROC_NAME IS NOT NULL
    GROUP BY 1, 2, 3
    -- Require enough SUCCESSFUL samples in BOTH windows: the p95 the delta is
    -- built on is success-only, so gating on total calls would let a single
    -- successful run (amid failures) drive a bogus regression.
    HAVING COUNT_IF(WIN = 'CUR' AND EXECUTION_STATUS = 'SUCCESS') >= {min_calls}
       AND COUNT_IF(WIN = 'PRIOR' AND EXECUTION_STATUS = 'SUCCESS') >= {min_calls}
)
SELECT
    PROC_NAME,
    DATABASE_NAME,
    SCHEMA_NAME,
    CUR_CALLS,
    PRIOR_CALLS,
    ROUND(CUR_P95_MS / 1000, 1) AS CUR_P95_S,
    ROUND(PRIOR_P95_MS / 1000, 1) AS PRIOR_P95_S,
    ROUND(CUR_AVG_MS / 1000, 1) AS CUR_AVG_S,
    ROUND(PRIOR_AVG_MS / 1000, 1) AS PRIOR_AVG_S,
    ROUND((CUR_P95_MS - PRIOR_P95_MS) / NULLIF(PRIOR_P95_MS, 0) * 100, 1) AS P95_DELTA_PCT,
    ROUND((CUR_AVG_MS - PRIOR_AVG_MS) / NULLIF(PRIOR_AVG_MS, 0) * 100, 1) AS AVG_DELTA_PCT,
    CUR_FAIL_PCT,
    PRIOR_FAIL_PCT
FROM agg
-- Rank by the STRONGER of the two regression signals, so the LIMIT keeps both the
-- slower procs AND the High-severity 'faster but failing' class (a proc that now errors
-- out fast has a NEGATIVE p95 delta but a big fail-jump). Ordering by signed P95_DELTA_PCT
-- alone sorted the failing-but-faster procs into the truncated tail and dropped exactly
-- the rows the panel exists to surface -> a false 'Faster but failing: 0' (bug-hunt 2026-08-30).
ORDER BY GREATEST(COALESCE(P95_DELTA_PCT, 0),
                  COALESCE(CUR_FAIL_PCT, 0) - COALESCE(PRIOR_FAIL_PCT, 0)) DESC NULLS LAST
LIMIT 200
"""


def result_cache_daily(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Share of successful queries answered without scanning (result cache /
    metadata answers). A falling line means redundant recomputation.

    Account-wide by design: a zero-scan answer (result cache / metadata) carries
    WAREHOUSE_NAME = NULL, so COMPANY_FOR_WAREHOUSE cannot attribute it to a company.
    A warehouse-company scope would drop the ENTIRE numerator (every zero-scan hit) while
    keeping the company's warehouse-run queries in the denominator, collapsing HIT_PCT to
    ~0 under any company filter. ``company`` is accepted for call-site symmetry but is not
    applied to this intrinsically un-scopable metric (bug-hunt 2026-08-30)."""
    days = bounded_days(days)
    scope = (scope_window_where("START_TIME", days, bounds=bounds) if bounds is not None
             else _query_scope(days, "ALL", "", "", "", ""))   # account-wide only -- see docstring
    return f"""
SELECT
    DATE_TRUNC('day', START_TIME) AS DAY,
    COUNT(*) AS QUERIES,
    COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND COALESCE(BYTES_SCANNED, 0) = 0) AS ZERO_SCAN,
    -- "share of SUCCESSFUL queries served zero-scan" (docstring): denominator is
    -- successful queries, not all queries — else a spike in failures dilutes the
    -- hit rate even though cache behavior did not change.
    ROUND(COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND COALESCE(BYTES_SCANNED, 0) = 0)
          / NULLIF(COUNT_IF(EXECUTION_STATUS = 'SUCCESS'), 0) * 100, 1) AS HIT_PCT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {scope}
GROUP BY 1
ORDER BY 1
"""


def warehouse_concurrency_peaks(days: int, company: str = "ALL") -> str:
    """Peak running/queued load per warehouse — right-size multi-cluster
    BEFORE queuing hurts, not after."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    WAREHOUSE_NAME,
    ROUND(MAX(AVG_RUNNING), 1) AS PEAK_RUNNING,
    ROUND(MAX(AVG_QUEUED_LOAD), 1) AS PEAK_QUEUED,
    COUNT_IF(AVG_QUEUED_LOAD > 0.5) AS QUEUED_INTERVALS,
    COUNT(*) AS INTERVALS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY
WHERE {where}
GROUP BY 1
ORDER BY PEAK_QUEUED DESC, PEAK_RUNNING DESC
LIMIT 100
"""


def copy_load_failures(days: int, company: str = "ALL") -> str:
    """Failed / partial COPY and Snowpipe file loads by target table."""
    days = bounded_days(days)
    where = and_where(
        f"LAST_LOAD_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "STATUS IN ('Load failed', 'Partially loaded')",
        companies.database_clause(company, "TABLE_CATALOG_NAME"),
    )
    return f"""
SELECT
    TABLE_CATALOG_NAME AS DATABASE_NAME,
    TABLE_SCHEMA_NAME AS SCHEMA_NAME,
    TABLE_NAME,
    MAX(PIPE_NAME) AS PIPE_NAME,
    COUNT(*) AS FAILED_FILES,
    MAX(LAST_LOAD_TIME) AS LAST_FAILURE,
    LEFT(MAX(FIRST_ERROR_MESSAGE), 300) AS SAMPLE_ERROR,
    'FAILED' AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY FAILED_FILES DESC
LIMIT 100
"""


def dynamic_table_health(days: int) -> str:
    """Refresh outcomes per dynamic table; failures mean downstream tables
    are silently serving stale data."""
    days = bounded_days(days, 14)
    return f"""
SELECT
    DATABASE_NAME, SCHEMA_NAME, NAME,
    COUNT(*) AS REFRESHES,
    -- UPSTREAM_FAILED (skipped because an upstream base/DT failed) and CANCELLED are
    -- also non-success terminal states: the dynamic table did NOT refresh and is serving
    -- stale data, exactly what this panel warns about — counting only 'FAILED' gave a
    -- false all-clear for a table stale via an upstream break (bug-hunt 2026-08-30).
    COUNT_IF(STATE IN ('FAILED', 'UPSTREAM_FAILED', 'CANCELLED')) AS FAILURES,
    MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE,
    MAX(REFRESH_END_TIME) AS LAST_REFRESH,
    IFF(COUNT_IF(STATE IN ('FAILED', 'UPSTREAM_FAILED', 'CANCELLED')) > 0, 'FAILED', 'SUCCEEDED') AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
WHERE REFRESH_END_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY FAILURES DESC, LAST_REFRESH DESC
LIMIT 200
"""


def show_streams_sql() -> str:
    """SHOW-based (no ACCOUNT_USAGE view exists for stream staleness).
    LIMIT keeps the runtime row-cap rewrite from touching a SHOW command."""
    return "SHOW STREAMS IN ACCOUNT LIMIT 200"


def running_queries(warehouse: str) -> str:
    """Live in-flight statements (INFORMATION_SCHEMA function — real time,
    unlike the lagged ACCOUNT_USAGE view). Feeds the kill-switch panel.

    BY_WAREHOUSE variant (live error 2026-07-11, code 090234): the no-arg
    QUERY_HISTORY() defaults to CURRENT-USER scoping, and owner's-rights
    procedures — which is what Streamlit-in-Snowflake runs as — cannot
    access current-user information. Per-warehouse also matches the
    kill-switch flow: you already know which warehouse is burning."""
    from app.core.sqlsafe import sql_literal
    wh = str(warehouse or "").strip()
    if not wh:
        raise ValueError("running_queries needs a warehouse name")
    return f"""
SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, EXECUTION_STATUS,
       START_TIME, ROUND(TOTAL_ELAPSED_TIME / 1000, 1) AS ELAPSED_S,
       LEFT(QUERY_TEXT, 90) AS QUERY_PREVIEW
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE(
    WAREHOUSE_NAME => {sql_literal(wh.upper())}, RESULT_LIMIT => 1000))
WHERE EXECUTION_STATUS IN ('RUNNING', 'QUEUED', 'RESUMING_WAREHOUSE', 'BLOCKED')
ORDER BY START_TIME
LIMIT 100
"""


def _task_graph_snapshot_ctes(root_task_id: str = "", graph_version: int | None = None) -> str:
    """Current tasks joined to one coherent version per root task graph."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    root_clause = f"WHERE v.ROOT_TASK_ID = {sql_literal(root)}" if root else ""
    version_clause = ""
    if graph_version is not None:
        version = int(graph_version)
        if version < 0:
            raise ValueError("graph_version must be non-negative")
        version_clause = f"\n      AND v.GRAPH_VERSION = {version}"
        if not root_clause:
            root_clause = "WHERE 1 = 1"
    return f"""
WITH current_tasks AS (
    SELECT TO_VARCHAR(ID) AS TASK_ID, STATE, WAREHOUSE AS WAREHOUSE_NAME, SCHEDULE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASKS
    WHERE DELETED IS NULL
),
versions AS (
    SELECT TO_VARCHAR(COALESCE(v.ROOT_TASK_ID, v.ID)) AS ROOT_TASK_ID,
           TO_VARCHAR(v.ID) AS TASK_ID,
           v.GRAPH_VERSION, v.GRAPH_VERSION_CREATED_ON,
           v.DATABASE_NAME, v.SCHEMA_NAME, v.NAME,
           v.DATABASE_NAME || '.' || v.SCHEMA_NAME || '.' || v.NAME AS TASK_FQN,
           v.PREDECESSORS, t.STATE, t.WAREHOUSE_NAME, t.SCHEDULE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS v
    INNER JOIN current_tasks t ON t.TASK_ID = TO_VARCHAR(v.ID)
    INNER JOIN current_tasks r
        ON r.TASK_ID = TO_VARCHAR(COALESCE(v.ROOT_TASK_ID, v.ID))
),
graph_versions AS (
    SELECT DISTINCT v.ROOT_TASK_ID, v.GRAPH_VERSION, v.GRAPH_VERSION_CREATED_ON
    FROM versions v
    {root_clause}{version_clause}
),
latest_graphs AS (
    SELECT ROOT_TASK_ID, GRAPH_VERSION, GRAPH_VERSION_CREATED_ON
    FROM graph_versions
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ROOT_TASK_ID
        ORDER BY GRAPH_VERSION_CREATED_ON DESC, GRAPH_VERSION DESC
    ) = 1
),
snapshot AS (
    SELECT v.*
    FROM versions v
    INNER JOIN latest_graphs g
        ON g.ROOT_TASK_ID = v.ROOT_TASK_ID
       AND g.GRAPH_VERSION = v.GRAPH_VERSION
)
"""


def _recent_fails_cte(name: str) -> str:
    """Day-grain failure counts per task, from MART_TASK_NODE_DAILY (V058).

    The rec34 original summed live ACCOUNT_USAGE.TASK_HISTORY over a rolling
    24h — the only ACCOUNT_USAGE scan in these formerly metadata-only queries,
    and its secure-view expansion dominated the ~21s first paint. The mart
    carries the same FAILED counts at DAY grain, so the window is today +
    yesterday (the daily loader's reach) rather than rolling 24h. An empty or
    lagging mart degrades the sort to node-count order through the existing
    LEFT JOIN + COALESCE — never an error.
    """
    return f""",
{name} AS (
    SELECT DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TASK_NAME AS TASK_FQN,
           SUM(FAILED) AS RECENT_FAILURES
    FROM {core_object('MART_TASK_NODE_DAILY')}
    WHERE DAY >= DATEADD('day', -1, CURRENT_DATE())
      AND FAILED > 0
    GROUP BY 1
)"""


def task_graph_roots() -> str:
    """Latest coherent graph snapshot per active root, for the graph picker.

    rec34: ordered FAILURES-FIRST so the graph a DBA needs to open sits on
    top, not buried alphabetically/by size under healthy trees. RECENT_FAILURES
    sums the task mart's day-grain failures across the graph's tasks; the JOIN
    is 1:0-or-1 per TASK_FQN so it never inflates NODE_COUNT. With the 500-row
    fetch cap the survivors are the most-failing then largest — a failing tree
    is never truncated away in favour of a big healthy one.
    """
    return _task_graph_snapshot_ctes() + _recent_fails_cte("root_fails") + """
SELECT s.ROOT_TASK_ID, s.GRAPH_VERSION, MAX(s.GRAPH_VERSION_CREATED_ON) AS GRAPH_CREATED_ON,
       COALESCE(
           MAX(IFF(s.TASK_ID = s.ROOT_TASK_ID, s.TASK_FQN, NULL)),
           MAX(IFF(COALESCE(ARRAY_SIZE(s.PREDECESSORS), 0) = 0, s.TASK_FQN, NULL))
       ) AS ROOT_TASK_FQN,
       COUNT(*) AS NODE_COUNT,
       COUNT_IF(LOWER(COALESCE(s.STATE, '')) LIKE '%suspend%') AS SUSPENDED_NODES,
       COALESCE(SUM(rf.RECENT_FAILURES), 0) AS RECENT_FAILURES
FROM snapshot s
LEFT JOIN root_fails rf ON rf.TASK_FQN = s.TASK_FQN
GROUP BY s.ROOT_TASK_ID, s.GRAPH_VERSION
ORDER BY RECENT_FAILURES DESC, NODE_COUNT DESC, ROOT_TASK_FQN
"""


def task_graph_nodes(root_task_id: str = "", graph_version: int | None = None) -> str:
    """One complete task-graph snapshot with current state and recent failures.

    The old reader chose the newest row independently per task and silently
    stopped at 300 rows. That could combine graph versions or draw dangling
    edges. This reader keys the snapshot by ``ROOT_TASK_ID + GRAPH_VERSION``;
    the UI applies an explicit fetch cap and refuses a truncated frame.
    """
    return _task_graph_snapshot_ctes(root_task_id, graph_version) + _recent_fails_cte("fails") + """
SELECT s.ROOT_TASK_ID, s.GRAPH_VERSION, s.GRAPH_VERSION_CREATED_ON,
       s.TASK_ID, s.TASK_FQN, s.PREDECESSORS, s.STATE,
       s.WAREHOUSE_NAME, s.SCHEDULE,
       COALESCE(f.RECENT_FAILURES, 0) AS RECENT_FAILURES,
       COUNT(*) OVER () AS SNAPSHOT_NODE_COUNT
FROM snapshot s
LEFT JOIN fails f ON f.TASK_FQN = s.TASK_FQN
ORDER BY s.TASK_FQN
"""


def task_graph_recent_runs(root_task_id: str = "0", days: int = 7,
                           limit: int = 40) -> str:
    """Recent graph executions for one root, bounded for TASK_HISTORY pruning."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    if not root:
        raise ValueError("root_task_id is required")
    horizon = max(1, min(int(days or 7), 14))
    cap = max(1, min(int(limit or 40), 100))
    return f"""
WITH attempts AS (
    SELECT
        GRAPH_RUN_GROUP_ID,
        QUERY_ID,
        SCHEDULED_TIME,
        QUERY_START_TIME,
        COMPLETED_TIME,
        STATE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE SCHEDULED_TIME >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND TO_VARCHAR(ROOT_TASK_ID) = {sql_literal(root, 80)}
      AND GRAPH_RUN_GROUP_ID IS NOT NULL
    -- Auto-retries emit multiple rows sharing one SCHEDULED_TIME within a graph run;
    -- collapse each scheduled task to its terminal attempt so TASKS/FAILED_TASKS count
    -- tasks, not attempts (mirrors task_runs / task_recent_states).
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY GRAPH_RUN_GROUP_ID, DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT COALESCE(TO_VARCHAR(GRAPH_RUN_GROUP_ID), QUERY_ID) AS RUN_KEY,
       MIN(SCHEDULED_TIME) AS SCHEDULED_AT,
       MIN(QUERY_START_TIME) AS STARTED_AT,
       MAX(COMPLETED_TIME) AS COMPLETED_AT,
       COUNT(*) AS TASKS,
       COUNT_IF(STATE = 'FAILED') AS FAILED_TASKS,
       ROUND(SUM(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                                   QUERY_START_TIME), 0)) / 1000, 2) AS QUEUE_SEC,
       -- MAX per-task queue = the run's actual dispatch latency (the worst-delayed task).
       -- QUEUE_SEC above SUMS every task's queue; in a fan-out graph independent branches
       -- queue concurrently, so that sum grows with task count and is NOT comparable to the
       -- single WALL_SEC span -- it over-fires a dispatch-delay warn on healthy wide graphs
       -- and can exceed wall. The MAX is run-level and wall-comparable (bug-hunt 2026-08-30).
       ROUND(MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                                   QUERY_START_TIME), 0)) / 1000, 2) AS MAX_QUEUE_SEC,
       DATEDIFF('second', MIN(SCHEDULED_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC
FROM attempts
GROUP BY COALESCE(TO_VARCHAR(GRAPH_RUN_GROUP_ID), QUERY_ID)
ORDER BY SCHEDULED_AT DESC
LIMIT {cap}
"""


def task_graph_run_nodes(root_task_id: str = "0", run_key: str = "0",
                         days: int = 7) -> str:
    """Node timing and profile evidence for one selected graph execution."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    run = str(run_key or "").strip()
    if not root or not run:
        raise ValueError("root_task_id and run_key are required")
    horizon = max(1, min(int(days or 7), 14))
    return f"""
SELECT DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS TASK_FQN,
       QUERY_ID, STATE, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
       ROUND(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                               QUERY_START_TIME), 0) / 1000, 2) AS QUEUE_SEC,
       ROUND(GREATEST(DATEDIFF('millisecond', QUERY_START_TIME,
                               COMPLETED_TIME), 0) / 1000, 2) AS EXEC_SEC,
       ROUND(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME,
                               COMPLETED_TIME), 0) / 1000, 2) AS RUN_SEC,
       COALESCE(ERROR_CODE::VARCHAR, '') AS ERROR_CODE,
       LEFT(COALESCE(ERROR_MESSAGE, ''), 500) AS ERROR_MESSAGE
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE SCHEDULED_TIME >= DATEADD('day', -{horizon}, CURRENT_DATE())
  AND TO_VARCHAR(ROOT_TASK_ID) = {sql_literal(root, 80)}
  AND TO_VARCHAR(GRAPH_RUN_GROUP_ID) = {sql_literal(run, 100)}
-- Auto-retries emit multiple rows sharing one SCHEDULED_TIME within a graph run;
-- collapse each scheduled task to its terminal attempt so STATE reflects the final
-- outcome, not a phantom FAILED attempt (mirrors task_graph_recent_runs / task_runs).
-- GRAPH_RUN_GROUP_ID is already pinned by the WHERE, so the partition omits it.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
    ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
ORDER BY SCHEDULED_TIME, TASK_FQN
LIMIT 2000
"""


def task_graph_versions(root_task_id: str = "0", limit: int = 30) -> str:
    """Historical coherent graph versions for a selected root."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    if not root:
        raise ValueError("root_task_id is required")
    cap = max(2, min(int(limit or 30), 100))
    return f"""
SELECT GRAPH_VERSION, MAX(GRAPH_VERSION_CREATED_ON) AS CREATED_AT,
       COUNT(*) AS NODE_COUNT,
       SUM(COALESCE(ARRAY_SIZE(PREDECESSORS), 0)) AS DEPENDENCY_COUNT
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
WHERE TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) = {sql_literal(root, 80)}
GROUP BY GRAPH_VERSION
ORDER BY CREATED_AT DESC, GRAPH_VERSION DESC
LIMIT {cap}
"""


def task_graph_version_nodes(root_task_id: str = "0",
                             graph_version: int = 0) -> str:
    """Historical topology for one root/version, including deleted nodes."""
    from app.core.sqlsafe import sql_literal

    root = str(root_task_id or "").strip()
    version = int(graph_version)
    if not root:
        raise ValueError("root_task_id is required")
    if version < 0:
        raise ValueError("graph_version must be non-negative")
    return f"""
SELECT TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) AS ROOT_TASK_ID,
       GRAPH_VERSION, GRAPH_VERSION_CREATED_ON,
       TO_VARCHAR(ID) AS TASK_ID,
       DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS TASK_FQN,
       PREDECESSORS
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
WHERE TO_VARCHAR(COALESCE(ROOT_TASK_ID, ID)) = {sql_literal(root, 80)}
  AND GRAPH_VERSION = {version}
ORDER BY TASK_FQN
LIMIT 2000
"""


def volume_deltas() -> str:
    """Yesterday's rows-added vs prior-7d average per moving table — the panel behind
    the PIPE_VOLUME_DROP alert. That alert is LIVE: SP_ANOMALY_SWEEP (TASK_ANOMALY_SWEEP,
    06:40 CT daily) raises a HIGH event when a PROD table's rows-added collapses past a 50%
    drop vs its prior-7-day average, gated on ALERT_CONFIG.ENABLED (so it can be turned off).
    This panel is the account-wide informational view (all databases + the 30-50% WATCH
    band), so it intentionally shows rows that do not page."""
    return """
SELECT DB AS DATABASE_NAME, SCH AS SCHEMA_NAME, TBL AS TABLE_NAME,
       Y_ROWS, ROUND(AVG_ROWS, 0) AS AVG_ROWS_PRIOR_7D, DAYS_ACTIVE_7D,
       ROUND((1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100, 1) AS DROP_PCT,
       CASE WHEN Y_ROWS = 0 AND SAME_DOW_ROWS = 0 THEN 'NORMAL'
            WHEN (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 > 50 THEN 'FAILED'
            WHEN (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 > 30 THEN 'WATCH'
            ELSE 'NORMAL' END AS STATUS
FROM (
    SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
           SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()), d.ROWS_ADDED, 0)) AS Y_ROWS,
           -- Same weekday last week (7 days before yesterday). If it also added 0 rows, the table
           -- does not load on this weekday (weekday-only / business-days pipeline), so yesterday's 0
           -- is its normal pattern, not a drop -- suppress the false FAILED. A genuine stop on a
           -- normally-active weekday leaves this non-zero and still fires (bug-hunt 2026-08-30).
           SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -8, CURRENT_DATE()), d.ROWS_ADDED, 0)) AS SAME_DOW_ROWS,
           SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()), d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS,
           COUNT(DISTINCT IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE())
                              AND d.ROWS_ADDED > 0, DATE(d.START_TIME), NULL)) AS DAYS_ACTIVE_7D
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
    WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
      AND d.START_TIME < CURRENT_DATE()
      -- Exclude transient / per-run staging tables: dated (name_YYYYMMDD_...)
      -- load tables, staging schemas (delimited *_STAG / *_STG suffix so we don't
      -- catch POSTGRES etc.), and the SNOWFLAKE internal database. These
      -- truncate-reload or write once, so 'yesterday = 0 rows' is their normal
      -- pattern, not a real loss (the owner's DB_T_PROD_STAG case). Ephemeral
      -- temp tables are already excluded by the steady-baseline gate below.
      AND NOT REGEXP_LIKE(d.TABLE_NAME, '.*_[0-9]{8}(_[0-9]+)+', 'i')
      AND UPPER(d.SCHEMA_NAME) NOT LIKE '%!_STAG' ESCAPE '!'
      AND UPPER(d.SCHEMA_NAME) NOT LIKE '%!_STG' ESCAPE '!'
      AND UPPER(d.SCHEMA_NAME) NOT LIKE '%!_STAGING' ESCAPE '!'
      AND UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'
    GROUP BY 1, 2, 3
    -- A real drop needs a STEADY baseline: >=1,000 rows/day on average AND rows
    -- written on >=3 of the prior 7 days. A one-shot or newly-dropped table
    -- cannot establish a baseline and is filtered out.
    HAVING AVG_ROWS >= 1000 AND DAYS_ACTIVE_7D >= 3
)
ORDER BY DROP_PCT DESC
LIMIT 50
"""

def warehouse_blast_radius(warehouse: str, days: int = 7) -> str:
    """Who an operator action on this warehouse would affect: per-user share
    of the last N days' work, with role and query-tag evidence (tags reveal
    scheduled tools/tasks the user grain hides)."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    wh = safe_identifier(str(warehouse or "").strip())
    days = bounded_days(days, 30)
    return f"""
SELECT
    USER_NAME,
    ANY_VALUE(ROLE_NAME)                            AS ROLE_NAME,
    COUNT(*)                                        AS QUERIES,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0   AS ELAPSED_SEC,
    MAX(START_TIME)                                 AS LAST_SEEN,
    ANY_VALUE(NULLIF(QUERY_TAG, ''))                AS SAMPLE_TAG,
    -- Window totals over the FULL pre-LIMIT result (evaluated before ORDER BY..LIMIT):
    -- the blast-radius headline must count EVERY affected user/query, not just the top 25
    -- shown in the table. TOTAL_USERS = distinct users (one row per group here);
    -- TOTAL_QUERIES = every user's queries, incl. the light service/automation accounts
    -- that sort last by elapsed and would otherwise be dropped by LIMIT 25.
    COUNT(*) OVER ()                                AS TOTAL_USERS,
    SUM(COUNT(*)) OVER ()                           AS TOTAL_QUERIES
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND UPPER(WAREHOUSE_NAME) = {sql_literal(wh.upper())}
GROUP BY USER_NAME
ORDER BY ELAPSED_SEC DESC
LIMIT 25
"""


def show_resource_monitors_sql() -> str:
    """Resource monitors with quota/used/state (repo review wave 2: the
    coverage map needs which monitors exist; SHOW WAREHOUSES carries the
    per-warehouse linkage). LIMIT keeps the row-cap rewrite off SHOW."""
    return "SHOW RESOURCE MONITORS LIMIT 200"
