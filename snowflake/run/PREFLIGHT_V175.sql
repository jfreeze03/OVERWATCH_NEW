-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- ====================================================================================================
--  V175 PREFLIGHT (read-only; run BEFORE applying V175). V175 creates SP_ADMIN_ROLE_MEMBERS(), the admin-access
--  lookup as an owner-run procedure, so the lookup keeps answering after SNOW_SYSADMINS takes over the app.
--  Changes nothing. Run each statement in order (P175.1 and P175.4 read the SHOW right above them).
-- ====================================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P175.0 where the migrations stand. Expect MAX_VERSION 174. 173 means V174 is not applied yet: RUN_NEXT.sql applies it
--        first (its V174 section), then V175. Anything below 173: stop and say so.
SELECT MAX(VERSION) AS MAX_VERSION,
       IFF(MAX(VERSION) >= 174, 'OK: V175 can apply',
           IFF(MAX(VERSION) = 173, 'OK: RUN_NEXT applies V174 first, then V175', 'STOP: older than V173')) AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;

-- P175.1 what the procedure will return: SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA as SNOW_ACCOUNTADMINS (the role
--        that will own the procedure). The app reads only granted_to = USER rows. Expect the 6 users of the
--        2026-10-05 preflight (the 5 named admins and LD8283), unless the role's grants changed since.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SELECT "granted_to", "grantee_name", "granted_by", "created_on"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
ORDER BY "granted_to" DESC, "grantee_name";

-- P175.2 the grantee role exists: V175 grants USAGE on the procedure to SNOW_SYSADMINS (the GRANT fails if it does not).
--        Expect 1 row.
SHOW ROLES LIKE 'SNOW_SYSADMINS';

-- P175.3 the procedure is new (0 rows) or a re-run (1 row, owner SNOW_ACCOUNTADMINS). V175 is idempotent either way.
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, PROCEDURE_OWNER, CREATED, LAST_ALTERED
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_ADMIN_ROLE_MEMBERS';

-- P175.4 no future grant would hand the new procedure to another role. A FUTURE OWNERSHIP grant on procedures in this
--        schema (or this database) makes that role the procedure's owner, and an EXECUTE AS OWNER procedure runs as
--        its owner, so the lookup would answer as that role. Expect 0 rows from the SELECT below. Any row: stop and
--        paste it back before applying.
SHOW FUTURE GRANTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT 'schema' AS LEVEL, "privilege", "grant_on", "grantee_name"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "grant_on" = 'PROCEDURE' AND "privilege" = 'OWNERSHIP';
SHOW FUTURE GRANTS IN DATABASE DBA_MAINT_DB;
SELECT 'database' AS LEVEL, "privilege", "grant_on", "grantee_name"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "grant_on" = 'PROCEDURE' AND "privilege" = 'OWNERSHIP';

ALTER SESSION UNSET TIMEZONE;
