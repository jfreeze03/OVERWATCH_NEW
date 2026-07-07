-- webhook_delivery.sql — OPTIONAL Slack/Teams delivery for alert events.
-- Opt-in like native_alert_templates.sql: requires an ACCOUNTADMIN-created
-- webhook secret + notification integration before the task is resumed.
-- V007 adds ALERT_EVENTS.NOTIFIED_AT, which this uses for exactly-once sends.

-- 1) One-time setup (ACCOUNTADMIN; paste your webhook URL):
-- CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
--     TYPE = GENERIC_STRING
--     SECRET_STRING = 'https://hooks.slack.com/services/T000/B000/XXXX';
-- CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK
--     TYPE = WEBHOOK ENABLED = TRUE
--     WEBHOOK_URL = 'https://hooks.slack.com/services/T000/B000/XXXX'
--     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
--     WEBHOOK_BODY_TEMPLATE = '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'
--     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
-- For Microsoft Teams use the Teams incoming-webhook URL and template
--     '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'

-- 2) Sender: pushes unnotified OPEN critical/high events, marks NOTIFIED_AT.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    message VARCHAR;
    sent INT DEFAULT 0;
BEGIN
    SELECT LISTAGG('[' || SEVERITY || '] ' || LEFT(TITLE, 140), '\n')
           WITHIN GROUP (ORDER BY RAISED_AT DESC)
      INTO :message
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE STATUS = 'OPEN'
      AND SEVERITY IN ('CRITICAL', 'HIGH')
      AND NOTIFIED_AT IS NULL
      AND RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP());

    IF (:message IS NULL OR :message = '') THEN
        RETURN 'nothing to notify';
    END IF;

    BEGIN
        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                'OVERWATCH alerts:\n' || LEFT(:message, 3000)),
            SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_WEBHOOK'));
    EXCEPTION
        WHEN OTHER THEN
            RETURN 'webhook send failed - is the OVERWATCH_WEBHOOK integration created and enabled?';
    END;

    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET NOTIFIED_AT = CURRENT_TIMESTAMP()
     WHERE STATUS = 'OPEN'
       AND SEVERITY IN ('CRITICAL', 'HIGH')
       AND NOTIFIED_AT IS NULL
       AND RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP());
    sent := SQLROWCOUNT;

    RETURN 'notified ' || :sent || ' event(s)';
END;
$$;

-- 3) Chain after the hourly scan. Created suspended; resume once the
--    integration exists:
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK();

-- ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
