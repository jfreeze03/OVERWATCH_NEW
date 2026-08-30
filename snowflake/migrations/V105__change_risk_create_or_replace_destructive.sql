-- V105__change_risk_create_or_replace_destructive.sql
--
-- CREATE OR REPLACE TABLE is scored DESTRUCTIVE change-risk. The FACT_SECURITY_CHANGE loader
-- classified change-risk purely by QUERY_TYPE, so a CREATE OR REPLACE TABLE (QUERY_TYPE
-- 'CREATE_TABLE'/'CREATE_TABLE_AS_SELECT') that drops and rebuilds a live table fell into the ELSE
-- arm => CHANGE_KIND='CREATE', RISK_SCORE base 30. change_risk_destructive_breakdown and the V088
-- CHANGE RISK exception-queue arm both require CHANGE_KIND='DESTRUCTIVE' AND RISK_SCORE>=70, so a
-- genuinely destructive replace entered neither -- a false all-clear on the destructive-events KPI.
--
-- Re-derives SP_LOAD_SECURITY_FACTS from V100 so BOTH arms (d<=3 OW_QH_EXTRACT + d>3 QUERY_HISTORY
-- backfill) mark a table create whose text contains 'OR REPLACE' as CHANGE_KIND='DESTRUCTIVE' with
-- RISK_SCORE base 55 (same band as ALTER, NOT 90). Base 55 + the existing PROD/admin bumps keeps
-- routine service-role replaces below the 70 queue threshold -- the V080 ETL/service roles that
-- drove the historical destructive flood are never ACCOUNTADMIN/SNOW_ACCOUNTADMINS, so a service
-- replace reaches at most 65; only a PROD replace by an admin role (75) surfaces. CREATE OR REPLACE
-- VIEW is left alone. No schema change; owner applies after V104 and re-runs
-- SP_LOAD_SECURITY_FACTS(90) to re-stamp trailing FACT_SECURITY_CHANGE rows. This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20105, 'V105 requires V104 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 104) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    d INT;
    emsg VARCHAR;
    trust_ok BOOLEAN DEFAULT TRUE;
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 3), 180))::INT;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
     WHERE DAY < DATEADD('day', -180, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
     WHERE DAY < DATEADD('day', -180, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
     WHERE DAY < DATEADD('day', -400, CURRENT_DATE());

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
        (DAY, USER_NAME, COMPANY, CLIENT_IP, AUTH_FACTOR, ERROR_CATEGORY,
         LOGINS, SUCCESSES, FAILURES, FIRST_SEEN, LAST_SEEN)
    SELECT DATE(EVENT_TIMESTAMP),
           COALESCE(USER_NAME, 'UNKNOWN'),
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(COALESCE(USER_NAME, 'UNKNOWN')),
           COALESCE(CLIENT_IP, '(none)'),
           COALESCE(FIRST_AUTHENTICATION_FACTOR, 'UNKNOWN'),
           CASE
             WHEN IS_SUCCESS = 'YES' THEN 'SUCCESS'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%network%' THEN 'NETWORK POLICY'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%disabled%' THEN 'DISABLED USER'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%mfa%' THEN 'MFA'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE ANY ('%password%', '%credential%', '%authentication%')
               THEN 'CREDENTIAL'
             ELSE 'OTHER'
           END,
           COUNT(*),
           COUNT_IF(IS_SUCCESS = 'YES'),
           COUNT_IF(IS_SUCCESS = 'NO'),
           MIN(EVENT_TIMESTAMP),
           MAX(EVENT_TIMESTAMP)
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= DATEADD('day', -:d, CURRENT_DATE())
    GROUP BY 1, 2, 3, 4, 5, 6;

    IF (d <= 3) THEN
        -- V100: the d<=3 change reload reads OW_QH_EXTRACT, which retains only a rolling
        -- ~72h (SP_LOAD_QH_EXTRACT purges START_TIME < now-72h). Deleting the full calendar
        -- window (DAY >= -:d days = up to 72h + hour-of-day) while the extract can refill
        -- only the last ~72h silently dropped the earliest hours of the oldest day, for good.
        -- Delete ONLY the window the extract actually covers, so older already-loaded rows
        -- are preserved instead of erased-and-not-refilled. Empty extract -> MIN() NULL ->
        -- the delete matches nothing (safe no-op).
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
         WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
            (QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
             DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
             RISK_LEVEL, QUERY_PREVIEW)
        WITH raw AS (
            SELECT QUERY_ID, DATE(START_TIME) AS DAY, START_TIME AS EVENT_TS,
                   USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
                   CASE
                     WHEN DATABASE_NAME IS NOT NULL
                      AND DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) <> 'UNKNOWN'
                       THEN DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)
                     ELSE DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME)
                   END AS COMPANY,
                   CASE
                     WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'
                     WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 'PRIVILEGE'
                     WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 'SECURITY POLICY'
                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'
                     WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                          AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'
                     ELSE 'CREATE'
                   END AS CHANGE_KIND,
                   LEAST(100,
                     CASE
                       WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90
                       WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80
                       WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 85
                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55
                       WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                            AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55
                       ELSE 30
                     END
                     + IFF(ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0)
                     + IFF(COALESCE(DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
                   ) AS RISK_SCORE,
                   LEFT(QUERY_TEXT, 200) AS QUERY_PREVIEW
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW',
                   'CREATE_TABLE_AS_SELECT', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN',
                   'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME',
                   'RENAME_TABLE', 'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER',
                   'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE')
                   OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%'
                   OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%'
                   OR QUERY_TYPE ILIKE 'TRUNCATE%')
        )
        SELECT QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
               DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
               CASE WHEN RISK_SCORE >= 90 THEN 'CRITICAL'
                    WHEN RISK_SCORE >= 70 THEN 'HIGH'
                    WHEN RISK_SCORE >= 45 THEN 'MEDIUM' ELSE 'LOW' END,
               QUERY_PREVIEW
        FROM raw;
    ELSE
        -- Full backfill / manual path (d>3): reads ACCOUNT_USAGE.QUERY_HISTORY directly
        -- (full history), so delete the whole calendar window and rebuild it.
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
         WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());
        INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
            (QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
             DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
             RISK_LEVEL, QUERY_PREVIEW)
        WITH raw AS (
            SELECT QUERY_ID, DATE(START_TIME) AS DAY, START_TIME AS EVENT_TS,
                   USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
                   CASE
                     WHEN DATABASE_NAME IS NOT NULL
                      AND DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) <> 'UNKNOWN'
                       THEN DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)
                     ELSE DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME)
                   END AS COMPANY,
                   CASE
                     WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'
                     WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 'PRIVILEGE'
                     WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 'SECURITY POLICY'
                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'
                     WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                          AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'
                     ELSE 'CREATE'
                   END AS CHANGE_KIND,
                   LEAST(100,
                     CASE
                       WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90
                       WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80
                       WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 85
                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55
                       WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                            AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55
                       ELSE 30
                     END
                     + IFF(ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0)
                     + IFF(COALESCE(DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
                   ) AS RISK_SCORE,
                   LEFT(QUERY_TEXT, 200) AS QUERY_PREVIEW
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW',
                   'CREATE_TABLE_AS_SELECT', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN',
                   'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME',
                   'RENAME_TABLE', 'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER',
                   'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE')
                   OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%'
                   OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%'
                   OR QUERY_TYPE ILIKE 'TRUNCATE%')
        )
        SELECT QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
               DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
               CASE WHEN RISK_SCORE >= 90 THEN 'CRITICAL'
                    WHEN RISK_SCORE >= 70 THEN 'HIGH'
                    WHEN RISK_SCORE >= 45 THEN 'MEDIUM' ELSE 'LOW' END,
               QUERY_PREVIEW
        FROM raw;
    END IF;

    BEGIN
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT t
        USING (
            WITH current_findings AS (
                SELECT CURRENT_DATE() AS DAY, SCANNER_ID::VARCHAR AS SCANNER_ID,
                       SCANNER_NAME, UPPER(SEVERITY) AS SEVERITY,
                       TOTAL_AT_RISK_COUNT, CREATED_ON AS SCANNED_AT
                FROM SNOWFLAKE.TRUST_CENTER.FINDINGS
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY SCANNER_ID ORDER BY CREATED_ON DESC
                ) = 1
            ), prior AS (
                SELECT SCANNER_ID, SCANNER_NAME, SEVERITY
                FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY SCANNER_ID ORDER BY DAY DESC, LOAD_TS DESC
                ) = 1
            )
            SELECT DAY, SCANNER_ID, SCANNER_NAME, SEVERITY,
                   TOTAL_AT_RISK_COUNT, SCANNED_AT
            FROM current_findings
            UNION ALL
            SELECT CURRENT_DATE(), p.SCANNER_ID, p.SCANNER_NAME, p.SEVERITY,
                   0, CURRENT_TIMESTAMP()
            FROM prior p
            WHERE NOT EXISTS (
                SELECT 1 FROM current_findings c WHERE c.SCANNER_ID = p.SCANNER_ID
            )
        ) s
        ON t.DAY = s.DAY AND t.SCANNER_ID = s.SCANNER_ID
        WHEN MATCHED THEN UPDATE SET
            SCANNER_NAME = s.SCANNER_NAME, SEVERITY = s.SEVERITY,
            TOTAL_AT_RISK_COUNT = s.TOTAL_AT_RISK_COUNT,
            SCANNED_AT = s.SCANNED_AT, LOAD_TS = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (DAY, SCANNER_ID, SCANNER_NAME, SEVERITY, TOTAL_AT_RISK_COUNT, SCANNED_AT)
        VALUES
            (s.DAY, s.SCANNER_ID, s.SCANNER_NAME, s.SEVERITY,
             s.TOTAL_AT_RISK_COUNT, s.SCANNED_AT);
    EXCEPTION
        WHEN OTHER THEN
            trust_ok := FALSE;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'SecurityLoader', 'trust_snapshot_unavailable', :emsg,
                   'Login/change facts loaded; grant TRUST_CENTER_VIEWER for snapshots',
                   CURRENT_ROLE();
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_SECURITY_LOGIN_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT, 'OK' AS LOAD_STATUS
          FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
        UNION ALL
        SELECT 'FACT_SECURITY_CHANGE', MAX(LOAD_TS), COUNT(*), 'OK'
          FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
        UNION ALL
        SELECT 'SECURITY_TRUST_SNAPSHOT', MAX(LOAD_TS), COUNT(*),
               IFF(:trust_ok, 'OK', 'ERROR')
          FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = s.LOAD_STATUS
    WHEN NOT MATCHED THEN INSERT
        (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS, GENERATION, STATUS)
    VALUES
        (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, CURRENT_TIMESTAMP(), 1, s.LOAD_STATUS);

    RETURN 'security facts loaded ' || d || 'd';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 105 AS VERSION,
       'Change-risk CREATE OR REPLACE destructive: SP_LOAD_SECURITY_FACTS re-derived from V100 so both reload arms mark a table create (CREATE_TABLE / CREATE_TABLE_AS_SELECT) whose text contains OR REPLACE as CHANGE_KIND=DESTRUCTIVE with RISK_SCORE base 55 (ALTER band, not 90). A CREATE OR REPLACE TABLE that wipes a live table now enters the destructive-events breakdown and the RISK>=70 change-risk queue when done by an admin role on a PROD db (55+10+10=75), fixing a false all-clear -- while base 55 keeps routine service-role replaces (never ACCOUNTADMIN/SNOW_ACCOUNTADMINS, so <=65) out of the queue so the V080/V088 de-noise is preserved. CREATE OR REPLACE VIEW untouched. Live recent_ddl_changes given the matching base-55 bump app-side. Proc only, no schema change; owner re-runs SP_LOAD_SECURITY_FACTS(90) to re-stamp.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 105);
