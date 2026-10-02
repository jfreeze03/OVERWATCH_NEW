-- =====================================================================
--  OVERWATCH -- STATUS_2026-10-02.sql  (READ-ONLY)
--  Five quick reads that tell Claude exactly where production stands after this morning's apply, so the
--  forward fix (V173: the SEC_NEW_ADMIN_NETWORK "Unsupported subquery type" and the COST_IDLE_OPPORTUNITY
--  "Division by zero") is written against what is really installed.
--  SELECT / SHOW only, plus this worksheet's clock. Nothing is created, changed or granted.
--  Paste back every grid (an empty grid is an answer: say 'no rows'). If a block errors, the error text is
--  that block's answer: copy it, then run from the next block.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- (S1) Which migrations are applied, and when.
SELECT VERSION, APPLIED_AT, LEFT(DESCRIPTION, 90) AS DESCRIPTION
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION >= 160
ORDER BY VERSION;

-- (S2) Every error family logged since 2026-09-30: what is failing now, how often, first and last time.
SELECT PAGE, ERROR_TYPE, LEFT(CONTEXT, 70) AS CONTEXT, COUNT(*) AS N,
       MIN(LOGGED_AT) AS FIRST_AT, MAX(LOGGED_AT) AS LAST_AT,
       LEFT(MAX(ERROR_MESSAGE), 160) AS SAMPLE_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE LOGGED_AT >= '2026-09-30'
GROUP BY 1, 2, 3
ORDER BY LAST_AT DESC;

-- (S3) The alert scans' own heartbeats (the hourly and daily tallies, e.g. 'n/n rule blocks').
SELECT SOURCE_NAME, STATUS, ROW_COUNT, LAST_LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('ALERT_SCAN', 'ALERT_SCAN_DAILY')
ORDER BY SOURCE_NAME;

-- (S4) The warehouses that trip the COST_IDLE_OPPORTUNITY division: queries but zero metered credits over the
--      scan's 14 complete Central days.
SELECT WAREHOUSE_NAME, COUNT(*) AS DAYS, SUM(CREDITS_TOTAL) AS CREDITS, SUM(BILLED_HOURS) AS BILLED_HOURS,
       SUM(ACTIVE_HOURS) AS ACTIVE_HOURS
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
WHERE DAY >= DATEADD('day', -14, CURRENT_DATE()) AND DAY < CURRENT_DATE()
  AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
GROUP BY WAREHOUSE_NAME
HAVING SUM(CREDITS_TOTAL) = 0
ORDER BY WAREHOUSE_NAME;

-- (S5) Did OWNER_REPAIRS PART 1 run? Rows here mean the V167 coverage stamp is in place (an 'invalid
--      identifier COVERAGE_FROM' error means V167 is not applied).
SELECT SOURCE_NAME, STATUS, LAST_LOAD_TS, COVERAGE_FROM
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE COVERAGE_FROM IS NOT NULL
ORDER BY SOURCE_NAME;

-- (S6) The deployed app and its owner (the 'owner' column; docs say SNOW_ACCOUNTADMINS).
SHOW STREAMLITS IN SCHEMA DBA_MAINT_DB.OVERWATCH;

ALTER SESSION UNSET TIMEZONE;
