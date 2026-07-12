-- ============================================================================
-- OVERWATCH alert-pipeline diagnosis (2026-07-12, "no alerts anymore")
-- Run top to bottom in a Snowsight worksheet as ACCOUNTADMIN.
-- The pipeline: TASK_LOAD_HOURLY -> TASK_ALERT_SCAN -> SP_ALERT_SCAN
--   -> ALERT_EVENTS -> TASK_ALERT_NOTIFY -> SP_NOTIFY_WEBHOOK
--   -> ALERT_ROUTES / notification integration -> Teams card.
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
-- No rows for TASK_ALERT_SCAN  -> chain not firing (see STEP 1 / FIX A).
-- Rows with STATE = 'FAILED'   -> read ERROR_MESSAGE; that's the bug.
-- ---------------------------------------------------------------------------
SELECT NAME, STATE, ERROR_MESSAGE,
       SCHEDULED_TIME, COMPLETED_TIME
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
       SCHEDULED_TIME_RANGE_START => DATEADD('hour', -48, CURRENT_TIMESTAMP()),
       RESULT_LIMIT => 1000))
WHERE NAME IN ('TASK_ALERT_SCAN', 'TASK_ALERT_NOTIFY', 'TASK_LOAD_HOURLY',
               'TASK_LOAD_DAILY', 'TASK_WAREHOUSE_CHANGE_SCAN')
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
-- STEP 4: are deliveries being ATTEMPTED, and how do they end?
-- No rows at all         -> TASK_ALERT_NOTIFY not running (STEP 1/2, FIX A).
-- error_type rows        -> read them: 'undelivered_expired' = event was
--                           already >24h old when the sender saw it;
--                           webhook/HTTP errors = integration (STEP 6).
-- ---------------------------------------------------------------------------
SELECT DATE(ATTEMPTED_AT) AS DAY, ROUTE_NAME, STATUS, ERROR_TYPE, COUNT(*) AS N
FROM ALERT_DELIVERIES
WHERE ATTEMPTED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
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
-- STEP 7: the warehouse every task runs on. If a resource monitor tripped,
-- tasks queue or fail even though the app (same warehouse, your session)
-- may still respond from cache.
-- ---------------------------------------------------------------------------
SHOW WAREHOUSES LIKE 'WH_ALFA_OVERWATCH';
SHOW RESOURCE MONITORS;

-- ============================================================================
-- FIXES
-- ============================================================================
-- FIX A (by far the most common): resume the whole hourly chain + loners.
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
-- SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;      -- own schedule
-- ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS RESUME; -- own schedule
--
-- FIX B (scan or sender FAILING in STEP 2): send me the ERROR_MESSAGE text —
-- that is the actual bug and we fix it in the repo, not in the worksheet.
--
-- FIX C (deliveries failing with webhook/HTTP errors in STEP 4): recreate the
-- Teams integration per snowflake/webhook_delivery.sql (v2 Adaptive Card).
-- ============================================================================
