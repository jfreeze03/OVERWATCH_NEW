-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- PART B -- V173 verify (READ-ONLY after the session pin). V173.1 right after the apply; V173.2 after the next hourly
-- scan; V173.3 after the next 06:50 Central daily scan; V173.4 once V173.2 reads OK. Every RESULT should read OK (a
-- WAIT means no scan that started after the apply has finished yet); paste the grids back. APPLIED_AT, LAST_LOAD_TS
-- and LOGGED_AT are all Central wall-clock. A scan already running at the apply finishes on its old body, so V173.2
-- and V173.3 count only a heartbeat 55+ minutes after the apply and failures logged from 30 minutes after it: a scan
-- that started within about half an hour of the apply reads WAIT, so re-run the grid after the next one.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

SELECT 'V173.1 SCHEMA_VERSION has 173' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173) = 1,
           'OK', 'FAIL: V173 did not finish') AS RESULT
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN DDL has: AS KEY_HEAD', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'AS KEY_HEAD'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN DDL has: FROM recent r', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'FROM recent r'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN DDL has: r.KEY_LEN = LENGTH(b.DEDUPE_KEY)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'r.KEY_LEN = LENGTH(b.DEDUPE_KEY)'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN DDL has: alert scan v14 (V168:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v14 (V168:'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN DDL lacks: OR (e.RULE_ID = b.RULE_ID', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'OR (e.RULE_ID = b.RULE_ID'), 'OK', 'FAIL: still the pre-V173 text')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN_DAILY DDL has: NULLIF(i.TOTAL_CREDITS, 0)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'NULLIF(i.TOTAL_CREDITS, 0)'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN_DAILY DDL has: NULLIF(s.COVERED_DAYS, 0)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'NULLIF(s.COVERED_DAYS, 0)'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN_DAILY DDL has: alert scan daily v6 (V169:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'alert scan daily v6 (V169:'), 'OK', 'FAIL: not the V173 body')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN_DAILY DDL lacks: i.IDLE_CREDITS / i.TOTAL_CREDITS', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'i.IDLE_CREDITS / i.TOTAL_CREDITS'), 'OK', 'FAIL: still the pre-V173 text')
UNION ALL
SELECT 'V173.1 SP_ALERT_SCAN_DAILY DDL lacks: :credit_price / s.COVERED_DAYS', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), ':credit_price / s.COVERED_DAYS'), 'OK', 'FAIL: still the pre-V173 text');

-- V173.2 after the first hourly scan (graph from :07 Central) whose heartbeat lands 55+ minutes after the apply (within
--        about two hours): it reads 14/14, arm [18] logged no failure, and the V168 supersede sweep (its OR shape has
--        run only since V168's apply) logged no supersede_sweep_failed.
SELECT 'V173.2 hourly scan started after the apply' AS CHECK_NAME,
       CASE WHEN (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 173 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)), 'OK',
                                      'WAIT: no hourly scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                          'FAIL: no ALERT_SCAN_HOURLY row') END AS RESULT
UNION ALL
SELECT 'V173.2 hourly heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)),
                            IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row')
UNION ALL
SELECT 'V173.2 no SEC_NEW_ADMIN_NETWORK rule_block_failed since the apply',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
               AND CONTEXT LIKE 'rule SEC_NEW_ADMIN_NETWORK %'
               AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173))) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE')
UNION ALL
SELECT 'V173.2 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)))
UNION ALL
SELECT 'V173.2 no supersede_sweep_failed since the apply',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'supersede_sweep_failed'
               AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173))) = 0,
           'OK', 'FAIL: the V168 supersede sweep failed, see APP_ERROR_LOG ERROR_MESSAGE');

-- V173.3 after the first daily scan (06:50 Central) whose heartbeat lands 55+ minutes after the apply (apply before
--        about 05:50 to read it the same morning): it reads 14/14 and arm [24] logged no failure.
SELECT 'V173.3 daily scan started after the apply' AS CHECK_NAME,
       CASE WHEN (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 173 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)), 'OK',
                                      'WAIT: no daily scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                          'FAIL: no ALERT_SCAN_DAILY row') END AS RESULT
UNION ALL
SELECT 'V173.3 daily heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)),
                            IFF(MAX(STATUS) = 'alert scan daily 14/14 rule blocks ok (daily)', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                'FAIL: no ALERT_SCAN_DAILY row')
UNION ALL
SELECT 'V173.3 no COST_IDLE_OPPORTUNITY rule_block_failed since the apply',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
               AND CONTEXT LIKE 'rule COST_IDLE_OPPORTUNITY %'
               AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173))) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE')
UNION ALL
SELECT 'V173.3 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)));

-- V173.4 once V173.2 reads OK: the admin user + IP pairs arm [18] will never raise. First seen (against the 90-day
--        baseline) from 24h before V168's apply, no SEC_NEW_ADMIN_NETWORK event for the user + IP, at or over the rule's
--        THRESHOLD_NUM with the rule enabled (the arm's own join), and now past its 24h window. Expect no rows;
--        review any by hand in Security > Access (login history).
WITH nn AS (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
                   COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
                   MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
            JOIN (
                SELECT DISTINCT GRANTEE_NAME
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
            ) A ON A.GRANTEE_NAME = L.USER_NAME
            WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
            GROUP BY 1, 2
            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))
        ),
ev AS (
    SELECT DISTINCT SPLIT_PART(DEDUPE_KEY, '|', 2) AS USER_PART, SPLIT_PART(DEDUPE_KEY, '|', 3) AS IP_PART
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
      AND RAISED_AT >= DATEADD('day', -2, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))
)
SELECT nn.USER_NAME, nn.CLIENT_IP, nn.FIRST_SEEN, nn.LOGINS, nn.SUCCESSES
FROM nn
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
  ON c.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
 AND c.ENABLED
 AND nn.LOGINS >= c.THRESHOLD_NUM
LEFT JOIN ev
       ON ev.USER_PART = LEFT(nn.USER_NAME, 200)
      AND ev.IP_PART = nn.CLIENT_IP
WHERE ev.USER_PART IS NULL
  AND nn.FIRST_SEEN < DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY nn.FIRST_SEEN;
