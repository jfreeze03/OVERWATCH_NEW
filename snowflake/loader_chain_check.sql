-- loader_chain_check.sql — one-run Snowsight diagnosis for "the app went
-- stale after a migration": no new alerts, task-graph failures missing,
-- boards frozen. Same recipe as alert_pipeline_check.sql: run top to
-- bottom with your admin role, read each result, fixes annotated.
--
-- THE USUAL CULPRIT (the 07-12 outage class): a migration's worksheet run
-- halts partway, leaving every child task suspended. Step 0 is the fix —
-- safe to run any time, it only (re)enables the two roots' task trees.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

-- 0) THE LIKELY FIX. Enables both roots + every dependent task. Idempotent.
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');

-- 1) Task states — every row should read 'started' after step 0.
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "name" AS TASK_NAME, "state" AS STATE, "schedule" AS SCHEDULE,
       "predecessors" AS AFTER, "warehouse" AS WAREHOUSE
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
ORDER BY 1;
-- Fix if any 'suspended': rerun step 0; a standalone task (no predecessor)
-- needs its own ALTER TASK <name> RESUME.

-- 2) Did the chain actually RUN in the last 3 hours, and did it succeed?
SELECT NAME, STATE, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
       LEFT(COALESCE(ERROR_MESSAGE, ''), 200) AS ERROR_MESSAGE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
        SCHEDULED_TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()),
        RESULT_LIMIT => 200))
WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
ORDER BY SCHEDULED_TIME DESC;
-- Read: TASK_LOAD_HOURLY then TASK_QH_EXTRACT then TASK_LOAD_MARTS_V27_HOURLY
-- + TASK_OPS_DIAG_HOURLY each hour; alert scan/notify beside them. A FAILED
-- row's ERROR_MESSAGE names the broken statement. 'SKIPPED' children mean
-- the predecessor failed — fix the predecessor, the children follow.

-- 3) Loader-side errors the app logged (isolated arms land here, not in
--    task failures) — anything in the last 24h needs reading.
SELECT LOGGED_AT, PAGE, ERROR_TYPE, LEFT(ERROR_MESSAGE, 160) AS ERROR_MESSAGE, CONTEXT
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
  AND PAGE IN ('MartLoader', 'ExtractLoader', 'ChangeImpactScan')
ORDER BY LOGGED_AT DESC
LIMIT 100;

-- 4) Freshness — the loader-owned rows. HOURS_BEHIND beyond ~2 for an
--    hourly source (or ~26 for a daily one) means its loader is not landing.
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS,
       ROUND(DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0, 1) AS HOURS_BEHIND
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
ORDER BY HOURS_BEHIND DESC;

-- 5) The two facts the pages lean on hardest — do they have TODAY?
SELECT 'FACT_QUERY_HOURLY' AS FACT, MAX(HOUR_TS) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
UNION ALL
SELECT 'MART_TASK_GRAPH_DAILY', MAX(DAY)::TIMESTAMP_NTZ FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
UNION ALL
SELECT 'ALERT_EVENTS (raised)', MAX(RAISED_AT) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
UNION ALL
SELECT 'OW_QH_EXTRACT', MAX(START_TIME)::TIMESTAMP_NTZ FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT;
-- Fix for a stale extract/facts after step 0: force one cycle by hand —
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(0);
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(2);
