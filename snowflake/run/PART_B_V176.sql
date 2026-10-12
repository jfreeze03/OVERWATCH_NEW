-- PART B -- V176 verify (READ-ONLY after the session pin; as SNOW_ACCOUNTADMINS). V176.1 right after the apply; V176.2
-- after the first hourly scan that started after the apply (its heartbeat lands 55+ minutes after it: a scan already
-- running at the apply finishes on its old body, so V176.2 reads WAIT until then); V176.3 about 3h after the next app
-- deploy (GRANTS_TO_USERS lags up to ~2h, then the next hourly scan). Every RESULT should read OK; paste the grids back.
-- APPLIED_AT, LAST_LOAD_TS, LOGGED_AT and RAISED_AT are all Central wall-clock.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

SELECT 'V176.1 SCHEMA_VERSION has 176' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176) = 1,
           'OK', 'FAIL: V176 did not finish') AS RESULT
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL names STPLATSTREAMLIT 3 times (rt CTE, skip predicate, note)',
       IFF(REGEXP_COUNT(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'STPLATSTREAMLIT') = 3, 'OK',
           'FAIL: not the V176 body (' || REGEXP_COUNT(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'STPLATSTREAMLIT') || ' found)')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: STPLATSTREAMLIT[0-9]+', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'STPLATSTREAMLIT[0-9]+'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: rt.GRANTEE_NAME IS NOT NULL', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'rt.GRANTEE_NAME IS NOT NULL'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: SYSTEM$MANAGED', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SYSTEM$MANAGED'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: V176 (owner 2026-10-12)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'V176 (owner 2026-10-12)'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: alert scan v14 (V168:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v14 (V168:'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL has: V174 (owner 2026-10-05)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'V174 (owner 2026-10-05)'), 'OK', 'FAIL: not the V176 body')
UNION ALL
SELECT 'V176.1 SEC_ADMIN_GRANT is enabled',
       (SELECT CASE WHEN COUNT(*) = 0 THEN 'FAIL: no SEC_ADMIN_GRANT rule'
                    WHEN COUNT_IF(ENABLED) > 0 THEN 'OK'
                    ELSE 'FAIL: the rule is disabled, so real admin grants raise nothing: re-enable it in Alerts > Rules' END
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_ADMIN_GRANT');

-- V176.2 after the first hourly scan (graph from :07 Central) whose heartbeat lands 55+ minutes after the apply: it
--        reads 14/14 and the arm V176 changed logged no failure.
SELECT 'V176.2 hourly scan started after the apply' AS CHECK_NAME,
       CASE WHEN (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 176 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)), 'OK',
                                      'WAIT: no hourly scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                          'FAIL: no ALERT_SCAN_HOURLY row') END AS RESULT
UNION ALL
SELECT 'V176.2 hourly heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)),
                            IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row')
UNION ALL
SELECT 'V176.2 no rule_block_failed of SEC_ADMIN_GRANT since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT ERROR_MESSAGE, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
          AND CONTEXT LIKE 'rule SEC_ADMIN_GRANT %'
          AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)))
UNION ALL
SELECT 'V176.2 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)));

-- V176.3 about 3h after the next app deploy (the live test; WAIT until one is on record): the deploy's runtime grant is
--        on record with no grantor, carries the SYSTEM$MANAGED co-grant and raised NO SEC_ADMIN_GRANT event. The
--        last row is the positive control: every admin grant V176 still raises, made since the apply and 4h or older
--        (GRANTS_TO_USERS can land ~2h late and the hourly read varies), DID raise one (a person's grant to a runtime
--        user included). A FAIL that persists on a re-run an hour later, with the rule enabled, is a real miss.
WITH ag AS (
    SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
           ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
           CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
),
rt AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = 'SYSTEM$MANAGED'
      AND GRANTED_BY IS NULL
      AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')
    GROUP BY GRANTEE_NAME
),
dep AS (
    SELECT DISTINCT GRANTEE_NAME, CREATED_ON
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE GRANTED_BY IS NULL
      AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')
      AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
      AND CREATED_ON >= (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)
),
missed AS (
    SELECT g.GRANTEE_NAME || ' (' || g.ROLE || ')' AS GRANT_NAME,
           'SEC_ADMIN_GRANT|' || LEFT(g.GRANTEE_NAME, 200) || '|' || g.ROLE || '|' ||
           TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI:SS.FF3') AS DEDUPE_KEY
    FROM ag g
    LEFT JOIN rt
      ON rt.GRANTEE_NAME = g.GRANTEE_NAME
     AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)
    WHERE g.CREATED_ON >= (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)
      AND g.CREATED_ON <= DATEADD('hour', -4, CURRENT_TIMESTAMP())
      AND NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+') AND (g.ROLE = 'SNOW_ACCOUNTADMINS' OR g.ROLE = 'SNOW_SYSADMINS') AND rt.GRANTEE_NAME IS NOT NULL, FALSE)
)
SELECT 'V176.3 a deploy since the apply is on record' AS CHECK_NAME,
       CASE WHEN (SELECT COUNT(*) FROM dep) = 0
            THEN 'WAIT: no deploy since the apply on record yet (GRANTS_TO_USERS lags up to ~2h)'
            ELSE 'OK (' || (SELECT COUNT(DISTINCT GRANTEE_NAME) FROM dep) || ' runtime user(s))' END AS RESULT
UNION ALL
SELECT 'V176.3 each runtime user since the apply has the SYSTEM$MANAGED co-grant',
       CASE WHEN (SELECT COUNT(*) FROM dep) = 0
            THEN 'WAIT: no deploy since the apply on record yet (GRANTS_TO_USERS lags up to ~2h)'
            ELSE (SELECT IFF(COUNT(*) = 0, 'OK',
                             'CHECK: ' || LISTAGG(DISTINCT g.GRANTEE_NAME, ', ') || ' lack it within 60 s of the '
                             || 'admin grant, so SEC_ADMIN_GRANT still raises them; re-check in 2h (the co-grant '
                             || 'row may land later), else the runtime grant changed shape')
                  FROM dep g
                  LEFT JOIN rt
                    ON rt.GRANTEE_NAME = g.GRANTEE_NAME
                   AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)
                  WHERE rt.GRANTEE_NAME IS NULL) END
UNION ALL
SELECT 'V176.3 no SEC_ADMIN_GRANT event for a no-grantor runtime grant since the apply',
       CASE WHEN (SELECT COUNT(*) FROM dep) = 0
            THEN 'WAIT: no deploy since the apply on record yet (GRANTS_TO_USERS lags up to ~2h)'
            ELSE (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT TITLE, '; '))
                  FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                  WHERE RULE_ID = 'SEC_ADMIN_GRANT'
                    AND REGEXP_LIKE(SPLIT_PART(DEDUPE_KEY, '|', 2), 'STPLATSTREAMLIT[0-9]+')
                    AND SPLIT_PART(DEDUPE_KEY, '|', 3) IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
                    AND DETAIL LIKE 'Granted % Central by ?. %'
                    AND RAISED_AT >= DATEADD('minute', 30, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176))) END
UNION ALL
SELECT 'V176.3 every other admin grant since the apply raised',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: no event for ' || LISTAGG(DISTINCT m.GRANT_NAME, ', '))
        FROM missed m
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          ON e.DEDUPE_KEY = m.DEDUPE_KEY
        WHERE e.DEDUPE_KEY IS NULL);
