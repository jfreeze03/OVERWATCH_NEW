-- =====================================================================================
--  PROBES_NEXT_FIFTY_WAVE1_R2.sql  --  round 2 of the Next-Fifty wave-1 probes (2026-09-24)
--  READ-ONLY. No DDL/DML on any OVERWATCH object (the one remediation CALL at the bottom stays
--  commented until R3 says to run it). Separate from RUN_NEXT.sql -- the pending V148 -> V149 ->
--  V150 there are untouched.
--
--  Why a round 2:
--    * round 1 P1 (2) and (5) failed on MY SQL (reserved alias SAMPLE; NOTIFICATION_HISTORY takes
--      START_TIME, not START_TIME_RANGE_START) -- both fixed here and in the app (PR #29);
--    * round 1 P3 (#9 identity) was shipped EMPTY -- the queries are below;
--    * round 1 P4 (C2/C3) stopped 3-4 days short of today, so it could not tell "the new V146 arm
--      never loaded" from "the 365-day history reload never ran". R3 settles it.
--  Round 1 P2 (#7 tagging) is settled: statement_params tags + timeouts both work -> no rerun.
--  Paste back every grid (an empty grid is an answer too -- say "no rows").
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- -------------------------------------------------------------------------------------
-- R1 (#4) Email dead-man pre-flight -- the two fixed queries + the one not shown last time.
-- -------------------------------------------------------------------------------------
-- (1) sources that would email now (expect NO ROWS before resuming the new dead-man alerts)
SELECT SOURCE_NAME, LAST_LOAD_TS,
       ROUND(DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0, 1) AS HOURS_BEHIND,
       IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIMIT_H
  FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE LAST_LOAD_TS IS NULL
    OR DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0
       > IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0)
 ORDER BY 3 DESC;

-- (2) chronic loader / delivery failure types, last 24h (was: AS SAMPLE -> reserved word)
SELECT ERROR_TYPE, PAGE, COUNT(*) AS N_24H, MAX(LOGGED_AT) AS LAST_AT,
       ANY_VALUE(LEFT(ERROR_MESSAGE, 160)) AS SAMPLE_MSG
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
   AND (ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                       'cloud_svc_mart_failed', 'object_cost_load_failed')
        OR PAGE = 'NotifyWebhook')
 GROUP BY 1, 2 ORDER BY 3 DESC;

-- (5) NOTIFICATION_HISTORY columns + STATUS values the in-app 'Email path' row reads
--     (was: START_TIME_RANGE_START -> "invalid argument"). Expect columns incl. CREATED, STATUS,
--     ERROR_MESSAGE; note every distinct STATUS value you see.
SELECT *
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
         START_TIME => DATEADD('day', -14, CURRENT_TIMESTAMP()),
         INTEGRATION_NAME => 'OVERWATCH_EMAIL',
         RESULT_LIMIT => 20));

-- -------------------------------------------------------------------------------------
-- R2 (#9) Identity auth readiness -- do the columns the new Security panel reads resolve?
--    (a) runs the app's EXACT inventory SQL, aggregated so no names/emails print.
--    (b) the USERS.TYPE mix.  (c)+(d) where network policies are attached.
-- -------------------------------------------------------------------------------------
-- (a) the app's user_auth_inventory('ALL'), wrapped -- an error here = the panel reads 'needs setup'
SELECT COUNT(*)                                 AS ROWS_RETURNED,
       MAX(TOTAL_CANDIDATES)                    AS TOTAL_CANDIDATES,
       MAX(ADMIN_PW_NO_MFA_TOTAL)               AS ADMIN_PW_NO_MFA_TOTAL,
       COUNT_IF(USER_TYPE IS NULL)              AS TYPE_UNSET,
       COUNT_IF(USER_TYPE = 'LEGACY_SERVICE')   AS LEGACY_SERVICE,
       COUNT_IF(HAS_RSA_PUBLIC_KEY)             AS WITH_KEY_PAIR,
       COUNT_IF(HAS_PASSWORD AND NOT HAS_MFA)   AS PW_WITHOUT_MFA,
       COUNT_IF(PASSWORD_LOGINS_30D IS NULL)    AS NO_30D_LOGIN_EVIDENCE
FROM (
WITH password_logins AS (
    SELECT USER_NAME,
           SUM(PASSWORD_LOGINS) AS PASSWORD_LOGINS_30D,
           MAX(IFF(PASSWORD_LOGINS > 0, DAY, NULL)) AS LAST_PASSWORD_LOGIN
    FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
    WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
    GROUP BY USER_NAME
), admin_grants AS (
    SELECT GRANTEE_NAME,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ADMIN_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN')
    GROUP BY GRANTEE_NAME
), inv AS (
    SELECT
        U.NAME AS USER_NAME,
        U.LOGIN_NAME,
        U.EMAIL,
        NULLIF(UPPER(TRIM(U.TYPE)), '') AS USER_TYPE,
        COALESCE(U.HAS_PASSWORD, FALSE) AS HAS_PASSWORD,
        COALESCE(U.HAS_MFA, FALSE) AS HAS_MFA,
        COALESCE(U.HAS_RSA_PUBLIC_KEY, FALSE) AS HAS_RSA_PUBLIC_KEY,
        U.LAST_SUCCESS_LOGIN,
        PL.PASSWORD_LOGINS_30D,
        PL.LAST_PASSWORD_LOGIN,
        A.GRANTEE_NAME IS NOT NULL AS IS_ADMIN,
        A.ADMIN_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
    LEFT JOIN password_logins PL ON PL.USER_NAME = U.NAME
    LEFT JOIN admin_grants A ON A.GRANTEE_NAME = U.NAME
    WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE AND (COALESCE(U.HAS_PASSWORD, FALSE) = TRUE OR UPPER(COALESCE(U.TYPE, '')) = 'LEGACY_SERVICE' OR A.GRANTEE_NAME IS NOT NULL)
)
SELECT inv.*,
       COUNT(*) OVER () AS TOTAL_CANDIDATES,
       SUM(IFF(IS_ADMIN AND HAS_PASSWORD AND NOT HAS_MFA, 1, 0)) OVER () AS ADMIN_PW_NO_MFA_TOTAL
FROM inv
ORDER BY IFF(USER_TYPE = 'LEGACY_SERVICE', 0, 1), IFF(HAS_PASSWORD AND NOT HAS_MFA, 0, 1),
         IS_ADMIN DESC, COALESCE(PASSWORD_LOGINS_30D, 0) DESC, USER_NAME
LIMIT 1000
);

-- (b) USERS.TYPE mix across enabled users (PERSON / SERVICE / LEGACY_SERVICE / unset)
SELECT COALESCE(NULLIF(UPPER(TRIM(TYPE)), ''), '(unset)')                  AS USER_TYPE,
       COUNT(*)                                                           AS ENABLED_USERS,
       COUNT_IF(COALESCE(HAS_PASSWORD, FALSE))                            AS WITH_PASSWORD,
       COUNT_IF(COALESCE(HAS_PASSWORD, FALSE) AND NOT COALESCE(HAS_MFA, FALSE)) AS PW_WITHOUT_MFA,
       COUNT_IF(COALESCE(HAS_RSA_PUBLIC_KEY, FALSE))                      AS WITH_KEY_PAIR
  FROM SNOWFLAKE.ACCOUNT_USAGE.USERS
 WHERE DELETED_ON IS NULL AND COALESCE(DISABLED, FALSE) = FALSE
 GROUP BY 1 ORDER BY 2 DESC;

-- (c) network-policy attachments by domain. USER = 0 while you KNOW an admin has a user-level
--     policy means POLICY_REFERENCES does not carry them here (the panel then says 'needs setup').
SELECT UPPER(REF_ENTITY_DOMAIN) AS REF_DOMAIN, COUNT(*) AS REFS, COUNT(DISTINCT POLICY_NAME) AS POLICIES
  FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
 WHERE POLICY_KIND = 'NETWORK_POLICY'
 GROUP BY 1 ORDER BY 2 DESC;

-- (d) account-level network policy (context for (c))
SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN ACCOUNT;

-- -------------------------------------------------------------------------------------
-- R3 (#25) V146: is the new AI-Functions loader arm WORKING (so only the history reload is
--    missing), or FAILING? Round 1 showed the fact has ~no 'Functions' credits before 09-20.
-- -------------------------------------------------------------------------------------
-- (1) last 10 days INCLUDING the 3 the nightly load re-touches. Recent days FACT ~= LIVE -> the arm
--     works; FACT null on every day -> the arm is failing (see (2)).
WITH f AS (
    SELECT DAY, SUM(CREDITS) AS FACT_CREDITS, SUM(REQUESTS) AS FACT_REQUESTS, MAX(LOAD_TS) AS LAST_LOAD_TS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE = 'Functions'
      AND DAY >= DATEADD('day', -10, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
    GROUP BY DAY
),
l AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY,
           SUM(COALESCE(CREDITS, 0)) AS LIVE_CREDITS, COUNT(*) AS LIVE_REQUESTS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
    WHERE START_TIME >= DATEADD('day', -11, CURRENT_TIMESTAMP())
    GROUP BY 1
)
SELECT COALESCE(f.DAY, l.DAY) AS DAY,
       ROUND(f.FACT_CREDITS, 6) AS FACT_CREDITS, ROUND(l.LIVE_CREDITS, 6) AS LIVE_CREDITS,
       f.FACT_REQUESTS, l.LIVE_REQUESTS, f.LAST_LOAD_TS
FROM f FULL OUTER JOIN l ON l.DAY = f.DAY
WHERE COALESCE(f.DAY, l.DAY) >= DATEADD('day', -10, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
ORDER BY 1;

-- (2) has the Functions arm logged a failure since V146 was applied? (any row = the arm is broken ->
--     paste the message; do NOT run the remediation)
SELECT LOGGED_AT, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS ERROR_MSG
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE ERROR_TYPE = 'mart_load_failed'
   AND CONTEXT ILIKE 'FACT_AI_USAGE_DAILY%'
   AND LOGGED_AT >= (SELECT APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 146)
 ORDER BY LOGGED_AT DESC
 LIMIT 20;

-- (3) freshness stamp: FACT_AI_USAGE_DAILY is stamped ONLY when BOTH AI arms load, so a SNAPSHOT_TS
--     older than ~a day with (2) empty means the stamp is stuck for another reason (paste it).
SELECT SOURCE_NAME, LAST_LOAD_TS, SNAPSHOT_TS, STATUS, GENERATION
  FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE SOURCE_NAME = 'FACT_AI_USAGE_DAILY';

-- REMEDIATION -- ONLY if (1) shows the recent days loaded AND (2) is empty (= the arm works; only
-- the 365-day history reload never ran). Round 1 C2 found 0 stale frozen-view rows, so NO purge is
-- needed -- the loader MERGE upserts. Uncomment and run both lines in ONE worksheet session (the
-- Chicago session TZ keys the backfill's days exactly like the nightly task), then re-run (1):
-- ALTER SESSION SET TIMEZONE = 'America/Chicago';
-- CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
