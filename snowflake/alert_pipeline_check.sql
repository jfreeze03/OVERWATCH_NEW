-- ============================================================================
-- OVERWATCH alert-pipeline diagnosis (2026-07-12, "no alerts anymore")
-- Run top to bottom in a Snowsight worksheet as ACCOUNTADMIN.
-- The main raisers are two scans feeding one sender:
--   Hourly: TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_ALERT_SCAN -> SP_ALERT_SCAN
--     -> ALERT_EVENTS -> TASK_ALERT_NOTIFY -> SP_NOTIFY_WEBHOOK
--     -> ALERT_ROUTES / notification integration -> Teams card.
--   Daily:  TASK_LOAD_DAILY -> TASK_NIGHTLY_RECONCILE -> TASK_ALERT_SCAN_DAILY
--     -> SP_ALERT_SCAN_DAILY -> ALERT_EVENTS.
-- Standalone scheduled roots (their own cron, in neither chain) raise into the same
-- ALERT_EVENTS: TASK_ANOMALY_SWEEP (SP_ANOMALY_SWEEP), TASK_CANARY_SENTINEL
-- (SP_CANARY_SENTINEL, weekly on Mondays), TASK_WAREHOUSE_CHANGE_SCAN and
-- TASK_CHANGE_IMPACT_SCAN.
-- Read the WHAT-IT-MEANS comment after each step; fixes are at the bottom.
-- ============================================================================

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

-- ---------------------------------------------------------------------------
-- STEP 1: task states. Every row here should say state = 'started'.
-- A 'suspended' task is the single most likely cause: CREATE OR REPLACE TASK
-- leaves a task suspended, and suspending a root stops its whole chain.
-- ---------------------------------------------------------------------------
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "name", "state", "schedule", "predecessors", "warehouse"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
ORDER BY IFF("state" = 'suspended', 0, 1), "name";

-- ---------------------------------------------------------------------------
-- STEP 2: did the alert tasks RUN in the last 48h, and did they succeed?
-- No rows for TASK_ALERT_SCAN / TASK_ALERT_SCAN_DAILY -> chain not firing
--   (see STEP 1 / FIX A). A failed parent (TASK_QH_EXTRACT,
--   TASK_NIGHTLY_RECONCILE) stops its scan, so the parents are listed too.
-- The standalone raisers are listed as well; TASK_CANARY_SENTINEL runs weekly
--   (Mondays), so no row for it in a mid-week 48h window is normal.
-- Rows with STATE = 'FAILED'   -> read ERROR_MESSAGE; that's the bug.
-- ---------------------------------------------------------------------------
SELECT NAME, STATE, ERROR_MESSAGE,
       SCHEDULED_TIME, COMPLETED_TIME
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
       SCHEDULED_TIME_RANGE_START => DATEADD('hour', -48, CURRENT_TIMESTAMP()),
       RESULT_LIMIT => 1000))
WHERE NAME IN ('TASK_ALERT_SCAN', 'TASK_ALERT_SCAN_DAILY', 'TASK_ALERT_NOTIFY',
               'TASK_LOAD_HOURLY', 'TASK_QH_EXTRACT', 'TASK_LOAD_DAILY',
               'TASK_NIGHTLY_RECONCILE', 'TASK_ANOMALY_SWEEP', 'TASK_CANARY_SENTINEL',
               'TASK_WAREHOUSE_CHANGE_SCAN', 'TASK_CHANGE_IMPACT_SCAN')
ORDER BY SCHEDULED_TIME DESC;

-- ---------------------------------------------------------------------------
-- STEP 3: are new events being RAISED? (scan health)
-- Days with zero rows after a date = SP_ALERT_SCAN stopped raising then.
-- Events exist but Teams is quiet -> the problem is delivery (steps 4-6).
-- ---------------------------------------------------------------------------
SELECT DATE(RAISED_AT) AS DAY, STATUS, COUNT(*) AS EVENTS
FROM ALERT_EVENTS
WHERE RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1, 2 ORDER BY 1 DESC, 2;

-- ---------------------------------------------------------------------------
-- STEP 4a: successful deliveries per day/route. This table records
-- SUCCESSES only (EVENT_ID, ROUTE_ID, SENT_AT). The last DAY here is the
-- last time a Teams card actually went out.
-- ---------------------------------------------------------------------------
SELECT DATE(SENT_AT) AS DAY, ROUTE_ID, COUNT(*) AS SENT
FROM ALERT_DELIVERIES
WHERE SENT_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1, 2 ORDER BY 1 DESC, 2;

-- ---------------------------------------------------------------------------
-- STEP 4b: sender-side failures and the expiry watchdog (these log to
-- APP_ERROR_LOG, not the deliveries table). ROUTE first lists which fix a
-- row belongs to -- the V164 escalation email logs under PAGE 'NotifyWebhook'
-- too, because it runs inside SP_NOTIFY_WEBHOOK, but it is NOT a Teams fault.
-- 'undelivered_expired'   -> events aged past 24h with no successful send
--                            (sender running but nothing eligible reached it
--                            in time, or the send kept failing).
-- 'escalation_email_failed' -> the V164 EMAIL leg: OVERWATCH_EMAIL has no
--                            DEFAULT_RECIPIENTS, or the proc owner lost USAGE
--                            on it -> FIX D. Rotating the Teams secret or
--                            recreating the Teams integration never fixes it.
-- 'escalation_failed' (any other 'escalation%' type) -> the escalation pass
--                            itself; the run's deliveries still went -> FIX D
--                            (read its ERROR_MESSAGE there), never FIX C.
-- every other webhook/notification error -> the Teams integration end
--                            (STEP 6, FIX C).
-- ---------------------------------------------------------------------------
SELECT DATE(LOGGED_AT) AS DAY,
       CASE WHEN ERROR_TYPE = 'escalation_email_failed'
                 THEN 'FIX D: escalation email (OVERWATCH_EMAIL DEFAULT_RECIPIENTS / USAGE)'
            WHEN ERROR_TYPE ILIKE 'escalation%'
                 THEN 'FIX D: escalation pass (not Teams)'
            ELSE 'FIX C: Teams integration' END AS ROUTE,
       ERROR_TYPE, LEFT(ERROR_MESSAGE, 140) AS MSG, COUNT(*) AS N
FROM APP_ERROR_LOG
WHERE PAGE = 'NotifyWebhook'
  AND LOGGED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC, 2, 3;

-- ---------------------------------------------------------------------------
-- STEP 5: routes and rule config. Look for ENABLED = FALSE where you expect
-- TRUE, and a COMPANY_FILTER that excludes what you expect to receive
-- (the Teams route is deliberately ALFA-only since V034).
-- ---------------------------------------------------------------------------
SELECT * FROM ALERT_ROUTES;
SELECT * FROM ALERT_CONFIG ORDER BY 1;

-- ---------------------------------------------------------------------------
-- STEP 6: the Teams integration itself.
-- 'enabled' must be true. If the webhook URL rotated in Teams, deliveries
-- fail with HTTP errors in STEP 4 even though everything else is healthy.
-- ---------------------------------------------------------------------------
SHOW NOTIFICATION INTEGRATIONS;

-- ---------------------------------------------------------------------------
-- STEP 7: the warehouse every task runs on. (Resource monitors removed in
-- v4.45; if this account ever re-adds one and it trips,
-- tasks queue or fail even though the app (same warehouse, your session)
-- may still respond from cache.
-- ---------------------------------------------------------------------------
SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';
SHOW RESOURCE MONITORS;  -- expect none since V045

-- ============================================================================
-- FIXES
-- ============================================================================
-- FIX A (by far the most common): resume the hourly and daily chains + the standalone raisers.
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
--   ^ child of TASK_ALERT_SCAN; the hourly DEPENDENTS_ENABLE above already resumes it --
--     a standalone resume for when only the sender is suspended
-- The standalone roots (own cron; neither DEPENDENTS_ENABLE above reaches them):
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP RESUME;
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CANARY_SENTINEL RESUME;
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CHANGE_IMPACT_SCAN RESUME;
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN');
--   ^ also resumes its child TASK_LEDGER_AUTOBOOK
--
-- FIX B (scan or sender FAILING in STEP 2): send me the ERROR_MESSAGE text —
-- that is the actual bug and we fix it in the repo, not in the worksheet.
--
-- FIX C (deliveries failing with webhook/HTTP errors in STEP 4): the Teams URL
-- rotated -- run ONLY the ROTATION RUNBOOK step of snowflake/webhook_delivery.sql
-- (ALTER SECRET ... SET SECRET_STRING with the new value, pasted in Snowsight).
-- Do NOT re-run that whole file for a rotation: recreating the integration drops
-- its grants. Re-run it only if the URL prefix or the Adaptive Card template
-- changed (it is idempotent: it never adds a second route).
-- NOT for STEP 4b's escalation_* rows (ROUTE 'FIX D'): the Teams fix never fixes them.
--
-- FIX D (STEP 4b ROUTE 'FIX D' -- the V164 CRITICAL escalation, which logs under
-- PAGE 'NotifyWebhook' because it runs inside the notifier; Teams is not at fault):
--   escalation_email_failed -> the escalation email names no address: it goes ONLY
--     to OVERWATCH_EMAIL's DEFAULT_RECIPIENTS. Look, then follow
--     docs/EMAIL_RECIPIENT_RUNBOOK.md requirement 4 (Step 2 sets DEFAULT_RECIPIENTS
--     without touching ALLOWED_RECIPIENTS -- SET replaces a whole list, so never
--     re-SET ALLOWED_RECIPIENTS without every address DESC shows):
-- DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;     -- ENABLED true, DEFAULT_RECIPIENTS set?
-- SHOW GRANTS ON INTEGRATION OVERWATCH_EMAIL;        -- USAGE to SNOW_ACCOUNTADMINS?
--     After the fix, prove it: no new escalation_email_failed row after the next
--     hourly TASK_ALERT_NOTIFY run, and a SUCCESS row in
--     INFORMATION_SCHEMA.NOTIFICATION_HISTORY for OVERWATCH_EMAIL once the next
--     escalation sends (the runbook's Verify step).
--   escalation_failed -> the escalation pass itself errored (the run's normal
--     deliveries still went); send me the ERROR_MESSAGE text (as FIX B). RUNBOOK.md
--     section 19 "Escalation symptoms" lists both.
-- ============================================================================
