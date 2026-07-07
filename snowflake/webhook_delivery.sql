-- webhook_delivery.sql — ONE-TIME integration setup (the only step that
-- cannot ship in git: it holds your webhook secret).
--
-- Everything else is in the numbered chain already:
--   V012: SP_NOTIFY_WEBHOOK (route-aware sender) + ALERT_ROUTES
--   V018: TASK_ALERT_NOTIFY chained AFTER the scan + guarded auto-resume
--         + morning-digest delivery through the same route
--
-- Run as ACCOUNTADMIN, paste your Slack/Teams webhook URL, then re-run
-- V018 (or just: ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;)

CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
    TYPE = GENERIC_STRING
    SECRET_STRING = 'https://hooks.slack.com/services/T000/B000/XXXX';

CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK
    TYPE = WEBHOOK ENABLED = TRUE
    WEBHOOK_URL = 'https://hooks.slack.com/services/T000/B000/XXXX'
    WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
    WEBHOOK_BODY_TEMPLATE = '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'
    WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
-- Teams: use the Teams incoming-webhook URL; same body template works.

-- Extra channels for ALERT_ROUTES (repeat per channel, then INSERT a route):
-- CREATE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_FINOPS ... ;
-- INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
-- SELECT 'COST', 'MEDIUM', 'OVERWATCH_WEBHOOK_FINOPS';

ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
