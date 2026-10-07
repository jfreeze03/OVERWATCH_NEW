-- Run as SNOW_ACCOUNTADMINS.
USE ROLE SNOW_ACCOUNTADMINS;
-- PART B -- V175 verify, right after the apply. Every RESULT should read OK; paste the grids back.
-- Read-only: the only CALL is SP_ADMIN_ROLE_MEMBERS(), which runs one SHOW and writes nothing.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- V175.1 the version row, the procedure and its owner. The procedure must be owned by SNOW_ACCOUNTADMINS: an EXECUTE AS
--        OWNER procedure answers as its owner, and that role sees every DSA grant (PREFLIGHT P175.1).
SELECT 'V175.1 SCHEMA_VERSION has 175' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 175) = 1,
           'OK', 'FAIL: V175 did not finish') AS RESULT
UNION ALL
SELECT 'V175.1 SP_ADMIN_ROLE_MEMBERS() exists, owned by SNOW_ACCOUNTADMINS',
       (SELECT CASE WHEN COUNT(*) = 0 THEN 'FAIL: the procedure is missing - re-run V175'
                    WHEN MAX(PROCEDURE_OWNER) = 'SNOW_ACCOUNTADMINS' THEN 'OK'
                    ELSE 'FAIL: owned by ' || MAX(PROCEDURE_OWNER) || ' - it would answer as that role' END
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
        WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_ADMIN_ROLE_MEMBERS')
UNION ALL
SELECT 'V175.1 the DDL hard-codes SNOW_PRI_GFR_PRD_ALFA_DSA and runs as owner',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS()'), 'SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;')
           AND CONTAINS(UPPER(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS()')), 'EXECUTE AS OWNER'),
           'OK', 'FAIL: not the V175 body');

-- V175.1b USAGE for SNOW_SYSADMINS (the future app owner). Expect a USAGE row with grantee_name SNOW_SYSADMINS
--         (and the OWNERSHIP row of SNOW_ACCOUNTADMINS).
SHOW GRANTS ON PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS();
SELECT 'V175.1b USAGE granted to SNOW_SYSADMINS' AS CHECK_NAME,
       IFF(COUNT_IF("privilege" = 'USAGE' AND "grantee_name" = 'SNOW_SYSADMINS') = 1, 'OK',
           'FAIL: no USAGE for SNOW_SYSADMINS - re-run V175') AS RESULT
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- V175.2 the CALL lists exactly the users SHOW lists (the app reads only granted_to = USER rows). Run the four
--        statements in order: the CALL, the SHOW, then the compare reads both by query id.
CALL DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS();
SET V175_CALL_QID = LAST_QUERY_ID();
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SET V175_SHOW_QID = LAST_QUERY_ID();
WITH c AS (SELECT "grantee_name" AS U FROM TABLE(RESULT_SCAN($V175_CALL_QID)) WHERE "granted_to" = 'USER'),
     s AS (SELECT "grantee_name" AS U FROM TABLE(RESULT_SCAN($V175_SHOW_QID)) WHERE "granted_to" = 'USER')
SELECT 'V175.2 CALL users = SHOW users' AS CHECK_NAME,
       CASE WHEN (SELECT COUNT(*) FROM c) = 0 THEN 'FAIL: the CALL listed no USER row'
            WHEN NOT EXISTS (SELECT U FROM c MINUS SELECT U FROM s)
             AND NOT EXISTS (SELECT U FROM s MINUS SELECT U FROM c)
            THEN 'OK: ' || (SELECT COUNT(*) FROM c) || ' users (' || (SELECT LISTAGG(U, ', ') WITHIN GROUP (ORDER BY U) FROM c) || ')'
            ELSE 'FAIL: the lists differ' END AS RESULT;

-- V175.3 (the cutover check, optional now): the same CALL as SNOW_SYSADMINS with no secondary roles, i.e. what a
--        SNOW_SYSADMINS-owned app will see. Expect the same users as V175.2. Run it LAST: it switches this worksheet's
--        role; open a fresh worksheet afterwards (or USE ROLE SNOW_ACCOUNTADMINS and restore your secondary roles).
--        An error here ("does not exist or not authorized") means SNOW_SYSADMINS lacks USAGE on the procedure, the
--        OVERWATCH schema or DBA_MAINT_DB (roles.sql's Streamlit block grants the last two).
-- USE ROLE SNOW_SYSADMINS;
-- USE SECONDARY ROLES NONE;
-- CALL DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS();

ALTER SESSION UNSET TIMEZONE;
