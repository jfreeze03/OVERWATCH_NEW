-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- PART B -- V174 verify (READ-ONLY after the session pin). V174.1 right after the apply; V174.2 after the first hourly
-- scan that started after the apply (its heartbeat lands 55+ minutes after it: a scan already running at the apply
-- finishes on its old body, so V174.2 reads WAIT until then); V174.3 once V174.2 reads OK. Every RESULT should read OK;
-- paste the grids back. APPLIED_AT, LAST_LOAD_TS, LOGGED_AT and RAISED_AT are all Central wall-clock.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

SELECT 'V174.1 SCHEMA_VERSION has 174' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174) = 1,
           'OK', 'FAIL: V174 did not finish') AS RESULT
UNION ALL
SELECT 'V174.1 SP_ALERT_SCAN DDL names SNOW_PRI_GFR_PRD_ALFA_DSA 6 times (three admin lists, three notes)',
       IFF(REGEXP_COUNT(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SNOW_PRI_GFR_PRD_ALFA_DSA') = 6, 'OK',
           'FAIL: not the V174 body (' || REGEXP_COUNT(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SNOW_PRI_GFR_PRD_ALFA_DSA') || ' found)')
UNION ALL
SELECT 'V174.1 SP_ALERT_SCAN DDL has: alert scan v14 (V168:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v14 (V168:'), 'OK', 'FAIL: not the V174 body')
UNION ALL
SELECT 'V174.1 SP_ALERT_SCAN DDL has: FROM recent r', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'FROM recent r'), 'OK', 'FAIL: not the V174 body')
UNION ALL
SELECT 'V174.1 SP_ALERT_SCAN DDL has: V174 (owner 2026-10-05)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'V174 (owner 2026-10-05)'), 'OK', 'FAIL: not the V174 body')
UNION ALL
SELECT 'V174.1 SEC_ADMIN_GRANT rule name lists SNOW_PRI_GFR_PRD_ALFA_DSA',
       (SELECT CASE WHEN COUNT(*) = 0 THEN 'FAIL: no SEC_ADMIN_GRANT rule'
                    WHEN CONTAINS(MAX(NAME), 'SNOW_PRI_GFR_PRD_ALFA_DSA') THEN 'OK'
                    ELSE 'CHECK: the rule name was edited by hand, so V174 kept it (the arm watches the role anyway)' END
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_ADMIN_GRANT');

-- V174.2 after the first hourly scan (graph from :07 Central) whose heartbeat lands 55+ minutes after the apply: it
--        reads 14/14 and none of the three arms V174 changed logged a failure.
SELECT 'V174.2 hourly scan started after the apply' AS CHECK_NAME,
       CASE WHEN (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 174 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)), 'OK',
                                      'WAIT: no hourly scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                          'FAIL: no ALERT_SCAN_HOURLY row') END AS RESULT
UNION ALL
SELECT 'V174.2 hourly heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)),
                            IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row')
UNION ALL
SELECT 'V174.2 no rule_block_failed of the three V174 arms since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
          AND (CONTEXT LIKE 'rule SEC_NEW_ADMIN_NETWORK %'
             OR CONTEXT LIKE 'rule SEC_LOGIN_TAKEOVER %'
             OR CONTEXT LIKE 'rule SEC_ADMIN_GRANT %')
          AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)))
UNION ALL
SELECT 'V174.2 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)));

-- V174.3 once V174.2 reads OK (informational): what the three arms raised since the apply for a user who holds or held
--        SNOW_PRI_GFR_PRD_ALFA_DSA directly. Each row is a real event: review it in Alerts (an approved grant resolves as
--        EXPECTED); none of them auto-declares an incident.
WITH dsa AS (
    SELECT DISTINCT LEFT(GRANTEE_NAME, 200) AS USER_PART
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = 'SNOW_PRI_GFR_PRD_ALFA_DSA'
)
SELECT e.RULE_ID, e.SEVERITY, e.STATUS, e.RAISED_AT, e.TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN dsa d
  ON d.USER_PART = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
WHERE e.RULE_ID IN ('SEC_NEW_ADMIN_NETWORK', 'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
  AND e.RAISED_AT >= (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)
ORDER BY e.RAISED_AT, e.RULE_ID;
