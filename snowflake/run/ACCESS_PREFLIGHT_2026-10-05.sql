-- =====================================================================
--  OVERWATCH -- ACCESS_PREFLIGHT_2026-10-05.sql  (READ-ONLY: SHOW commands only; nothing is granted or changed)
--  Before OVERWATCH gives SNOW_PRI_GFR_PRD_ALFA_DSA admin rights and SNOW_PRI_GFR_PRD_ALFA_DTI view rights,
--  these reads check that the app (running as SNOW_ACCOUNTADMINS) can see who is in DSA, and what the new
--  database/schema USAGE grants would expose. Paste back every grid (an empty grid is an answer: 'no rows');
--  an error is that block's answer -- copy it and run from the next block.
--  The actual grants come later, with app 4.610.0, in roles.sql. Do not grant anything from this file.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE SECONDARY ROLES NONE;   -- match the app: it runs with the owner role's hierarchy only

-- (P1) THE KEY CHECK. The app will run exactly this to decide who is an admin. It must SUCCEED and list one
--      row per DSA member with granted_to = USER. An EMPTY result means SNOW_ACCOUNTADMINS cannot see the role
--      (a privilege gap), not that DSA has no members. granted_to = ROLE rows are nested grants: those people
--      will NOT become admins (direct grants only, your choice).
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;

-- (P1-DTI) For your records only (the app does not look DTI up: any signed-in non-admin gets the view pages).
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;

-- (P1b) Why P1 works (or not): look for ACCOUNTADMIN / MANAGE GRANTS in the owner role's hierarchy.
SHOW GRANTS TO ROLE SNOW_ACCOUNTADMINS;

-- (P6) Who controls DSA membership = who can create OVERWATCH admins from now on: the "owner" column.
SHOW ROLES LIKE 'SNOW_PRI_GFR_PRD_ALFA_D%';

-- (P2) Managed access? If "options" says MANAGED ACCESS, only the schema owner or a MANAGE GRANTS role can grant
--      USAGE on the app.
SHOW SCHEMAS LIKE 'OVERWATCH' IN DATABASE DBA_MAINT_DB;

-- (P3) What USAGE on DBA_MAINT_DB / OVERWATCH would newly expose: objects already granted to these roles or to
--      PUBLIC, and FUTURE grants that would expose objects created later. Expect nothing OVERWATCH-specific.
SHOW GRANTS TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SHOW GRANTS TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;
SHOW GRANTS TO ROLE PUBLIC;
SHOW FUTURE GRANTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SHOW FUTURE GRANTS IN DATABASE DBA_MAINT_DB;

-- (P4) Who can open the app today. Expect USAGE for SNOW_ACCOUNTADMINS and SNOW_SYSADMINS, OWNERSHIP for
--      SNOW_ACCOUNTADMINS.
SHOW GRANTS ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;

-- (P5) OPTIONAL: replace the two names with one real DSA member and one DTI member (from P1), then run. The
--      "name" column must equal P1's grantee_name (that is what the app sees as the signed-in user), and
--      default_role / default_secondary_roles show whether the role is active when they open Snowsight.
-- SHOW USERS LIKE 'ONE_DSA_MEMBER_USERNAME';
-- SHOW USERS LIKE 'ONE_DTI_MEMBER_USERNAME';

USE SECONDARY ROLES ALL;   -- back to the usual worksheet setting (change it if yours differs)
