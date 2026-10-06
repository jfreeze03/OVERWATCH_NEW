-- =====================================================================
--  OVERWATCH -- SYSADMINS_OWNER_PREFLIGHT_v2_2026-10-06.sql (READ-ONLY). Supersedes SYSADMINS_OWNER_PREFLIGHT_2026-10-06.sql
--  (runbox eda276e6): if you have not run that one, run only this file. Letters in brackets ([B], [J], [L] ...) name grant
--  blocks in the switch plan Claude sends with your results; you do not need them to run this file.
--  New in v2:
--    S0b no-op check; S1 ownership rows; S2 compared BY NAME with Z2; S6c (COMPANY_FOR_* UDFs);
--    S15 runtime attachments; S17 (ETL tables the app reads directly); Z16 future grants;
--    Z17 how SNOW_ACCOUNTADMINS reads the ETL tables; Z18 who holds USAGE on the database and schema.
--  Nothing is created, changed, granted or called. No Cortex call is made.
--  PART 1: as SNOW_SYSADMINS with USE SECONDARY ROLES NONE (exactly the app owner's session).
--  PART Z: the same reads as SNOW_ACCOUNTADMINS. This is the parity baseline that gates each grant.
--  When to run:
--    Run PART 1 + PART Z now.
--    After the grants (before Step 5), run PART 1 again: every AFTER expectation must hold.
--    Re-run PART 1's S2, S6b, S6c, S7, S9, S11 and S17 at +1 h and +24 h after the cutover. The app's probe reads
--    never log a missing grant (query.py:1337), so APP_ERROR_LOG cannot prove these.
--  Paste back every grid ('no rows' is an answer). An error IS that statement's answer: copy it and continue.
--  Run each SHOW and its RESULT_SCAN(LAST_QUERY_ID()) summary back to back.
-- =====================================================================

-- ================================ PART 1 ================================
USE ROLE SNOW_SYSADMINS;
USE SECONDARY ROLES NONE;
USE WAREHOUSE WH_ALFA_ADMIN;

-- (S0) Expect SNOW_SYSADMINS / no secondary roles / WH_ALFA_ADMIN.
SELECT CURRENT_ROLE() AS ROLE, CURRENT_SECONDARY_ROLES() AS SECONDARY, CURRENT_WAREHOUSE() AS WH;

-- (S0b) Run one name per statement: an unknown role name errors.
--       HAS_ACCOUNTADMIN or HAS_SNOW_ACCOUNTADMINS = TRUE: the switch is a no-op. Stop and re-plan.
--       HAS_SYSADMIN decides gate [I].
SELECT IS_ROLE_IN_SESSION('ACCOUNTADMIN') AS HAS_ACCOUNTADMIN;
SELECT IS_ROLE_IN_SESSION('SNOW_ACCOUNTADMINS') AS HAS_SNOW_ACCOUNTADMINS;
SELECT IS_ROLE_IN_SESSION('SECURITYADMIN') AS HAS_SECURITYADMIN;
SELECT IS_ROLE_IN_SESSION('SYSADMIN') AS HAS_SYSADMIN;
SELECT IS_ROLE_IN_SESSION('SNOW_PRI_GFR_PRD_ALFA_DSA') AS HAS_DSA;

-- (S1) Direct grants that matter, plus database/schema ownership.
--      If SNOW_SYSADMINS already owns a database or schema, or holds CREATE STREAMLIT anywhere, it can already
--      publish an owner's-rights app. Then [C] adds no new kind of power.
SHOW GRANTS TO ROLE SNOW_SYSADMINS;
SELECT "granted_on", "privilege", "name"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "granted_on" IN ('ACCOUNT','ROLE','DATABASE_ROLE','APPLICATION_ROLE','WAREHOUSE','PROCEDURE','FUNCTION','TASK','ALERT','INTEGRATION','STAGE','STREAMLIT')
   OR ("granted_on" = 'SCHEMA' AND "privilege" <> 'USAGE')
   OR ("granted_on" IN ('DATABASE','SCHEMA') AND "privilege" IN ('OWNERSHIP','CREATE STREAMLIT'))
ORDER BY 1, 2, 3;

-- (S1b) Who holds SNOW_SYSADMINS. After the switch these people control the code that runs as the owner,
--       and can extend the app's rights to other viewers.
--       A ROLE row named SNOW_ACCOUNTADMINS means SNOW_ACCOUNTADMINS inherits it: a deploy as SNOW_ACCOUNTADMINS
--       would then silently take ownership back.
SHOW GRANTS OF ROLE SNOW_SYSADMINS;
SELECT "granted_to", COUNT(*) AS N, LISTAGG("grantee_name", ', ') WITHIN GROUP (ORDER BY "grantee_name") AS WHO
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) GROUP BY 1;

-- (S2) THE ADMIN CHECK the app runs (session._admin_role_rows).
--      PASS = DSA_USERS identical to Z2's list. An error, 0 rows, or a different list means gate [J].
--      Without it, every DSA-only admin becomes read-only.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SELECT COUNT_IF("granted_to" = 'USER') AS DSA_USER_ROWS,
       LISTAGG(IFF("granted_to" = 'USER', "grantee_name", NULL), ', ') WITHIN GROUP (ORDER BY "grantee_name") AS DSA_USERS,
       COUNT_IF("granted_to" = 'ROLE') AS DSA_ROLE_ROWS
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- AFTER [J] only (read-only):
-- CALL DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS();

-- (S3) ACCOUNT_USAGE. Expect a number.
SELECT COUNT(*) AS METERING_ROWS_24H FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP());

-- (S4) ORGANIZATION_USAGE, the 5 views the app reads. 1 or 0 = readable; an error = no access.
SELECT COUNT(*) AS USAGE_IN_CURRENCY_OK FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY LIMIT 1);
SELECT COUNT(*) AS REMAINING_BALANCE_OK FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY LIMIT 1);
SELECT COUNT(*) AS CONTRACT_ITEMS_OK    FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.CONTRACT_ITEMS LIMIT 1);
SELECT COUNT(*) AS RATE_SHEET_OK        FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.RATE_SHEET_DAILY LIMIT 1);
SELECT COUNT(*) AS MARKETPLACE_OK       FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY LIMIT 1);

-- (S5) Trust Center findings.
SELECT COUNT(*) AS TRUST_CENTER_READABLE FROM (SELECT 1 FROM SNOWFLAKE.TRUST_CENTER.FINDINGS LIMIT 1);
-- (S5b) Native cost-anomaly feed.
SELECT COUNT(*) AS ANOMALY_INSIGHTS_READABLE FROM (SELECT * FROM SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS LIMIT 1);
-- (S5c) Cortex entitlement WITHOUT calling Cortex: does PUBLIC hold CORTEX_USER, a model role or an AI privilege?
SHOW GRANTS TO ROLE PUBLIC;
SELECT "granted_on", "privilege", "name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "name" ILIKE '%CORTEX%' OR "privilege" ILIKE '%AI%';

-- (S6) OVERWATCH's own tables. Expect a number.
SELECT COUNT(*) AS SETTINGS_ROWS FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
-- (S6b) Tables and views this role can see in OVERWATCH. PASS = equal to Z6b.
SELECT TABLE_TYPE, COUNT(*) AS N FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'OVERWATCH' GROUP BY 1 ORDER BY 1;
-- (S6c) The COMPANY_FOR_* UDFs the builders call directly. Expect three values and no error.
SELECT DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE('WH_ALFA_ADMIN') AS WH_CO,
       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER('X') AS USER_CO,
       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE('DBA_MAINT_DB') AS DB_CO;

-- (S7) The procedures the app CALLs. BEFORE the grants: 0 rows. AFTER: 8 rows (9 with SP_ADMIN_ROLE_MEMBERS).
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, PROCEDURE_OWNER
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH'
  AND PROCEDURE_NAME IN ('SP_ALERT_LIFECYCLE','SP_ALERT_SNOOZE','SP_ALERT_CLEAR_SCOPE','SP_INCIDENT_DECLARE',
                         'SP_ACTION_LIFECYCLE','SP_CHANGE_IMPACT_SCAN','SP_WAREHOUSE_CHANGE_SCAN','SP_ADMIN_ROLE_MEMBERS')
ORDER BY 1, 2;

-- (S8) Warehouses visible (compare with Z8).
SHOW WAREHOUSES;
SELECT COUNT(*) AS WAREHOUSES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- (S8b) Other users' statements on the app warehouse in the last hour.
--       A worksheet also shows your own statements, so look at USERS_1H vs Z8b.
SELECT COUNT(*) AS STATEMENTS_1H, COUNT(DISTINCT USER_NAME) AS USERS_1H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE(
    WAREHOUSE_NAME => 'WH_ALFA_ADMIN',
    END_TIME_RANGE_START => DATEADD('hour', -1, CURRENT_TIMESTAMP()),
    RESULT_LIMIT => 1000));

-- (S9) OVERWATCH tasks. AFTER [D]: equal to Z9.
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT COUNT(*) AS TASKS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- (S10) Task runs. AFTER [D]: greater than 0 if any task ran in 24 h.
SELECT COUNT(*) AS TASK_RUNS_24H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())))
WHERE SCHEMA_NAME = 'OVERWATCH';

-- (S11) Native email alerts and their history. AFTER [E]: equal to Z11.
SHOW ALERTS LIKE 'NATIVE_ALERT%' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT COUNT(*) AS ALERTS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SELECT COUNT(*) AS ALERT_RUNS_24H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.ALERT_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())));

-- (S12) Notification integrations. 0 OVERWATCH rows is expected without [K]; the v4.611 banner handles it.
SHOW NOTIFICATION INTEGRATIONS;
SELECT COUNT(*) AS INTEGRATIONS_VISIBLE,
       COUNT_IF("name" IN ('OVERWATCH_EMAIL','OVERWATCH_WEBHOOK_TEAMS')) AS OVERWATCH_INTEGRATIONS
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S13) Parameters (timeout posture).
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;

-- (S14) Inventories (compare with Z14). Lower numbers are accepted, with v4.611's wording.
SHOW SHARES;            SELECT COUNT(*) AS SHARES_VISIBLE    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW DATABASES;         SELECT COUNT(*) AS DATABASES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW RESOURCE MONITORS; SELECT COUNT(*) AS MONITORS_VISIBLE  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW STREAMS IN ACCOUNT LIMIT 200; SELECT COUNT(*) AS STREAMS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- (S15) The app object.
--       Read root_location vs live_version_location_uri (deploy mode, gate [C2]).
--       Read every runtime attachment: query_warehouse, external_access_integrations, external access secrets,
--       import URLs, runtime_name and compute_pool. The new owner needs USAGE on each, or it must be unset first.
--       If DESCRIBE errors here, use Z15.
SHOW STREAMLITS LIKE 'OVERWATCH_APP' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
DESCRIBE STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;
-- (S15b) The legacy code stage. An error is expected unless [C2] was granted.
LIST @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE PATTERN = '.*streamlit_app[.]py';

-- (S16) Who can open the app, and who owns it.
SHOW GRANTS ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;

-- (S17) ETL source tables the app reads DIRECTLY as its owner: Operations > Pipeline, plus the Brief /
--       Control Room nightly-cycle and reference-gap signals. Those are fail-silent probe reads (attention.py:24-133).
--       First the live FQNs. Then one probe each: substitute the SETTINGS values where they differ from the
--       V128/V134-V136 seeds, and add a line per staging table in ETL_REF_GAP_CHECKS.
--       1 or 0 = readable; an error = no access (gate [L] when Z17 reads it).
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
WHERE KEY IN ('ETL_CONTROL_STATUS_FQN','ETL_CONTROL_RUN_ID_FQN','ETL_CONTROL_PARAMS_FQN','ETL_RECON_ERROR_FQN','ETL_REF_GAP_XLAT','ETL_REF_GAP_CHECKS')
ORDER BY 1;
SELECT COUNT(*) AS CONTROL_STATUS_OK FROM (SELECT 1 FROM ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS LIMIT 1);
SELECT COUNT(*) AS CONTROL_RUN_ID_OK FROM (SELECT 1 FROM ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID LIMIT 1);
SELECT COUNT(*) AS CONTROL_PARAMS_OK FROM (SELECT 1 FROM ALFA_EDW_PRD.PUBLIC.CONTROL_PARAMS LIMIT 1);
SELECT COUNT(*) AS RECON_ERROR_OK    FROM (SELECT 1 FROM ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR LIMIT 1);
SELECT COUNT(*) AS REF_XLAT_OK       FROM (SELECT 1 FROM ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT LIMIT 1);
SELECT COUNT(*) AS STAGING_OK        FROM (SELECT 1 FROM ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE LIMIT 1);

-- ================================ PART Z (parity baseline: run separately) ================================
USE ROLE SNOW_ACCOUNTADMINS;
USE SECONDARY ROLES NONE;
USE WAREHOUSE WH_ALFA_ADMIN;

-- (Z0b) One name per statement.
--       HAS_ACCOUNTADMIN decides whether SNOW_ACCOUNTADMINS can act as G2.
--       HAS_SNOW_SYSADMINS = TRUE means a deploy as SNOW_ACCOUNTADMINS would silently re-own the app,
--       and that SNOW_ACCOUNTADMINS can roll back and re-grant on its own.
SELECT IS_ROLE_IN_SESSION('ACCOUNTADMIN') AS HAS_ACCOUNTADMIN;
SELECT IS_ROLE_IN_SESSION('SECURITYADMIN') AS HAS_SECURITYADMIN;
SELECT IS_ROLE_IN_SESSION('SYSADMIN') AS HAS_SYSADMIN;
SELECT IS_ROLE_IN_SESSION('SNOW_SYSADMINS') AS HAS_SNOW_SYSADMINS;
-- (Z1) MANAGE GRANTS, and the database/application roles SNOW_ACCOUNTADMINS reads through
--      (org usage, Trust Center, CORTEX_USER, CORTEX-MODEL-ROLE-*).
SHOW GRANTS TO ROLE SNOW_ACCOUNTADMINS;
SELECT "granted_on", "privilege", "name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "granted_on" IN ('ACCOUNT','ROLE','DATABASE_ROLE','APPLICATION_ROLE') ORDER BY 1, 2, 3;
-- (Z2) The DSA user list S2 must equal.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SELECT COUNT_IF("granted_to" = 'USER') AS DSA_USER_ROWS,
       LISTAGG(IFF("granted_to" = 'USER', "grantee_name", NULL), ', ') WITHIN GROUP (ORDER BY "grantee_name") AS DSA_USERS
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- (Z4) Organization usage parity.
SELECT COUNT(*) AS USAGE_IN_CURRENCY_OK FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY LIMIT 1);
SELECT COUNT(*) AS REMAINING_BALANCE_OK FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY LIMIT 1);
SELECT COUNT(*) AS CONTRACT_ITEMS_OK    FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.CONTRACT_ITEMS LIMIT 1);
SELECT COUNT(*) AS RATE_SHEET_OK        FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.RATE_SHEET_DAILY LIMIT 1);
SELECT COUNT(*) AS MARKETPLACE_OK       FROM (SELECT 1 FROM SNOWFLAKE.ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY LIMIT 1);
-- (Z5 / Z5b) Trust Center and anomaly-feed parity.
SELECT COUNT(*) AS TRUST_CENTER_READABLE FROM (SELECT 1 FROM SNOWFLAKE.TRUST_CENTER.FINDINGS LIMIT 1);
SELECT COUNT(*) AS ANOMALY_INSIGHTS_READABLE FROM (SELECT * FROM SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS LIMIT 1);
-- (Z6b) The table/view counts S6b must match.
SELECT TABLE_TYPE, COUNT(*) AS N FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'OVERWATCH' GROUP BY 1 ORDER BY 1;
-- (Z7) LIVE signatures and owners for grant block [B].
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, PROCEDURE_OWNER
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH'
  AND PROCEDURE_NAME IN ('SP_ALERT_LIFECYCLE','SP_ALERT_SNOOZE','SP_ALERT_CLEAR_SCOPE','SP_INCIDENT_DECLARE',
                         'SP_ACTION_LIFECYCLE','SP_CHANGE_IMPACT_SCAN','SP_WAREHOUSE_CHANGE_SCAN','SP_VERIFY_EXPERIMENT')
ORDER BY 1, 2;
-- (Z8 / Z8b) Warehouse and running-statement visibility today.
SHOW WAREHOUSES;
SELECT COUNT(*) AS WAREHOUSES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SELECT COUNT(*) AS STATEMENTS_1H, COUNT(DISTINCT USER_NAME) AS USERS_1H
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE(
    WAREHOUSE_NAME => 'WH_ALFA_ADMIN',
    END_TIME_RANGE_START => DATEADD('hour', -1, CURRENT_TIMESTAMP()),
    RESULT_LIMIT => 1000));
-- (Z9 / Z11) Task and alert owners (expect SNOW_ACCOUNTADMINS).
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "owner", COUNT(*) AS TASKS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) GROUP BY 1;
SHOW ALERTS LIKE 'NATIVE_ALERT%' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "name", "owner", "state" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) ORDER BY 1;
-- (Z12 / Z14) Integration and inventory counts.
SHOW NOTIFICATION INTEGRATIONS;
SELECT COUNT_IF("name" IN ('OVERWATCH_EMAIL','OVERWATCH_WEBHOOK_TEAMS')) AS OVERWATCH_INTEGRATIONS FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW SHARES;            SELECT COUNT(*) AS SHARES_VISIBLE    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW DATABASES;         SELECT COUNT(*) AS DATABASES_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
SHOW STREAMS IN ACCOUNT LIMIT 200; SELECT COUNT(*) AS STREAMS_VISIBLE FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
-- (Z15) Deploy mode and runtime attachments, if S15's DESCRIBE errored as SNOW_SYSADMINS.
DESCRIBE STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;
-- (Z16) FUTURE grants. Empty = roles.sql's FUTURE lines never took: run [A3] as G2.
SHOW FUTURE GRANTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SHOW FUTURE GRANTS IN DATABASE DBA_MAINT_DB;
-- (Z17) How SNOW_ACCOUNTADMINS reads the ETL tables ('granted separately'). [L] mirrors these rows.
--       Then run the six S17 probes here too, as the parity baseline.
SHOW GRANTS TO ROLE SNOW_ACCOUNTADMINS;
SELECT "granted_on", "privilege", "name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "name" ILIKE 'ALFA_EDW_PRD%' ORDER BY 1, 3, 2;
-- (Z18) Who the new owner could open the app to. App USAGE works only for a role that also holds USAGE on the
--       database and schema. Expect exactly the four access roles (DSA and DTI after p6101-roles).
--       PUBLIC or any other role here is a NO-GO until resolved.
SHOW GRANTS ON SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "privilege", "granted_to", "grantee_name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "privilege" IN ('USAGE','OWNERSHIP') ORDER BY 3;
SHOW GRANTS ON DATABASE DBA_MAINT_DB;
SELECT "privilege", "granted_to", "grantee_name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "privilege" IN ('USAGE','OWNERSHIP') ORDER BY 3;

USE SECONDARY ROLES ALL;   -- back to your usual worksheet setting (change it if yours differs)