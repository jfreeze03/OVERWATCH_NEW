-- Webhook delivery setup with Power Automate URL for Teams notifications
-- Co-authored with CoCo
-- webhook_delivery.sql — ONE-TIME integration setup (the only step that
-- cannot ship in git: it holds your webhook secret).
--
-- Everything else is in the numbered chain already:
--   V012: SP_NOTIFY_WEBHOOK (route-aware sender) + ALERT_ROUTES
--   V018: TASK_ALERT_NOTIFY chained AFTER the scan + guarded auto-resume
--         + morning-digest delivery through the same route
--   V164: actionable lines ('[SEV] title | company | detail | event <id>') + a one-time
--         CRITICAL escalation (SETTINGS ESCALATE_AFTER_MIN / ESCALATE_EMAIL_INTEGRATION):
--         re-posted to the route(s) that delivered it, emailed via OVERWATCH_EMAIL's
--         DEFAULT_RECIPIENTS (recipe at the end of this file)
--
-- Run as ACCOUNTADMIN in a Snowsight worksheet. First-time setup: uncomment the
-- CREATE SECRET below (paste the value there) and open the GATE, then re-run
-- V018 (or just: ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;).
-- A rotated URL needs only the ROTATION step further down -- not a re-run of
-- this file.


-- ---------------------------------------------------------------------------
-- MICROSOFT TEAMS (Workflows / Power Automate) — live lesson 2026-07-08.
-- The retired O365 "incoming webhook" connectors accepted {"text": ...};
-- Teams WORKFLOWS URLs (prod-XX.*.logic.azure.com/...) do NOT — the flow's
-- "Send each adaptive card" action rejects it (the "text card" error).
-- Setup: Teams channel -> Workflows -> "Post to a channel when a webhook
-- request is received", copy the HTTP URL, then:
-- PASTE YOUR TEAMS WORKFLOWS HTTP URL IN SNOWSIGHT ONLY.
-- <REDACTED-PASTE-IN-SNOWSIGHT>  (NEVER PASTE THE REAL URL INTO THIS FILE —
-- it lands in git + git history; keep it in the SECRET object in Snowsight.)
--
-- FIRST-TIME SETUP ONLY (the secret does not exist yet). In the Snowsight
-- worksheet, uncomment this statement and paste the value there: everything
-- after /workflows/ in that URL (the flow id + ?api-version=...&sig=...).
--   CREATE SECRET IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
--       TYPE = GENERIC_STRING
--       SECRET_STRING = '<REDACTED-PASTE-IN-SNOWSIGHT>';
-- Once it exists, change the value ONLY with the ROTATION RUNBOOK's ALTER
-- SECRET below (IF NOT EXISTS never overwrites the live secret).
-- Keep the value in a TOP-LEVEL CREATE / ALTER SECRET. NEVER put it in a
-- scripting block (a DECLARE ... DEFAULT) or a SET variable: QUERY_HISTORY
-- keeps that statement's whole text verbatim -- 365 days in ACCOUNT_USAGE,
-- readable by every role with ACCOUNT_USAGE access -- and OVERWATCH ingests
-- QUERY_TEXT itself (OW_QH_EXTRACT -> change-risk QUERY_PREVIEW, Query
-- detail). If the real value was EVER run that way (an older revision of this
-- file used SET variables for it), treat the sig as exposed: regenerate the
-- Workflows URL in Teams, then rotate (ROTATION RUNBOOK below).
--
-- GATE: Run All stops HERE unless you set recreate_integration TRUE in your
-- Snowsight copy -- first-time setup, or the URL PREFIX (before /workflows/)
-- or the card template changed. The CREATE OR REPLACE below drops every grant
-- on the integration (SP_NOTIFY_WEBHOOK runs as owner), and a rotation never
-- needs it. The gate holds no secret, so its text is safe in QUERY_HISTORY.
EXECUTE IMMEDIATE $$
DECLARE
    recreate_integration BOOLEAN DEFAULT FALSE;
    not_confirmed EXCEPTION (-20001, 'ABORT: webhook_delivery.sql stops before recreating OVERWATCH_WEBHOOK_TEAMS (CREATE OR REPLACE drops every grant on it). A rotated URL needs only the ROTATION RUNBOOK step (ALTER SECRET), not this file. First-time setup or a URL prefix / card template change: create the secret first (the commented CREATE SECRET IF NOT EXISTS), set recreate_integration TRUE in your Snowsight copy, then re-run.');
BEGIN
    IF (NOT recreate_integration) THEN
        RAISE not_confirmed;
    END IF;
    RETURN 'gate open: recreating OVERWATCH_WEBHOOK_TEAMS';
END;
$$;
 CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_TEAMS
     TYPE = WEBHOOK ENABLED = TRUE
     WEBHOOK_URL = 'https://default22d2e650b7a647b5af0ef9719fea2b.b8.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/SNOWFLAKE_WEBHOOK_SECRET'
     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
     WEBHOOK_BODY_TEMPLATE = '{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{"$schema":"http://adaptivecards.io/schemas/adaptive-card.json","type":"AdaptiveCard","version":"1.4","body":[{"type":"TextBlock","text":"SNOWFLAKE_WEBHOOK_MESSAGE","wrap":true}]}}]}'
     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
-- Idempotent: ROUTE_ID is a UUID default and Snowflake does not enforce the key,
-- so a bare INSERT minted a SECOND enabled route on every re-run -- and
-- SP_NOTIFY_WEBHOOK / SP_DAILY_DIGEST deliver per ROUTE_ID, so every alert,
-- digest and escalation then posted twice (plus a backlog burst on the new
-- route). Keyed on the integration alone, NOT on ENABLED: a route you disabled
-- on purpose is never re-added.
 INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
 SELECT 'ALL', 'HIGH', 'OVERWATCH_WEBHOOK_TEAMS'
 WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
                   WHERE INTEGRATION_NAME = 'OVERWATCH_WEBHOOK_TEAMS');

-- V026's sender JSON-escapes the message (quotes, newlines, tabs), so
-- multi-alert digests render as line breaks in the card instead of
-- breaking the flow. Workflows replies 202 Accepted on success.

-- ---------------------------------------------------------------------------
-- ROTATION RUNBOOK -- the Teams URL was regenerated, or deliveries fail with
-- webhook/HTTP errors (alert_pipeline_check.sql STEP 4 / FIX C). Change ONLY
-- the secret, in a Snowsight worksheet (paste the new value there, never here):
--   ALTER SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
--       SET SECRET_STRING = '<everything after /workflows/ in the new URL>';
-- The integration, its grants and ALERT_ROUTES stay as they are. Re-run the
-- setup above (GATE opened) only when the URL PREFIX (before /workflows/) or
-- the card template changes -- and then check the grants it dropped:
--   SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS;
-- Prove delivery end to end (posts one real card):
--   CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
--     SNOWFLAKE.NOTIFICATION.TEXT_PLAIN('OVERWATCH rotation test ' || CURRENT_TIMESTAMP()),
--     SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_WEBHOOK_TEAMS'));
-- Every card arriving TWICE = a duplicate route left by an older, unguarded
-- re-run of this file. Keep the oldest enabled row, disable the others:
--   SELECT ROUTE_ID, FAMILY, MIN_SEVERITY, COMPANY_FILTER, ENABLED, CREATED_AT
--     FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
--    WHERE INTEGRATION_NAME = 'OVERWATCH_WEBHOOK_TEAMS' ORDER BY CREATED_AT;
--   UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE
--    WHERE ROUTE_ID = '<a newer duplicate ROUTE_ID>';

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

-- ---------------------------------------------------------------------------
-- V164 CRITICAL escalation email (optional; Teams re-post works without it).
-- SP_NOTIFY_WEBHOOK emails an unacknowledged CRITICAL through the integration
-- named in SETTINGS ESCALATE_EMAIL_INTEGRATION (default OVERWATCH_EMAIL, the
-- email integration of docs/EMAIL_RECIPIENT_RUNBOOK.md). It sends to that
-- integration's DEFAULT_RECIPIENTS -- no address lives in OVERWATCH. Set it
-- ONCE in Snowsight (normally the same list as ALLOWED_RECIPIENTS), replacing
-- the placeholder there, never here -- an address in this file lands in git:
-- ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET
--     DEFAULT_RECIPIENTS = ('<recipient>')
--     DEFAULT_SUBJECT = 'OVERWATCH escalation';
-- No email leg: Admin > Settings, ESCALATE_EMAIL_INTEGRATION blank.
-- No escalation at all: Admin > Settings, ESCALATE_AFTER_MIN 0.

ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
