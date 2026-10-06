-- >>> SUPERSEDED 2026-10-06: run snowflake/run/SYSADMINS_OWNER_PREFLIGHT_v2_2026-10-06.sql instead (it adds the checks this one missed).
-- =====================================================================
--  OVERWATCH -- SYSADMINS_OWNER_PREFLIGHT_2026-10-06.sql   (READ-ONLY)
--  Question: if SNOW_SYSADMINS owned and drove the OVERWATCH app instead of SNOW_ACCOUNTADMINS, what could it
--  still read and do? The app runs with OWNER'S RIGHTS, so every query, CALL and lever runs as the owner role
--  with ONLY that role's own hierarchy (no secondary roles). This file runs the app's kinds of reads AS
--  SNOW_SYSADMINS in exactly that mode. Nothing is created, changed, granted or called; no Cortex call is made.
--
--  Run top to bottom. Paste back EVERY grid (an empty grid is an answer: say 'no rows'). An error IS the answer
--  for that block: copy its text, then highlight from the NEXT block to the end and run again.
--  PAIRS: each SHOW is followed by a SELECT ... RESULT_SCAN(LAST_QUERY_ID()) count -- run them back to back.
--  Section Z (optional) repeats the counts as SNOW_ACCOUNTADMINS so we can compare side by side.
-- =====================================================================

USE ROLE SNOW_SYSADMINS;
USE SECONDARY ROLES NONE;          -- the app's owner session has only the owner role's hierarchy
USE WAREHOUSE WH_ALFA_ADMIN;       -- the app's query warehouse (roles.sql grants SNOW_SYSADMINS USAGE)
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- (S0) Confirm the session: expect SNOW_SYSADMINS, no secondary roles, WH_ALFA_ADMIN.
SELECT CURRENT_ROLE() AS ROLE, CURRENT_SECONDARY_ROLES() AS SECONDARY, CURRENT_WAREHOUSE() AS WH;

-- (S1) Everything SNOW_SYSADMINS holds. Look for: ROLE rows (roles it inherits, e.g. SYSADMIN / ACCOUNTADMIN),
--      ACCOUNT-level privileges (MANAGE GRANTS, MONITOR USAGE, EXECUTE TASK ...), DATABASE_ROLE SNOWFLAKE.* rows
--      (CORTEX_USER, ORGANIZATION_*_VIEWER, ...), APPLICATION_ROLE rows (Trust Center), PROCEDURE / WAREHOUSE rows.
SHOW GRANTS TO ROLE SNOW_SYSADMINS;

-- (S1b) Who holds SNOW_SYSADMINS (users and roles). These people can already open the app today.
SHOW GRANTS OF ROLE SNOW_SYSADMINS;

-- (S2) THE ADMIN CHECK the app runs: must list the 6 DSA members as granted_to = USER. Empty or an error means
--      SNOW_SYSADMINS cannot see DSA membership, and every DSA admin would be read-only after the switch.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;

-- (S3) ACCOUNT_USAGE (most panels). Expect a number, not an error.
SELECT COUNT(*) AS METERING_ROWS_24H
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP());

-- (S4) ORGANIZATION_USAGE (Cost > Contract / Spend, org spend). An error = SNOW_SYSADMINS lacks the org-usage access.
SELECT COUNT(*) AS ORG_USAGE_ROWS_3D
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE());
SELECT COUNT(*) AS REMAINING_BALANCE_ROWS
FROM SNOWFLAKE.ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY
WHERE DATE >= DATEADD('day', -7, CURRENT_DATE());

-- (S5) Trust Center findings (Security > Trust Center). An error = no Trust Center application role.
SELECT COUNT(*) AS TRUST_CENTER_FINDINGS FROM SNOWFLAKE.TRUST_CENTER.FINDINGS;

-- (S6) OVERWATCH's own tables (roles.sql grants these). Expect a number.
SELECT COUNT(*) AS SETTINGS_ROWS FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

-- (S7) Stored procedures the app CALLs (about 45, all owned by SNOW_ACCOUNTADMINS). SHOW lists only procedures
--      this role holds a privilege on: if the count is far below the total, the app's CALLs would fail.
SHOW PROCEDURES IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT COUNT(*) AS PROCEDURES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S8) Warehouses (Operations, levers, idle/sizing panels). SHOW lists only warehouses this role has a privilege on.
SHOW WAREHOUSES;
SELECT COUNT(*) AS WAREHOUSES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S9) OVERWATCH's tasks (Operations > Tasks, pipeline health).
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT COUNT(*) AS TASKS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S10) Task run history through INFORMATION_SCHEMA (the live task timeline). Expect a number (0 = cannot see runs).
SELECT COUNT(*) AS TASK_RUNS_24H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())))
WHERE SCHEMA_NAME = 'OVERWATCH';

-- (S11) Native email alerts and their history (Alerts > Native delivery).
SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT COUNT(*) AS ALERTS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SELECT COUNT(*) AS ALERT_RUNS_24H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.ALERT_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())));

-- (S12) Notification integrations and delivery history (Alerts > delivery health).
SHOW NOTIFICATION INTEGRATIONS;
SELECT COUNT(*) AS INTEGRATIONS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SELECT COUNT(*) AS NOTIFICATIONS_24H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
    START_TIME => DATEADD('hour', -24, CURRENT_TIMESTAMP())));

-- (S13) Account / warehouse parameters (timeout posture panels).
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;

-- (S14) Shares, databases and resource monitors (Security > Exposure, inventories).
SHOW SHARES;
SELECT COUNT(*) AS SHARES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW DATABASES;
SELECT COUNT(*) AS DATABASES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW RESOURCE MONITORS;
SELECT COUNT(*) AS MONITORS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S15) The app's code stage (the owner of the app must be able to read its files).
LIST @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE PATTERN = '.*streamlit_app[.]py';

ALTER SESSION UNSET TIMEZONE;

-- =====================================================================
-- (Z) OPTIONAL comparison: the same counts as today's owner. Run this section on its own after the above.
-- =====================================================================
-- USE ROLE SNOW_ACCOUNTADMINS;
-- USE SECONDARY ROLES NONE;
-- SHOW PROCEDURES IN SCHEMA DBA_MAINT_DB.OVERWATCH;
-- SELECT COUNT(*) AS PROCEDURES_VISIBLE_ACCOUNTADMINS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- SHOW WAREHOUSES;
-- SELECT COUNT(*) AS WAREHOUSES_VISIBLE_ACCOUNTADMINS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
-- SELECT COUNT(*) AS TASKS_VISIBLE_ACCOUNTADMINS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- SHOW SHARES;
-- SELECT COUNT(*) AS SHARES_VISIBLE_ACCOUNTADMINS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- SHOW DATABASES;
-- SELECT COUNT(*) AS DATABASES_VISIBLE_ACCOUNTADMINS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- USE SECONDARY ROLES ALL;        -- back to your usual worksheet setting (change it if yours differs)
