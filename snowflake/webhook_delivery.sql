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

-- ---------------------------------------------------------------------------
-- Severity-based multi-channel routing (the sender already walks
-- ALERT_ROUTES per family/severity through NAMED integrations — these are
-- copy-paste recipes, not new capability):
--
-- CRITICAL -> PagerDuty (wakes someone up). PagerDuty Events API v2 accepts
-- a webhook whose body carries the message; the integration's body template
-- does the wrapping:
-- CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_PD_KEY
--     TYPE = GENERIC_STRING SECRET_STRING = '<pagerduty-integration-key>';
-- CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_PAGERDUTY
--     TYPE = WEBHOOK ENABLED = TRUE
--     WEBHOOK_URL = 'https://events.pagerduty.com/v2/enqueue'
--     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_PD_KEY
--     WEBHOOK_BODY_TEMPLATE = '{"routing_key": "SNOWFLAKE_WEBHOOK_SECRET", "event_action": "trigger", "payload": {"summary": "SNOWFLAKE_WEBHOOK_MESSAGE", "source": "OVERWATCH", "severity": "critical"}}'
--     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
-- INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
-- SELECT 'ALL', 'CRITICAL', 'OVERWATCH_WEBHOOK_PAGERDUTY';
--
-- HIGH -> a finops/team Slack channel (seen in the morning):
-- CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_FINOPS
--     TYPE = WEBHOOK ENABLED = TRUE
--     WEBHOOK_URL = 'https://hooks.slack.com/services/T000/B000/YYYY'
--     WEBHOOK_SECRET = <a secret holding that URL>
--     WEBHOOK_BODY_TEMPLATE = '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'
--     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
-- INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
-- SELECT 'COST', 'HIGH', 'OVERWATCH_WEBHOOK_FINOPS';
--
-- Routes are additive: an event can match several and each send is isolated
-- — one bad channel never blocks the others. Disable a route by flipping
-- ENABLED, no deploy needed.

ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
