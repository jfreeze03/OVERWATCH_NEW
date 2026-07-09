"""Readers for OVERWATCH's own Snowflake objects (marts, facts, ops tables).

Object shapes are defined by snowflake/migrations/. These builders read only;
lifecycle INSERT/UPDATE statements are built in the pages that own them.
"""

from __future__ import annotations

from app.config import core_object, mart_object
from app.core.sqlsafe import sql_literal
from app.data.common import and_where, bounded_days


def _company_filter(company: str) -> str:
    value = str(company or "ALL")
    if value.upper() == "ALL":
        return f"COMPANY = {sql_literal('ALL')}"
    return f"COMPANY = {sql_literal(value)}"


def exec_board(company: str, days: int) -> str:
    """First-paint executive board rows for one company scope and window."""
    days = bounded_days(days)
    where = and_where(_company_filter(company), f"WINDOW_DAYS = {days}")
    return f"""
SELECT PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER, REFRESHED_AT
FROM {mart_object("MART_EXEC_BOARD")}
WHERE {where}
ORDER BY PANEL, SORT_ORDER, PERIOD_START
"""


def source_freshness() -> str:
    return f"SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, HOURS_SINCE_LOAD FROM {mart_object('MART_SOURCE_FRESHNESS')} ORDER BY SOURCE_NAME"


def fact_metering_by_service(days: int) -> str:
    """Spend-tab hot path: same output shape as the live metering reader,
    served from the hourly-loaded fact instead of ACCOUNT_USAGE."""
    days = bounded_days(days)
    return f"""
SELECT DAY, SERVICE_TYPE, CREDITS_USED, CREDITS_BILLED, CREDITS_ADJUSTMENT
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY DAY, SERVICE_TYPE
"""


def fact_query_window_summary(days: int, company: str = "ALL", warehouse_contains: str = "",
                              user_contains: str = "", database: str = "") -> str:
    """Ops Queries-tab hot path from FACT_QUERY_HOURLY.

    Counts, failures, queued time and spill are exact sums of the hourly
    fact. P95 is the PEAK hourly-group p95 (a true p95 needs raw rows) —
    the UI labels it as such. No schema dimension in the fact, so callers
    fall back to live when a schema filter is active.
    """
    from app import companies
    from app.core.sqlsafe import contains_filter

    days = bounded_days(days)
    where = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"]
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
    """The app's own slowest statement families on the dedicated warehouse.

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
    ROUND(AVG(BYTES_SCANNED) / POWER(1024, 3), 3) AS AVG_GB_SCANNED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME = {sql_literal(APP_WAREHOUSE)}
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY P95_S DESC
LIMIT 30
"""


def fact_daily_spend(days: int) -> str:
    """Account billed credits per day from the daily fact (adjustment applied)."""
    days = bounded_days(days)
    return f"""
SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY DAY
ORDER BY DAY
"""


def fact_warehouse_daily(days: int, company: str = "ALL") -> str:
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
ORDER BY DAY
"""


def fact_task_daily(days: int, company: str = "ALL", database: str = "") -> str:
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    if str(database or "").strip():
        where.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    return f"""
SELECT DAY, DATABASE_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR
FROM {mart_object("FACT_TASK_DAILY")}
WHERE {and_where(*where)}
ORDER BY FAILED DESC, DAY DESC
"""


def fact_warehouse_window_vs_prior(days: int, company: str = "ALL") -> str:
    """Window-vs-prior warehouse credits from FACT_WAREHOUSE_DAILY.

    Same output contract as cost_sql.warehouse_window_vs_prior but reads the
    hourly-loaded fact instead of scanning WAREHOUSE_METERING_HISTORY live
    (perf pass: Control Room movers). Inherits up-to-an-hour loader lag —
    callers keep the live builder as fallback and label the source.
    """
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{2 * days}, CURRENT_DATE())"]
    if str(company).upper() != "ALL":
        where.append(f"COMPANY = {sql_literal(company)}")
    return f"""
SELECT
    WAREHOUSE_NAME,
    COMPANY,
    ROUND(SUM(IFF(DAY >= DATEADD('day', -{days}, CURRENT_DATE()), CREDITS_TOTAL, 0)), 4) AS CREDITS_CURRENT,
    ROUND(SUM(IFF(DAY < DATEADD('day', -{days}, CURRENT_DATE()), CREDITS_TOTAL, 0)), 4) AS CREDITS_PRIOR
FROM {mart_object("FACT_WAREHOUSE_DAILY")}
WHERE {and_where(*where)}
GROUP BY 1, 2
HAVING CREDITS_CURRENT > 0 OR CREDITS_PRIOR > 0
ORDER BY CREDITS_CURRENT DESC
LIMIT 500
"""


def fact_cloud_services_ratio(days: int, company: str = "ALL") -> str:
    """Cloud-services share per warehouse from FACT_WAREHOUSE_DAILY.

    The fact already stores CREDITS_TOTAL and CREDITS_COMPUTE, so cloud
    services = TOTAL - COMPUTE — same thresholds as the live builder, no
    schema change needed (Codex #6, improved: they assumed a migration).
    Daily grain vs the live builder's hourly precision; identical for the
    windowed ratio this panel shows.
    """
    days = bounded_days(days)
    where = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
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
HAVING SUM(CREDITS_TOTAL) > 0
ORDER BY CLOUD_SVC_PCT DESC
LIMIT 500
"""


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


def alert_event_history(days: int) -> str:
    days = bounded_days(days)
    return f"""
SELECT DATE(RAISED_AT) AS DAY, SEVERITY, COUNT(*) AS EVENTS
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY 1, 2
ORDER BY DAY
"""


def alert_mttr(days: int = 90) -> str:
    """Weekly MTTA/MTTR from alert lifecycle timestamps."""
    days = bounded_days(days)
    return f"""
SELECT
    DATE_TRUNC('week', RAISED_AT)::DATE AS WEEK,
    COUNT(*) AS EVENTS,
    SUM(IFF(ACK_AT IS NOT NULL, 1, 0)) AS ACKED,
    SUM(IFF(RESOLVED_AT IS NOT NULL, 1, 0)) AS RESOLVED,
    ROUND(AVG(DATEDIFF('minute', RAISED_AT, ACK_AT)), 1) AS MTTA_MIN,
    ROUND(AVG(DATEDIFF('minute', RAISED_AT, RESOLVED_AT)), 1) AS MTTR_MIN
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


def action_queue(limit: int = 200) -> str:
    limit = max(1, min(int(limit), 1000))
    return f"""
SELECT ACTION_ID, CREATED_AT, COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, DUE_DATE,
       SOURCE, PROOF_SQL, ESTIMATED_USD, UPDATED_AT
FROM {core_object("ACTION_QUEUE")}
ORDER BY CREATED_AT DESC
LIMIT {limit}
"""


def savings_ledger() -> str:
    return f"""
SELECT ITEM_ID, ACTION_ID, CREATED_AT, DESCRIPTION, STATE, ESTIMATED_USD, VERIFIED_USD,
       VERIFIED_AT, VERIFIED_BY, PROOF_SQL, NOTES
FROM {core_object("SAVINGS_LEDGER")}
ORDER BY CREATED_AT DESC
LIMIT 500
"""


def latest_digest() -> str:
    return f"""
SELECT DIGEST_DATE, MODEL, BODY, CREATED_AT
FROM {core_object("DAILY_DIGEST")}
ORDER BY DIGEST_DATE DESC
LIMIT 1
"""


def savings_verification_runs() -> str:
    return f"""
SELECT V.RUN_AT, V.ITEM_ID, V.WAREHOUSE_NAME, V.BASELINE_EST_USD,
       V.MEASURED_IDLE_USD_30D, V.PROPOSED_VERIFIED_USD, L.STATE
FROM {core_object("SAVINGS_VERIFICATION_RUNS")} V
LEFT JOIN {core_object("SAVINGS_LEDGER")} L ON L.ITEM_ID = V.ITEM_ID
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


def app_self_cost(days: int) -> str:
    """What OVERWATCH itself spends: tagged queries + the app warehouse."""
    days = bounded_days(days, maximum=30)
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    COUNT(*) AS APP_QUERIES,
    SUM(TOTAL_ELAPSED_TIME) / 1000.0 AS ELAPSED_SEC,
    SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)) AS FAILED
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
  AND (QUERY_TAG LIKE 'OVERWATCH%' OR WAREHOUSE_NAME = 'WH_ALFA_OVERWATCH')
GROUP BY 1
ORDER BY DAY
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
    return f"""
SELECT 'OPEN_CRITICAL' AS METRIC, TO_VARCHAR(COUNT(*)) AS VALUE,
       IFF(COUNT(*) > 0, 'BAD', 'OK') AS STATE
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS = 'OPEN' AND SEVERITY = 'CRITICAL'
UNION ALL
SELECT 'STALEST_SOURCE_H', TO_VARCHAR(COALESCE(ROUND(MAX(HOURS_SINCE_LOAD), 1), -1)),
       CASE WHEN MAX(HOURS_SINCE_LOAD) IS NULL THEN 'MUTED'
            WHEN MAX(HOURS_SINCE_LOAD) > 26 THEN 'BAD'
            WHEN MAX(HOURS_SINCE_LOAD) > 3 THEN 'WARN'
            ELSE 'OK' END
FROM {mart_object("MART_SOURCE_FRESHNESS")}
UNION ALL
SELECT 'MTD_CREDITS', TO_VARCHAR(ROUND(COALESCE(SUM(CREDITS_BILLED), 0), 0)), 'INFO'
FROM {mart_object("FACT_METERING_DAILY")}
WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
"""


def incident_timeline(days: int, company: str = "ALL") -> str:
    """One time axis for everything that happened: alerts, task failures,
    DDL. The 'what else happened around then?' view Datadog does well."""
    days = bounded_days(days, 14)
    comp = str(company or "ALL")
    alert_filter = "" if comp.upper() == "ALL" else \
        f"AND COMPANY IN ({sql_literal(comp)}, 'ALL')"
    entity_filter = "" if comp.upper() == "ALL" else (
        "AND IFF(DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA') = " + sql_literal(comp))
    return f"""
SELECT 'ALERT' AS EVENT_TYPE, RAISED_AT::TIMESTAMP_NTZ AS AT, SEVERITY,
       LEFT(TITLE, 120) AS LABEL
FROM {core_object("ALERT_EVENTS")}
WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP()) {alert_filter}
UNION ALL
SELECT 'TASK FAILURE', COMPLETED_TIME::TIMESTAMP_NTZ, 'HIGH',
       LEFT(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME || ': ' ||
            COALESCE(ERROR_MESSAGE, 'failed'), 120)
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE COMPLETED_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND STATE = 'FAILED' {entity_filter}
UNION ALL
SELECT 'DDL CHANGE', START_TIME::TIMESTAMP_NTZ, 'INFO',
       LEFT(USER_NAME || ': ' || QUERY_TEXT, 120)
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND EXECUTION_STATUS = 'SUCCESS'
  AND (QUERY_TYPE ILIKE 'CREATE%' OR QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'DROP%')
  {entity_filter}
ORDER BY AT DESC
LIMIT 400
"""


def fact_daily_activity(days: int) -> str:
    """Daily query volume + failures from the hourly fact (sparkline feed)."""
    days = bounded_days(days, 30)
    return f"""
SELECT DATE_TRUNC('day', HOUR_TS)::DATE AS DAY,
       SUM(QUERY_COUNT) AS QUERIES,
       SUM(FAILED_COUNT) AS FAILS
FROM {mart_object("FACT_QUERY_HOURLY")}
WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
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
    days = bounded_days(days)
    return f"""
SELECT PAGE, COUNT(*) AS VISITS, COUNT(DISTINCT USER_NAME) AS USERS,
       MAX(AT) AS LAST_VISIT
FROM {core_object("APP_USAGE")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY PAGE
ORDER BY VISITS DESC
"""


def contract_exhaustion() -> str:
    """The CIO number: projected contract exhaustion at trailing 30d burn.
    Same math as the COST_CONTRACT_BREACH scan block; n/a until configured."""
    return f"""
SELECT TOTAL, CONSUMED, DAILY_BURN,
       CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
       DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
               CURRENT_DATE()) AS EXHAUST_DATE
FROM (
    SELECT
        (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0)
         FROM {core_object("SETTINGS")}) AS TOTAL,
        (SELECT COALESCE(SUM(CREDITS_BILLED), 0) FROM {mart_object("FACT_METERING_DAILY")}
         WHERE DAY >= COALESCE((SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                                FROM {core_object("SETTINGS")}), CURRENT_DATE())) AS CONSUMED,
        (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / 30 FROM {mart_object("FACT_METERING_DAILY")}
         WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())) AS DAILY_BURN
)
"""


def savings_summary_quarter() -> str:
    """The ROI numerator: VERIFIED savings this quarter (never mixed with
    estimates) plus the open estimated pipeline, labeled separately."""
    return f"""
SELECT
    ROUND(SUM(IFF(STATE = 'VERIFIED'
                  AND VERIFIED_AT >= DATE_TRUNC('quarter', CURRENT_DATE()),
                  COALESCE(VERIFIED_USD, 0), 0)), 2) AS VERIFIED_QTD_USD,
    COUNT_IF(STATE = 'VERIFIED'
             AND VERIFIED_AT >= DATE_TRUNC('quarter', CURRENT_DATE())) AS VERIFIED_ITEMS,
    ROUND(SUM(IFF(STATE = 'ESTIMATED', COALESCE(ESTIMATED_USD, 0), 0)), 2) AS ESTIMATED_OPEN_USD
FROM {core_object("SAVINGS_LEDGER")}
"""


def app_cost_quarter() -> str:
    """The ROI denominator: everything the app + its tasks burned this
    quarter on the dedicated warehouse."""
    return """
SELECT ROUND(COALESCE(SUM(CREDITS_USED), 0), 2) AS APP_CREDITS_QTD
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE WAREHOUSE_NAME = 'WH_ALFA_OVERWATCH'
  AND START_TIME >= DATE_TRUNC('quarter', CURRENT_DATE())
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
    COUNT(*)                                          AS RESOLVED_EVENTS,
    COUNT_IF(RESOLUTION_KIND = 'ACTIONED')            AS ACTIONED,
    COUNT_IF(RESOLUTION_KIND = 'NOISE')               AS NOISE,
    COUNT_IF(RESOLUTION_KIND = 'EXPECTED')            AS EXPECTED,
    COUNT_IF(RESOLUTION_KIND IS NULL)                 AS UNTAGGED,
    ROUND(100 * ACTIONED / NULLIF(ACTIONED + NOISE, 0), 1) AS PRECISION_PCT
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS = 'RESOLVED'
  AND RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY RULE_ID
ORDER BY RESOLVED_EVENTS DESC
LIMIT 100
"""


def mart_vs_live_recon() -> str:
    """Mart totals vs live ACCOUNT_USAGE over the same complete window —
    freshness says the loaders RAN; this says the numbers MATCH.

    Metering compares 28 complete days ending 3 days ago (metering-daily can
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
f_q AS (
    SELECT SUM(QUERY_COUNT) AS V
    FROM {core_object("FACT_QUERY_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -9, CURRENT_DATE())
      AND HOUR_TS <  DATEADD('day', -2, CURRENT_DATE())
),
l_q AS (
    SELECT COUNT(*) AS V
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -9, CURRENT_DATE())
      AND START_TIME <  DATEADD('day', -2, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
)
SELECT 'Billed credits (28d, mart vs metering-daily)' AS CHECK_NAME,
       ROUND(f_met.V, 2) AS FACT_VALUE, ROUND(l_met.V, 2) AS LIVE_VALUE,
       ROUND(100 * (f_met.V - l_met.V) / NULLIF(l_met.V, 0), 2) AS DRIFT_PCT
FROM f_met, l_met
UNION ALL
SELECT 'Query count (7d, mart vs query-history)',
       f_q.V, l_q.V,
       ROUND(100 * (f_q.V - l_q.V) / NULLIF(l_q.V, 0), 2)
FROM f_q, l_q
"""


def fleet_query_stats(days: int = 7) -> str:
    """Slow/failed fetches across ALL viewers (APP_QUERY_TELEMETRY, V021).

    Only rows the app chose to persist land here (>=2s or failed), so this is
    the regression surface, not a complete census — the note on the panel
    says so.
    """
    days = max(1, min(int(days or 7), 90))
    return f"""
SELECT
    PAGE,
    QUERY_KEY,
    COUNT(*)                                   AS SLOW_OR_FAILED,
    COUNT_IF(NOT OK)                           AS FAILURES,
    ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.5))  AS P50_MS,
    ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.95)) AS P95_MS,
    COUNT(DISTINCT ROLE_NAME)                  AS ROLES_AFFECTED,
    MAX(AT)                                    AS NEWEST
FROM {core_object("APP_QUERY_TELEMETRY")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY PAGE, QUERY_KEY
ORDER BY P95_MS DESC NULLS LAST
LIMIT 40
"""

def rule_metric_kinds(days: int = 90) -> str:
    """Raw material for threshold suggestions: each resolved event's metric
    value and its resolution kind (V021). The tuning logic aggregates."""
    days = max(7, min(int(days or 90), 365))
    return f"""
SELECT RULE_ID, METRIC_VALUE, RESOLUTION_KIND
FROM {core_object("ALERT_EVENTS")}
WHERE STATUS = 'RESOLVED'
  AND RESOLUTION_KIND IN ('ACTIONED', 'NOISE')
  AND METRIC_VALUE IS NOT NULL
  AND RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
LIMIT 5000
"""


def score_inputs_daily(days: int = 30) -> str:
    """Per-day signals for the RETRO platform-score trend, from facts +
    alert history. The live score adds stale-source/open-action penalties
    that facts don't carry per day — panel labels the difference."""
    days = max(7, min(int(days or 30), 120))
    return f"""
WITH spend AS (
    SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED
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
SELECT DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, LAST_ERROR
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
    SELECT EVENT_ID, RAISED_AT, SEVERITY, STATUS
    FROM {core_object("ALERT_EVENTS")}
    WHERE RAISED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
)
SELECT
    (SELECT COUNT(*) FROM e) AS EVENTS_RAISED,
    (SELECT COUNT(*) FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS EVENTS_DELIVERED,
    (SELECT ROUND(MEDIAN(DATEDIFF('second', e.RAISED_AT, d.FIRST_SENT)) / 60.0, 1)
       FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS MEDIAN_MIN,
    (SELECT ROUND(APPROX_PERCENTILE(DATEDIFF('second', e.RAISED_AT, d.FIRST_SENT), 0.95) / 60.0, 1)
       FROM e JOIN d ON d.EVENT_ID = e.EVENT_ID) AS P95_MIN,
    (SELECT COUNT(*) FROM e LEFT JOIN d ON d.EVENT_ID = e.EVENT_ID
      WHERE UPPER(e.SEVERITY) = 'CRITICAL' AND e.STATUS = 'OPEN'
        AND d.EVENT_ID IS NULL
        AND e.RAISED_AT <= DATEADD('minute', -30, CURRENT_TIMESTAMP())) AS UNDELIVERED_CRITICALS_30M,
    (SELECT COUNT(*) FROM {core_object("APP_ERROR_LOG")}
      WHERE ERROR_TYPE = 'route_send_failed'
        AND LOGGED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())) AS ROUTE_FAILURES
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
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'ESTIMATED') AS SAVINGS_ESTIMATED,
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'VERIFIED') AS SAVINGS_VERIFIED,
    (SELECT COUNT(*) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'REJECTED') AS SAVINGS_REJECTED,
    (SELECT ROUND(COALESCE(SUM(VERIFIED_USD), 0), 2) FROM {core_object("SAVINGS_LEDGER")}
      WHERE CREATED_AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
        AND STATE = 'VERIFIED') AS VERIFIED_USD
"""


def telemetry_by_page(days: int = 7) -> str:
    """Per-page fetch health from the V027 telemetry rider. Cache-hit % is
    computed over rows where CACHE_HIT is known (pre-V027 rows excluded) —
    and it measures PERSISTED fetches (slow/failed + 2% sample), a floor."""
    days = bounded_days(days, 90)
    return f"""
SELECT PAGE,
       COUNT(*) AS FETCHES,
       ROUND(APPROX_PERCENTILE(ELAPSED_MS, 0.95) / 1000, 2) AS P95_S,
       ROUND(AVG(IFF(CACHE_HIT IS NULL, NULL, IFF(CACHE_HIT, 1, 0))) * 100, 1) AS CACHE_HIT_PCT,
       COUNT_IF(NOT OK) AS FAILED,
       COUNT_IF(ELAPSED_MS >= 2000) AS SLOW_2S,
       ROUND(AVG(COALESCE(BATCH_SIZE, 1)), 1) AS AVG_BATCH,
       COUNT_IF(COALESCE(TRUNCATED, FALSE)) AS TRUNCATED_N
FROM {core_object("APP_QUERY_TELEMETRY")}
WHERE AT >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY PAGE
ORDER BY FETCHES DESC
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
