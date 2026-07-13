-- Webhook delivery setup with Power Automate URL for Teams notifications
-- Co-authored with CoCo
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

-- ---------------------------------------------------------------------------
-- MICROSOFT TEAMS (Workflows / Power Automate) — live lesson 2026-07-08.
-- The retired O365 "incoming webhook" connectors accepted {"text": ...};
-- Teams WORKFLOWS URLs (prod-XX.*.logic.azure.com/...) do NOT — the flow's
-- "Send each adaptive card" action rejects it (the "text card" error).
-- Setup: Teams channel -> Workflows -> "Post to a channel when a webhook
-- request is received", copy the HTTP URL, then:
--https://default22d2e650b7a647b5af0ef9719fea2b.b8.environment.api.powerplatform.com:443/powerautomate/automations/direct/cu/26/workflows/8bc55bec6a6340b7b04bb7b12eb0e7ed/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=jVLNX37r__13fDvdtYaA-lFoJPEsA707FY1wThIrSqs
 CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
    TYPE = GENERIC_STRING
     SECRET_STRING = '8bc55bec6a6340b7b04bb7b12eb0e7ed/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=jVLNX37r__13fDvdtYaA-lFoJPEsA707FY1wThIrSqs';
 CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_TEAMS
     TYPE = WEBHOOK ENABLED = TRUE
     WEBHOOK_URL = 'https://default22d2e650b7a647b5af0ef9719fea2b.b8.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/SNOWFLAKE_WEBHOOK_SECRET'
     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
     WEBHOOK_BODY_TEMPLATE = '{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{"$schema":"http://adaptivecards.io/schemas/adaptive-card.json","type":"AdaptiveCard","version":"1.4","body":[{"type":"TextBlock","text":"SNOWFLAKE_WEBHOOK_MESSAGE","wrap":true}]}}]}'
     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
 INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
 SELECT 'ALL', 'HIGH', 'OVERWATCH_WEBHOOK_TEAMS';

-- V026's sender JSON-escapes the message (quotes, newlines, tabs), so
-- multi-alert digests render as line breaks in the card instead of
-- breaking the flow. Workflows replies 202 Accepted on success.

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
--     WEBHO