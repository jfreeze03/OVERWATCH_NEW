-- native_alert_templates.sql — OPTIONAL Snowflake ALERT objects for
-- server-side email delivery. Keep notification-only; remediation stays
-- human-approved. Requires a NOTIFICATION INTEGRATION named OVERWATCH_EMAIL
-- and recipients verified in Snowflake.
--
-- Deliberately NOT part of numbered migrations: delivery is an opt-in that
-- needs the integration + recipient approval first.

-- Example integration (adjust recipients; run as ACCOUNTADMIN):
-- CREATE NOTIFICATION INTEGRATION IF NOT EXISTS OVERWATCH_EMAIL
--     TYPE = EMAIL ENABLED = TRUE
--     ALLOWED_RECIPIENTS = ('dba-team@example.com');

-- 1) New critical/high OVERWATCH alert events -> email within 30 minutes.
CREATE OR REPLACE ALERT OVERWATCH.CORE.NATIVE_ALERT_NEW_EVENTS
    WAREHOUSE = OVERWATCH_WH
    SCHEDULE = '30 MINUTE'
IF (EXISTS (
    SELECT 1 FROM OVERWATCH.CORE.ALERT_EVENTS
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

-- 2) OVERWATCH self-health: hourly facts stale > 3 hours -> email.
CREATE OR REPLACE ALERT OVERWATCH.CORE.NATIVE_ALERT_STALE_FACTS
    WAREHOUSE = OVERWATCH_WH
    SCHEDULE = '60 MINUTE'
IF (EXISTS (
    SELECT 1 FROM OVERWATCH.MART.MART_SOURCE_FRESHNESS
    WHERE SOURCE_NAME = 'FACT_QUERY_HOURLY' AND HOURS_SINCE_LOAD > 3
))
THEN
    CALL SYSTEM$SEND_EMAIL(
        'OVERWATCH_EMAIL',
        'dba-team@example.com',
        'OVERWATCH: telemetry loads are stale',
        'FACT_QUERY_HOURLY has not loaded for over 3 hours. Check TASK_LOAD_HOURLY and OVERWATCH_WH.'
    );

-- Alerts are created suspended by default:
-- ALTER ALERT OVERWATCH.CORE.NATIVE_ALERT_NEW_EVENTS RESUME;
-- ALTER ALERT OVERWATCH.CORE.NATIVE_ALERT_STALE_FACTS RESUME;
