-- =====================================================================================
--  PROBES_NEXT_FIFTY_WAVE1.sql  --  OWNER-RUN, Snowsight, as SNOW_ACCOUNTADMINS
--  Staged for the Next-Fifty wave 1 (app v4.588.0 / v4.589.0). SEPARATE from RUN_NEXT.sql on
--  purpose: RUN_NEXT still stages the pending V148 -> V149 -> V150 apply, which this file does
--  not touch and does not depend on. Run it any time, before or after V148-V150.
--
--  Everything here is READ-ONLY except P2, which CREATEs a scratch procedure, CALLs it and
--  DROPs it again (a faithful proxy for the owner's-rights Streamlit app). No numbered
--  migration, no change to any OVERWATCH object. Paste back the grids for P1-P4.
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- -------------------------------------------------------------------------------------
-- P1 (#4) Email dead-man pre-flight. Rows from (1) and (2) would email HOURLY once the new
--    alerts in snowflake/native_alert_templates.sql are resumed -- they must be 0 rows first.
-- -------------------------------------------------------------------------------------
-- (1) sources that would email now
SELECT SOURCE_NAME, LAST_LOAD_TS, ROUND(DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP())/60.0,1) AS HOURS_BEHIND, IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIMIT_H FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE LAST_LOAD_TS IS NULL OR DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP())/60.0 > IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) ORDER BY 3 DESC;
-- (2) chronic loader / delivery failure types (24h)
SELECT ERROR_TYPE, PAGE, COUNT(*) AS N_24H, MAX(LOGGED_AT) AS LAST_AT, ANY_VALUE(LEFT(ERROR_MESSAGE,160)) AS SAMPLE FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG WHERE LOGGED_AT >= DATEADD('hour',-24,CURRENT_TIMESTAMP()) AND (ERROR_TYPE IN ('mart_load_failed','fact_load_failed','extract_load_failed','cloud_svc_mart_failed','object_cost_load_failed') OR PAGE='NotifyWebhook') GROUP BY 1,2 ORDER BY 3 DESC;
-- (3) heartbeat ground truth
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, LEFT(ERROR_MESSAGE,160) FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(SCHEDULED_TIME_RANGE_START=>DATEADD('hour',-3,CURRENT_TIMESTAMP()), RESULT_LIMIT=>10000)) WHERE SCHEMA_NAME='OVERWATCH' AND NAME IN ('TASK_ALERT_SCAN','TASK_ALERT_NOTIFY') ORDER BY SCHEDULED_TIME DESC;
-- (4) email objects + visibility
SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;
SHOW GRANTS ON INTEGRATION OVERWATCH_EMAIL;
-- (5) NOTIFICATION_HISTORY columns / STATUS values the in-app 'Email path' row assumes
SELECT * FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(START_TIME_RANGE_START=>DATEADD('day',-14,CURRENT_TIMESTAMP()), INTEGRATION_NAME=>'OVERWATCH_EMAIL', RESULT_LIMIT=>20));

-- -------------------------------------------------------------------------------------
-- P2 (#7) Per-statement tagging probe for owner's-rights SiS (creates + drops a scratch proc).
--    Decision: params_tag row shows QUERY_TAG 'OVERWATCH|probe=params' -> statement_params
--    transport; else TEXT_TAIL must show '/* OVERWATCH_APP|probe=trail */' -> comment marker.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_OW_TAG_PROBE()
RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'pandas') HANDLER = 'run' EXECUTE AS OWNER
AS
$$
def run(session):
    out = {}
    def attempt(key, fn):
        try:
            out[key] = str(fn())
        except Exception as e:
            out[key] = 'ERROR: ' + str(e)[:300]
    attempt('params_tag', lambda: session.sql("SELECT 'OW_PROBE_PARAMS' AS P").collect(statement_params={'QUERY_TAG': 'OVERWATCH|probe=params'})[0][0])
    attempt('params_pandas', lambda: len(session.sql("SELECT 'OW_PROBE_PANDAS' AS P").to_pandas(statement_params={'QUERY_TAG': 'OVERWATCH|probe=pandas'})))
    attempt('params_timeout', lambda: session.sql("SELECT 'OW_PROBE_TIMEOUT', SYSTEM$WAIT(5) AS W").collect(statement_params={'STATEMENT_TIMEOUT_IN_SECONDS': '2'})[0][0])
    attempt('lead_comment', lambda: session.sql("/* OVERWATCH_APP|probe=lead */ SELECT 'OW_PROBE_LEAD' AS P").collect()[0][0])
    attempt('trail_comment', lambda: session.sql("SELECT 'OW_PROBE_TRAIL' AS P\n/* OVERWATCH_APP|probe=trail */").collect()[0][0])
    attempt('trail_show', lambda: len(session.sql("SHOW TASKS LIKE 'TASK_ALERT_SCAN' IN SCHEMA DBA_MAINT_DB.OVERWATCH\n/* OVERWATCH_APP|probe=show */").collect()))
    return out
$$;
CALL DBA_MAINT_DB.OVERWATCH.SP_OW_TAG_PROBE();
SELECT START_TIME, QUERY_TYPE, EXECUTION_STATUS, QUERY_TAG, LEFT(QUERY_TEXT, 70) AS TEXT_HEAD,
       RIGHT(QUERY_TEXT, 45) AS TEXT_TAIL, LEFT(ERROR_MESSAGE, 120) AS ERR
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.QUERY_HISTORY(
       END_TIME_RANGE_START => DATEADD('minute', -30, CURRENT_TIMESTAMP()), RESULT_LIMIT => 2000))
WHERE (QUERY_TEXT ILIKE '%OW_PROBE_%' OR QUERY_TEXT ILIKE '%probe=show%')
  AND QUERY_TYPE IN ('SELECT', 'SHOW')
  AND QUERY_TEXT NOT ILIKE '%INFORMATION_SCHEMA.QUERY_HISTORY%'
ORDER BY START_TIME;
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_OW_TAG_PROBE();
-- If the readback is empty (child statements hidden), re-run it >=45 min later against
-- SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY with the same WHERE and START_TIME >= today.

-- -------------------------------------------------------------------------------------
-- P3 (#9) Identity auth readiness: do USERS.TYPE / HAS_RSA_PUBLIC_KEY and USER-level
--    network-policy references resolve on this account?
-- -------------------------------------------------------------------------------------
-- 

-- -------------------------------------------------------------------------------------
-- P4 (#25) V146 STEP-2: did the Functions purge + reload run? (remediation stays commented)
-- -------------------------------------------------------------------------------------
-- =====================================================================
--  PART C -- V146 STEP-2 settle-the-question VERIFY (READ-ONLY: no DDL/DML).
--  Independent of V148-V150 (reads FACT_AI_USAGE_DAILY, SCHEMA_VERSION and the canonical
--  Cortex view only) -- safe to run before or after Parts A/B. Paste back C1, C2, C3.
--  Day keys are pinned to America/Chicago EXPLICITLY (the loader task runs in the account
--  TZ; a UTC worksheet would otherwise shift every day boundary). No ALTER SESSION needed.
-- =====================================================================

-- (C1) When was V146 applied?
SELECT VERSION, APPLIED_AT, APPLIED_BY
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION IN (145, 146) ORDER BY VERSION;

-- (C2) DECISIVE: were the pre-apply 'Functions' days re-loaded after V146?
--   STALE = 0                  -> STEP-2 purge + reload RAN (done; nothing to do).
--   STALE = FUNCTIONS_ROWS     -> neither ran (run the remediation below).
--   0 < STALE < FUNCTIONS_ROWS -> reload ran WITHOUT the purge: the STALE rows are the
--                                 double-counting orphans (run the remediation below).
WITH v AS (SELECT APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 146)
SELECT COUNT(*)                            AS FUNCTIONS_ROWS_IN_SCOPE,
       COUNT_IF(t.LOAD_TS <  v.APPLIED_AT) AS STALE_ROWS_LOADED_BEFORE_V146,
       COUNT_IF(t.LOAD_TS >= v.APPLIED_AT) AS ROWS_RELOADED_AFTER_V146,
       ROUND(SUM(IFF(t.LOAD_TS < v.APPLIED_AT, t.CREDITS, 0)), 4) AS STALE_CREDITS,
       MIN(t.DAY) AS FIRST_DAY, MAX(t.DAY) AS LAST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t, v
WHERE t.SOURCE = 'Functions'
  AND t.DAY >= '2026-01-05'
  AND t.DAY <  DATEADD('day', -4, v.APPLIED_AT::DATE);   -- older than the daily task's 3-day reach

-- (C3) Per-day fact vs canonical view since the horizon (only days that DIFFER are listed).
--   LIVE_CREDITS is a plain SUM(CREDITS) (no FLATTEN) = the independent answer; LIVE_CREDITS_DEDUPE
--   reproduces the loader's FLATTEN + COALESCE(INDEX,0)=0 and must equal it.
--   One-sided POSITIVE deltas (fact > live) = orphans -> remediate. Adjacent +/- pairs that net
--   to ~0 = a day-key time-zone skew from a backfill run in a UTC session (harmless to totals).
WITH f AS (
    SELECT DAY, SUM(CREDITS) AS FACT_CREDITS, SUM(REQUESTS) AS FACT_REQUESTS,
           COUNT(*) AS FACT_MODEL_ROWS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE = 'Functions' AND DAY >= '2026-01-05'
    GROUP BY DAY
),
l AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', h.START_TIME)::DATE AS DAY,
           SUM(COALESCE(h.CREDITS, 0)) AS LIVE_CREDITS,
           COUNT(*) AS LIVE_REQUESTS,
           COUNT(DISTINCT COALESCE(NULLIF(h.MODEL_NAME, ''), 'n/a')) AS LIVE_MODELS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY h
    WHERE h.START_TIME >= '2026-01-04'::TIMESTAMP_LTZ
    GROUP BY 1
),
ld AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', h.START_TIME)::DATE AS DAY,
           SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN COALESCE(h.CREDITS, 0) ELSE 0 END) AS LIVE_CREDITS_DEDUPE
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY h,
         LATERAL FLATTEN(input => h.METRICS, OUTER => TRUE) m
    WHERE h.START_TIME >= '2026-01-04'::TIMESTAMP_LTZ
    GROUP BY 1
)
SELECT COALESCE(f.DAY, l.DAY) AS DAY,
       ROUND(f.FACT_CREDITS, 6) AS FACT_CREDITS,
       ROUND(l.LIVE_CREDITS, 6) AS LIVE_CREDITS,
       ROUND(ld.LIVE_CREDITS_DEDUPE, 6) AS LIVE_CREDITS_DEDUPE,
       ROUND(COALESCE(f.FACT_CREDITS, 0) - COALESCE(l.LIVE_CREDITS, 0), 6) AS DELTA_CREDITS,
       ROUND(100 * (COALESCE(f.FACT_CREDITS, 0) - COALESCE(l.LIVE_CREDITS, 0))
             / NULLIF(l.LIVE_CREDITS, 0), 2) AS DRIFT_PCT,
       f.FACT_REQUESTS, l.LIVE_REQUESTS, f.FACT_MODEL_ROWS, l.LIVE_MODELS
FROM f
FULL OUTER JOIN l ON l.DAY = f.DAY
LEFT JOIN ld ON ld.DAY = COALESCE(f.DAY, l.DAY)
WHERE COALESCE(f.DAY, l.DAY) >= '2026-01-05'
  AND COALESCE(f.DAY, l.DAY) <  DATEADD('day', -3, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
  AND ABS(COALESCE(f.FACT_CREDITS, 0) - COALESCE(l.LIVE_CREDITS, 0)) > 0.0001
ORDER BY DAY;

-- (C3 summary) whole-horizon totals (TZ-skew nets out here; orphans do not).
SELECT
  (SELECT ROUND(SUM(CREDITS), 4) FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE = 'Functions' AND DAY >= '2026-01-06'
      AND DAY < DATEADD('day', -4, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS FACT_TOTAL,
  (SELECT ROUND(SUM(COALESCE(CREDITS, 0)), 4) FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
    WHERE CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE >= '2026-01-06'
      AND CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE
          < DATEADD('day', -4, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS LIVE_TOTAL;

-- REMEDIATION -- ONLY if C2 shows STALE_ROWS_LOADED_BEFORE_V146 > 0, or C3 shows one-sided
-- positive deltas. Uncomment and run as ONE step in one worksheet session (order matters: the
-- loader is upsert-only, so the scoped purge must precede the reload; the Chicago session TZ makes
-- the backfill key days exactly like the daily task). Then re-run C2 (expect STALE = 0) and C3.
-- ALTER SESSION SET TIMEZONE = 'America/Chicago';
-- DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY WHERE SOURCE = 'Functions' AND DAY >= '2026-01-05';
-- CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
