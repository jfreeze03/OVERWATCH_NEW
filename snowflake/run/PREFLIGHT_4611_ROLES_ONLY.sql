-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- ====================================================================================================
--  PREFLIGHT for app 4.611.0 (roles alone decide who is an OVERWATCH admin) -- READ-ONLY, run BEFORE deploying it.
--  From 4.611.0 the hard-coded admin list (H21427, E22292, KEBARR1, CLROY, N22514) is gone: an admin is ONLY a direct
--  USER grantee of SNOW_PRI_GFR_PRD_ALFA_DSA, matched EXACTLY (case included) against the signed-in username. These
--  checks prove nobody who is an admin today silently becomes read-only. Changes nothing. Run in order: R2 reads the
--  SHOW right above it.
-- ====================================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- R1 DSA's direct members, exactly as the app's lookup sees them (granted_to = USER rows only count). Expect the 6
--    users of the 2026-10-05 preflight (the 5 former named admins and LD8283), unless the role's grants changed since.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
SELECT "granted_to", "grantee_name", "granted_by", "created_on"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
ORDER BY "granted_to" DESC, "grantee_name";

-- R2 the exact-case cross-check (no username typed): every viewer the app resolved as a NAMED admin ('allowlist') in
--    the last 90 days must be a direct DSA USER grantee, spelled exactly the same. Run it right after R1's SHOW.
--    PASS = 0 rows. Any row is a person who would become read-only on 4.611.0: grant them the role
--    (GRANT ROLE SNOW_PRI_GFR_PRD_ALFA_DSA TO USER <name>) or confirm they should lose admin.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
WITH dsa AS (SELECT "grantee_name" AS U FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "granted_to" = 'USER')
SELECT u.USER_NAME AS WOULD_LOSE_ADMIN, MAX(u.AT) AS LAST_SEEN_AS_NAMED_ADMIN
FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE u
LEFT JOIN dsa d ON d.U = u.USER_NAME
WHERE u.EVENT_KIND = 'access_resolved'
  AND u.SECTION = 'allowlist'
  AND u.AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
  AND d.U IS NULL
GROUP BY u.USER_NAME
ORDER BY 1;

-- R2b the five former named admins by name (covers anyone who has not opened the app in 90 days). Run it right after
--     its SHOW. EXACT_MEMBER must be TRUE for each one who should stay an admin. CASE_ONLY_MATCH TRUE means the grant
--     exists but the spelling differs in case: the app's match is exact, so check how that user signs in.
SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
WITH dsa AS (SELECT "grantee_name" AS U FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "granted_to" = 'USER'),
     former (NAME) AS (SELECT * FROM VALUES ('H21427'), ('E22292'), ('KEBARR1'), ('CLROY'), ('N22514'))
SELECT f.NAME AS FORMER_NAMED_ADMIN,
       COUNT_IF(d.U = f.NAME) > 0 AS EXACT_MEMBER,
       COUNT_IF(d.U = f.NAME) = 0 AND COUNT(d.U) > 0 AS CASE_ONLY_MATCH
FROM former f
LEFT JOIN dsa d ON UPPER(d.U) = UPPER(f.NAME)
GROUP BY f.NAME
ORDER BY 1;

-- R3 the same people who reached the app as admins by ROLE in the last 90 days (for comparison; informational).
SELECT u.SECTION AS DECIDED_BY, COUNT(DISTINCT u.USER_NAME) AS VIEWERS, MAX(u.AT) AS LAST_SEEN
FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE u
WHERE u.EVENT_KIND = 'access_resolved'
  AND u.AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
GROUP BY 1
ORDER BY 1;

ALTER SESSION UNSET TIMEZONE;
