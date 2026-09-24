-- native_alert_templates.sql — OPTIONAL Snowflake ALERT objects for
-- server-side email delivery. Keep notification-only; remediation stays
-- human-approved. Requires a NOTIFICATION INTEGRATION named OVERWATCH_EMAIL
-- and recipients verified in Snowflake.
--
-- Deliberately NOT part of numbered migrations: delivery is an opt-in that
-- needs the integration + recipient approval first. The recipient below is a
-- PLACEHOLDER in all FOUR SYSTEM$SEND_EMAIL calls — replace it locally, never commit
-- a real address (the app renders this file to every viewer).
--
-- Next-Fifty #4: besides new-event mail, three OUT-OF-BAND dead-man watchers email when
-- OVERWATCH itself goes quiet — stale telemetry or a loader failure, a lost alert-scan /
-- notify heartbeat, and failing Teams/webhook delivery. They do not depend on the
-- in-app notifier, so they still fire when that path is the thing that broke.
--
-- PREREQS (run as ACCOUNTADMIN):
-- CREATE NOTIFICATION INTEGRATION IF NOT EXISTS OVERWATCH_EMAIL
--     TYPE = EMAIL ENABLED = TRUE
--     ALLOWED_RECIPIENTS = ('dba-team@example.com');
-- GRANT USAGE ON INTEGRATION OVERWATCH_EMAIL TO ROLE SNOW_ACCOUNTADMINS;
-- GRANT EXECUTE ALERT ON ACCOUNT TO ROLE SNOW_ACCOUNTADMINS;
--
-- Run THIS file as SNOW_ACCOUNTADMINS (the app owner role) so Alerts > Native delivery can
-- read the alerts' state/history; if an alert already exists under another owner, DROP it
-- as that owner first. Re-running CREATE OR REPLACE leaves every alert SUSPENDED — resume
-- all four at the bottom. PRE-FLIGHT: run the pre-flight SELECTs in
-- docs/EMAIL_RECIPIENT_RUNBOOK.md first; any row they return will email hourly until fixed.
--
-- The action blocks use Snowflake Scripting: Snowsight runs them as written; in SnowSQL wrap
-- each THEN block in EXECUTE IMMEDIATE $$ ... $$. If an account rejects
-- RESULT_SCAN(SNOWFLAKE.ALERT.GET_CONDITION_QUERY_UUID()) in the action (ALERT_HISTORY shows
-- ACTION_FAILED and the in-app 'Email path' row goes red), replace that THEN block with the
-- static CALL SYSTEM$SEND_EMAIL(...) form used by NEW_EVENTS.

-- 1) New critical/high OVERWATCH alert events -> email within 30 minutes.
CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = '30 MINUTE'
IF (EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE STATUS = 'OPEN'
      AND SEVERITY IN ('CRITICAL', 'HIGH')
      AND RAISED_AT > COALESCE(SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME(), DATEADD('hour', -1, CURRENT_TIMESTAMP()))
))
THEN
    CALL SYSTEM$SEND_EMAIL(
        'OVERWATCH_EMAIL',
        'dba-team@example.com',
        'OVERWATCH: new critical/high alerts',
        'New OPEN critical/high alert events were raised. Open the OVERWATCH Alerts page to triage.'
    );

-- 2) Dead-man #1: STALE telemetry OR a LOADER FAILURE -> email. Reads the loader-owned
--    SOURCE_FRESHNESS_STATE (every fact/mart row) with the app's cadence rule (DAILY/METERING
--    past 30h, else 3h; app/config.py THRESHOLDS, locked by tests/test_native_alert_templates.py),
--    OR any loader-failure row logged since the last evaluation (gated on
--    LAST_SUCCESSFUL_SCHEDULED_TIME so one failure emails once). Staleness re-emails hourly.
--    Scheduled at :10 Central, inside the hourly chain's warm window (TASK_LOAD_HOURLY fires at
--    :07), so it rides the already-running warehouse instead of paying an extra resume.
CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 10 * * * * America/Chicago'
IF (EXISTS (
    SELECT f.SOURCE_NAME || ': ' ||
           IFF(f.LAST_LOAD_TS IS NULL, 'never loaded',
               TO_VARCHAR(ROUND(DATEDIFF('minute', f.LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0, 1))
               || 'h since load (limit ' || TO_VARCHAR(f.LIM_H) || 'h)') AS DETAIL
    FROM (SELECT SOURCE_NAME, LAST_LOAD_TS,
                 IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIM_H
          FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE) f
    WHERE f.LAST_LOAD_TS IS NULL
       OR DATEDIFF('minute', f.LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0 > f.LIM_H
    UNION ALL
    SELECT 'loader failure ' || ERROR_TYPE || ' [' || COALESCE(PAGE, '?') || '] at '
           || TO_VARCHAR(LOGGED_AT, 'YYYY-MM-DD HH24:MI') || ': ' || LEFT(COALESCE(CONTEXT, ''), 120)
           || ' - ' || LEFT(COALESCE(ERROR_MESSAGE, ''), 240)
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                         'cloud_svc_mart_failed', 'object_cost_load_failed')
      AND LOGGED_AT >  COALESCE(SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME(),
                                DATEADD('hour', -1, SNOWFLAKE.ALERT.SCHEDULED_TIME()))::TIMESTAMP_NTZ
      AND LOGGED_AT <= SNOWFLAKE.ALERT.SCHEDULED_TIME()::TIMESTAMP_NTZ
))
THEN
    DECLARE
        body VARCHAR;
    BEGIN
        SELECT 'OVERWATCH self-health (out-of-band dead-man):\n'
               || LEFT(LISTAGG(DETAIL, '\n') WITHIN GROUP (ORDER BY DETAIL), 6000)
               || '\n\nRun snowflake/loader_chain_check.sql; Admin > Migrations & freshness names the likeliest cause.'
          INTO :body
          FROM TABLE(RESULT_SCAN(SNOWFLAKE.ALERT.GET_CONDITION_QUERY_UUID()));
        CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', 'dba-team@example.com',
                               'OVERWATCH: telemetry stale or a loader failed', :body);
    END;

-- 3) Dead-man #2: the in-app alert SCAN / NOTIFY heartbeat. INFORMATION_SCHEMA.TASK_HISTORY has
--    no ACCOUNT_USAGE lag. TASK_ALERT_NOTIFY is only expected when a delivery route is enabled.
CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_SCAN_HEARTBEAT
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 10 * * * * America/Chicago'
IF (EXISTS (
    SELECT t.TASK_NAME || ': no SUCCEEDED run in the last 3h (last state '
           || COALESCE(h.LAST_STATE, 'none recorded')
           || COALESCE(' at ' || TO_VARCHAR(h.LAST_AT, 'YYYY-MM-DD HH24:MI'), '')
           || COALESCE(' - ' || LEFT(h.LAST_ERROR, 240), '') || ')' AS DETAIL
    FROM (SELECT 'TASK_ALERT_SCAN' AS TASK_NAME
          UNION ALL
          -- notify only runs when a delivery route exists (V018 leaves it suspended otherwise)
          SELECT DISTINCT 'TASK_ALERT_NOTIFY' FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES WHERE ENABLED) t
    LEFT JOIN (
        SELECT NAME,
               COUNT_IF(STATE = 'SUCCEEDED') AS OK_N,
               MAX_BY(STATE, COALESCE(COMPLETED_TIME, SCHEDULED_TIME)) AS LAST_STATE,
               MAX(COALESCE(COMPLETED_TIME, SCHEDULED_TIME))::TIMESTAMP_NTZ AS LAST_AT,
               MAX_BY(ERROR_MESSAGE, IFF(ERROR_MESSAGE IS NOT NULL, COMPLETED_TIME, NULL)) AS LAST_ERROR
        FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
                 SCHEDULED_TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()),
                 RESULT_LIMIT => 10000))
        WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
          AND NAME IN ('TASK_ALERT_SCAN', 'TASK_ALERT_NOTIFY') AND STATE <> 'SCHEDULED'
        GROUP BY NAME
    ) h ON h.NAME = t.TASK_NAME
    WHERE COALESCE(h.OK_N, 0) = 0
))
THEN
    DECLARE body VARCHAR;
    BEGIN
        SELECT 'OVERWATCH alerting heartbeat LOST -- no in-app alerts are being raised or delivered:\n'
               || LISTAGG(DETAIL, '\n') WITHIN GROUP (ORDER BY DETAIL)
               || '\n\nSHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH; a root auto-suspends after 10 consecutive failures (V071) -- fix, then ALTER TASK ... RESUME.'
          INTO :body FROM TABLE(RESULT_SCAN(SNOWFLAKE.ALERT.GET_CONDITION_QUERY_UUID()));
        CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', 'dba-team@example.com',
                               'OVERWATCH: alert scan/notify heartbeat lost', :body);
    END;

-- 4) Dead-man #3: Teams/webhook DELIVERY failing — a sender failure logged since the last
--    evaluation, or a CRITICAL that crossed 60 minutes OPEN with no delivery on any route.
CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_DELIVERY_FAILING
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 10 * * * * America/Chicago'
IF (EXISTS (
    SELECT ERROR_TYPE || ' at ' || TO_VARCHAR(LOGGED_AT, 'YYYY-MM-DD HH24:MI') || ': '
           || LEFT(COALESCE(CONTEXT, ''), 160) || ' - ' || LEFT(COALESCE(ERROR_MESSAGE, ''), 240) AS DETAIL
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE PAGE = 'NotifyWebhook'
      AND ERROR_TYPE IN ('route_send_failed', 'undelivered_expired', 'webhook_run_failed')
      AND LOGGED_AT >  COALESCE(SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME(),
                                DATEADD('hour', -1, SNOWFLAKE.ALERT.SCHEDULED_TIME()))::TIMESTAMP_NTZ
      AND LOGGED_AT <= SNOWFLAKE.ALERT.SCHEDULED_TIME()::TIMESTAMP_NTZ
    UNION ALL
    -- a CRITICAL that crossed 60 minutes OPEN since the last evaluation with NO delivery row at all
    SELECT 'CRITICAL ' || e.EVENT_ID || ' (' || e.RULE_ID || ') raised '
           || TO_VARCHAR(e.RAISED_AT, 'YYYY-MM-DD HH24:MI') || ' is OPEN > 60 min with no delivery on any route'
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS = 'OPEN' AND UPPER(e.SEVERITY) = 'CRITICAL'
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r WHERE r.ENABLED)
      AND e.RAISED_AT <= DATEADD('minute', -60, SNOWFLAKE.ALERT.SCHEDULED_TIME())::TIMESTAMP_NTZ
      AND e.RAISED_AT >  DATEADD('minute', -60, COALESCE(SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME(),
                                 DATEADD('hour', -1, SNOWFLAKE.ALERT.SCHEDULED_TIME())))::TIMESTAMP_NTZ
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d WHERE d.EVENT_ID = e.EVENT_ID)
))
THEN
    DECLARE body VARCHAR;
    BEGIN
        SELECT 'OVERWATCH Teams/webhook delivery is failing (this email is the independent channel):\n'
               || LEFT(LISTAGG(DETAIL, '\n') WITHIN GROUP (ORDER BY DETAIL), 6000)
               || '\n\nAlerts > Native delivery shows the failing route; check the notification integration.'
          INTO :body FROM TABLE(RESULT_SCAN(SNOWFLAKE.ALERT.GET_CONDITION_QUERY_UUID()));
        CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', 'dba-team@example.com',
                               'OVERWATCH: alert delivery failing', :body);
    END;

-- Alerts are created suspended by default — resume all four after the pre-flight is clean:
-- ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS RESUME;
-- ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS RESUME;
-- ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_SCAN_HEARTBEAT RESUME;
-- ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_DELIVERY_FAILING RESUME;
-- EXECUTE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_SCAN_HEARTBEAT;  -- optional immediate smoke
