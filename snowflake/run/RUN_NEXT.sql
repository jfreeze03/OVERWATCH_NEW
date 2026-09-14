-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (DIAGNOSTIC: why is FACT_OBJECT_COST_DAILY stale?)
--
--  SYMPTOM (dashboard, 2026-09-14): FACT_OBJECT_COST_DAILY last load 2026-09-09
--  06:45, ~123h stale, ROW COUNT still full (916,574). Every OTHER fact loaded
--  this morning. This is the ONE source on its own standalone task.
--
--  MECHANISM: TASK_LOAD_OBJECT_COST (own task, WH_ALFA_ADMIN, cron 45 6 * * * CT)
--  calls SP_LOAD_OBJECT_COST, an ATOMIC DELETE+INSERT that ROLLS BACK on error and
--  KEEPS the last good fill (V062/V067). So a frozen LOAD_TS + full row count = the
--  loader is FAILING every night, not idle. It is the heaviest loader in the system
--  (ACCESS_HISTORY flatten x QUERY_ATTRIBUTION_HISTORY, 1 query -> N objects), so a
--  statement/resource timeout is the prime suspect -- see the customer ETL p95 blowups
--  and the open "SP_LOAD_PATTERN_COST regressed" critical (same cost-attribution family).
--
--  GOAL: pull the ACTUAL failure so the fix targets the real cause, not a guess.
--  READ-ONLY. Run All as SNOW_ACCOUNTADMINS. Paste each RESULT block back to Claude.
--
--  NOTE: the prior ETL cost-attribution-join diagnostic that was here is preserved in
--  git history on the runbox branch (commit 303ac7e); nothing below re-runs it.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;      -- ACCOUNT_USAGE + the OVERWATCH objects need the admin role
USE WAREHOUSE WH_ALFA_ADMIN;      -- any running warehouse is fine

-- [1] THE ANSWER ------------------------------------------------------------
--  SP_LOAD_OBJECT_COST self-logs its real Snowflake error here on rollback.
--  ERROR_MESSAGE = the actual SQLERRM (expect a timeout / resource / spill line).
--  If this returns rows dated 2026-09-10 onward, the load is failing-and-rolling-back
--  (confirmed). If it returns NOTHING, the failure is a TASK-level abort -> see [2].
SELECT LOGGED_AT, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'ObjectCost'
ORDER BY LOGGED_AT DESC
LIMIT 20;
--  >>> paste RESULT [1] <<<

-- [2] TASK RUN HISTORY (last 10 days) ---------------------------------------
--  IMPORTANT: STATE may read SUCCEEDED even on a failed load, because the proc
--  CATCHES its own error and RETURNS a string. So the truth is in RETURN_VALUE
--  ('FAILED: object-cost load rolled back ...' vs 'OK'). A STATE=FAILED row with a
--  timeout ERROR_MESSAGE instead means the TASK itself was killed (task-level timeout,
--  the proc never got to log to [1]). RUN_SEC shows how long each attempt ran.
SELECT SCHEDULED_TIME, STATE, RETURN_VALUE, ERROR_CODE, ERROR_MESSAGE,
       DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SEC
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
        TASK_NAME => 'TASK_LOAD_OBJECT_COST',
        SCHEDULED_TIME_RANGE_START => DATEADD('day', -10, CURRENT_TIMESTAMP())))
ORDER BY SCHEDULED_TIME DESC;
--  >>> paste RESULT [2] <<<

-- [3] TASK STATE / SCHEDULE / WAREHOUSE -------------------------------------
--  Confirm the task is 'started' (not suspended), on WH_ALFA_ADMIN, cron 45 6 * * *.
SHOW TASKS LIKE 'TASK_LOAD_OBJECT_COST' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
--  >>> paste RESULT [3] <<<

-- [4] THE FRESHNESS STAMP THE DASHBOARD READS -------------------------------
--  Confirms the stale LAST_LOAD_TS + full ROW_COUNT (= the rollback signature).
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME = 'FACT_OBJECT_COST_DAILY';
--  >>> paste RESULT [4] <<<

-- [5] TIMEOUTS IN EFFECT FOR THE TASK (the likely fix lever) -----------------
--  USER_TASK_TIMEOUT_MS (task-level, default 3600000 = 60m) and the statement timeout
--  that applies to the proc's INSERTs. If the run in [2] died near one of these, that
--  is the knob to raise.
SHOW PARAMETERS LIKE '%TIMEOUT%' IN TASK DBA_MAINT_DB.OVERWATCH.TASK_LOAD_OBJECT_COST;
--  >>> paste RESULT [5] <<<

-- [6] WAREHOUSE STATEMENT TIMEOUT -------------------------------------------
--  The account/warehouse STATEMENT_TIMEOUT_IN_SECONDS the object-cost INSERTs run under.
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;
--  >>> paste RESULT [6] <<<
