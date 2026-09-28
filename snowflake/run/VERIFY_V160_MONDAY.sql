-- =====================================================================
--  OVERWATCH -- VERIFY_V160_MONDAY.sql  (READ-ONLY)
--  The V160 PART B grids that wait for the first Monday daily scan (TASK_ALERT_SCAN_DAILY, ~07:00 Central,
--  2026-09-28 onward). Moved out of RUN_NEXT.sql verbatim when RUN_NEXT was reused for V161; V160.1-.5 were
--  the apply-time grids. Paste every grid back; an empty grid is an answer ('no rows').
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- (V160.6) the daily scan's heartbeat reads 12/12 (the RETURN_VALUE of proc-calling tasks is NULL, so read the
--          heartbeat row the scan stamps last).
SELECT SOURCE_NAME, STATUS, ROW_COUNT, LAST_LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY';

-- (V160.7) the week's census. Expect the ACCOUNT row with COMPLETED_AT set (BILLED_CS_CREDITS ~ the Spend panel's
--          sleep-polling billed credits on a 7-day window viewed the same morning) and ~2 POLLER rows (~106 / ~105
--          USD_WEEK; 3 if the TRXS tasks run under different owner roles).
SELECT WEEK_START, ROW_KIND, WAREHOUSE_NAME, OWNER_HINT, CALLS, FAMILIES, POLLERS, RUNS, ACTIVE_DAYS, LAST_ACTIVE_DAY,
       ROUND(CS_CREDITS, 2) AS CS_CREDITS, ROUND(BILLED_CS_CREDITS, 2) AS BILLED_CS_CREDITS, USD_WEEK,
       ABOVE_ALLOWANCE_DAYS, WIN_START, WIN_END, RAISED, SUPERSEDED, CLEARED, COMPLETED_AT
FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY
ORDER BY WEEK_START DESC, ROW_KIND, USD_WEEK DESC;

-- (V160.8) the events. Expect ~2 MEDIUM, METRIC_VALUE ~ USD_WEEK, COMPANY ALFA / Trexis.
SELECT RULE_ID, COMPANY, SEVERITY, STATUS, METRIC_VALUE, TITLE, DEDUPE_KEY, RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'COST_SLEEP_POLLING'
ORDER BY RAISED_AT DESC;

-- (V160.9) errors / deferrals. Expect no rows, or one sleep_polling_scan_deferred followed by a completed week.
SELECT LOGGED_AT, ERROR_TYPE, ERROR_MESSAGE, CONTEXT
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE CONTEXT LIKE 'rule COST_SLEEP_POLLING%'
ORDER BY LOGGED_AT DESC
LIMIT 20;

-- OPTIONAL (commented): force this week's evaluation now instead of waiting for Monday. It RAISES REAL EVENTS
-- (and emails per your routes) and rewrites this week's census.
-- CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(TRUE);

ALTER SESSION UNSET TIMEZONE;
