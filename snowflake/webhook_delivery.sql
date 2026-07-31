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

-- ---------------------------------------------------------------------------
-- SLACK (OVERWATCH_WEBHOOK) — NOT USED. Owner 2026-07-31: "we only use
-- OVERWATCH_TEAMS_URL and OVERWATCH_WEBHOOK_TEAMS." Commented out because the
-- URL below is a PLACEHOLDER: creating this integration would wire a route to
-- an endpoint that can only fail. Kept as the reference recipe.
--
-- CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
--     TYPE = GENERIC_STRING
--     SECRET_STRING = '<slack-webhook-url>';
--
-- CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK
--     TYPE = WEBHOOK ENABLED = TRUE
--     WEBHOOK_URL = '<slack-webhook-url>'
--     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_WEBHOOK_URL
--     WEBHOOK_BODY_TEMPLATE = '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'
--     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
--
-- THREE THINGS STILL NAME 'OVERWATCH_WEBHOOK' ON A TEAMS-ONLY INSTALL:
--  1. V012 seeds a DEFAULT route ('ALL','HIGH','OVERWATCH_WEBHOOK') into
--     ALERT_ROUTES (V012__routing_anomaly_remediation.sql:29). On this account it
--     points at an integration that does not exist, so every SP_NOTIFY_WEBHOOK run
--     logs a route_send_failed for it. The live sender isolates per route, so it
--     does NOT block Teams — but it buries the real Teams error in the log. Disable:
--        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
--           SET ENABLED = FALSE WHERE INTEGRATION_NAME = 'OVERWATCH_WEBHOOK';
--  2. SP_DAILY_DIGEST hardcodes INTEGRATION('OVERWATCH_WEBHOOK')
--     (V018__delivery_first_class.sql:104) and swallows the error with
--     `WHEN OTHER THEN NULL` -> the morning digest has NEVER been deliverable here.
--     Do not read "no digest" as a signal. Fix belongs in a future migration.
--  3. V018's auto-resume gate only RESUMEs TASK_ALERT_NOTIFY when
--     OVERWATCH_WEBHOOK exists; this file's trailing RESUME is what covers that.

-- ---------------------------------------------------------------------------
-- MICROSOFT TEAMS (Workflows / Power Automate) — live lesson 2026-07-08.
-- The retired O365 "incoming webhook" connectors accepted {"text": ...};
-- Teams WORKFLOWS URLs (prod-XX.*.logic.azure.com/...) do NOT — the flow's
-- "Send each adaptive card" action rejects it (the "text card" error).
-- Setup: Teams channel -> Workflows -> "Post to a channel when a webhook
-- request is received", copy the HTTP URL.
--
-- !! NEVER PASTE THE REAL URL INTO THIS FILE !!
-- It is TRACKED IN GIT. A live Power Automate trigger URL carries its own bearer
-- credential in the `sig=` query parameter — anyone who can read the repo can post
-- into the channel. A real one WAS committed here (through 2026-07-31) and had to be
-- rotated; tests/test_no_committed_secrets.py now fails the build if one returns.
-- Keep the placeholders below and run the two statements from a Snowsight worksheet
-- with the real values pasted there, where nothing is version-controlled.
--
-- The trigger URL splits into two parts:
--   WEBHOOK_URL    = everything up to /workflows/, then the literal SNOWFLAKE_WEBHOOK_SECRET
--   SECRET_STRING  = everything after /workflows/  (the flow id + ?api-version...&sig=...)
-- NOTE the cluster segment: current Power Automate URLs include /direct/cu/NN/workflows/.
-- If yours has it, WEBHOOK_URL must keep it too, or every send 404s.
 CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
    TYPE = GENERIC_STRING
     SECRET_STRING = '<flow-id>/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=<REDACTED-PASTE-IN-SNOWSIGHT>';
 CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_TEAMS
     TYPE = WEBHOOK ENABLED = TRUE
     WEBHOOK_URL = 'https://<your-env>.environment.api.powerplatform.com/powerautomate/automations/direct/cu/<NN>/workflows/SNOWFLAKE_WEBHOOK_SECRET'
     WEBHOOK_SECRET = DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
     WEBHOOK_BODY_TEMPLATE = '{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{"$schema":"http://adaptivecards.io/schemas/adaptive-card.json","type":"AdaptiveCard","version":"1.4","body":[{"type":"TextBlock","text":"SNOWFLAKE_WEBHOOK_MESSAGE","wrap":true}]}}]}'
     WEBHOOK_HEADERS = ('Content-Type' = 'application/json');
 -- !! RE-RUN HAZARD: this is a bare INSERT, not a MERGE — running this file again
 -- mints a SECOND route row for OVERWATCH_WEBHOOK_TEAMS (duplicate Teams cards).
 -- And CREATE OR REPLACE on the integration above DROPS AND RECREATES it, which
 -- DESTROYS every grant on it — SP_NOTIFY_WEBHOOK runs EXECUTE AS OWNER, so if the
 -- owning role loses USAGE, every send throws "insufficient privileges" and Teams
 -- goes silent with only route_send_failed rows to show for it. After any re-run:
 --   SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS;   -- re-grant USAGE if bare
 -- Use the guarded form instead of the bare INSERT:
 MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES t
 USING (SELECT 'ALL' AS FAMILY, 'HIGH' AS MIN_SEVERITY,
               'OVERWATCH_WEBHOOK_TEAMS' AS INTEGRATION_NAME) s
 ON t.FAMILY = s.FAMILY AND t.INTEGRATION_NAME = s.INTEGRATION_NAME
 WHEN NOT MATCHED THEN INSERT (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
      VALUES (s.FAMILY, s.MIN_SEVERITY, s.INTEGRATION_NAME);

-- V026's sender JSON-escapes the message (quotes, newlines, tabs), so
-- multi-alert digests render as line breaks in the card instead of
-- breaking the flow. Workflows replies 202 Accepted on success.

-- ---------------------------------------------------------------------------
-- ROTATION RUNBOOK (do this in Snowsight; nothing here is version-controlled)
--
-- WHY: a live trigger URL was committed to this repo through 2026-07-31. Deleting
-- it from the file does NOT undo that — it is in git history and was pushed. The
-- only real remedy is to invalidate the old URL at the provider.
--
-- 1. Power Automate -> your flow -> the "When a Teams webhook request is received"
--    trigger. Regenerate the URL (or delete + recreate the trigger). The moment the
--    new URL exists, the leaked one is dead. Copy the new URL.
-- 2. In a Snowsight worksheet, split it as described above and run:
--       CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL
--           TYPE = GENERIC_STRING SECRET_STRING = '<everything after /workflows/>';
--       CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_TEAMS ...
--           (the form above, with your real WEBHOOK_URL prefix)
-- 3. CREATE OR REPLACE on an integration DESTROYS ITS GRANTS. Re-check and re-grant:
--       SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS;
--       -- GRANT USAGE ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS TO ROLE <owner-role>;
--    SP_NOTIFY_WEBHOOK runs EXECUTE AS OWNER; without USAGE every send throws
--    "insufficient privileges" and Teams goes silent with only route_send_failed rows.
-- 4. Prove it end to end (sends one real card):
--       CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
--         SNOWFLAKE.NOTIFICATION.TEXT_PLAIN('OVERWATCH rotation test ' || CURRENT_TIMESTAMP()),
--         SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_WEBHOOK_TEAMS'));
--
-- ---------------------------------------------------------------------------
-- RETIRE THE DEAD SLACK ROUTE (one statement, safe, reversible)
--
-- V012 seeds a default route to 'OVERWATCH_WEBHOOK'. On a Teams-only account that
-- integration does not exist, so every SP_NOTIFY_WEBHOOK run attempts a send that can
-- only fail. Per-route isolation means it does NOT block Teams — but it writes a
-- route_send_failed row every run, which buries real delivery errors in the log.
--       UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
--          SET ENABLED = FALSE
--        WHERE INTEGRATION_NAME = 'OVERWATCH_WEBHOOK';
-- Verify (expect the Teams route ENABLED, the Slack one not):
--       SELECT ROUTE_ID, FAMILY, MIN_SEVERITY, INTEGRATION_NAME, ENABLED
--         FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES ORDER BY ENABLED DESC;
-- Reversible: set ENABLED = TRUE to restore. The Alerts page's delivery banner now
-- flags any enabled route naming an integration that does not exist.

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
