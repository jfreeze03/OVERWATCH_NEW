-- ===========================================================================
--  OVERWATCH  -  RUN BOX  (RUN_NEXT.sql)          updated: 2026-08-25
-- ===========================================================================
--  Post-apply verification for V091 (auto-resolve cleared alerts). Run each
--  block in Snowsight, paste the result back to Claude. All read-only except
--  Q3, which just runs the scan the hourly task already runs (safe/idempotent).
-- ===========================================================================

-- ------------------------------------------------------------------------
-- Q1: V091 recorded?
-- LOOK FOR: SCHEMA_VERSION = 91.
-- ------------------------------------------------------------------------
SELECT MAX(VERSION) AS SCHEMA_VERSION
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;

-- RESULT Q1:


-- ------------------------------------------------------------------------
-- Q2: new columns present + the 3 rules opted in?
-- LOOK FOR: 3 rows, AUTO_CLEAR_ENABLED = TRUE, CLEAR_THRESHOLD_NUM = NULL
--           (NULL means 0.9 x THRESHOLD_NUM is used at sweep time).
-- ------------------------------------------------------------------------
SELECT RULE_ID, ENABLED, AUTO_CLEAR_ENABLED, THRESHOLD_NUM, CLEAR_THRESHOLD_NUM
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')
ORDER BY RULE_ID;

-- RESULT Q2:


-- ------------------------------------------------------------------------
-- Q3: exercise the re-derived proc now (the hourly task runs this anyway).
-- LOOK FOR: 'alert scan v11 (V091: + auto-clear sweep): N/16 rule blocks ok'.
--           The 'v11' string confirms SP_ALERT_SCAN was replaced by V091.
-- ------------------------------------------------------------------------
CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();

-- RESULT Q3:


-- ------------------------------------------------------------------------
-- Q4: did the auto-clear sweep error? (it is wrapped so a failure can't break
--     the scan — this just checks the log)
-- LOOK FOR: 0 rows.
-- ------------------------------------------------------------------------
SELECT ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE ERROR_TYPE = 'autoclear_sweep_failed'
LIMIT 20;

-- RESULT Q4:
