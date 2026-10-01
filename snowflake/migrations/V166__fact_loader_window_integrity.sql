-- V166__fact_loader_window_integrity.sql
--
-- WHY: four fact loaders could lose or understate history that no later run ever reloaded.
--   R2-007  The hourly security change reload (d <= 3) deleted from the first row of OW_QH_EXTRACT but
--           re-read only from midnight(today - d). When the extract reached further back -- a swallowed
--           extract failure across midnight keeps its old untrimmed fill, backfill_365 widens it to 90
--           days, a manual DAYS_BACK of 0/1/2 -- the rows in between were deleted, never re-read, and
--           then trimmed out of the 72h extract for good: holes in the CHANGE RISK queue, the destructive
--           breakdown and Who changed what, below the day-grain coverage check of the app.
--   R2-009  FACT_STORAGE_DAILY averaged DATABASE_STORAGE_USAGE_HISTORY per name-day. The view has one row
--           per DATABASE_ID, and a re-created or clone-refreshed database keeps its dropped IDs (Time Travel
--           and fail-safe bytes) under the same name, so the fact divided the billed bytes by the row count.
--           The live twin (storage_by_database_calendar_live) already SUMs, so mart and live disagreed.
--   R2-011  SP_LOAD_APP_COST and SP_LOAD_STORAGE_TRUTH ran DELETE then INSERT under autocommit. A failed
--           INSERT left the trailing window deleted; the next run starts a day later, so the oldest
--           deleted day was never reloaded (the v4.371.0 deferral assumed it self-healed; it does not).
--   C10     SP_LOAD_APP_COST resolved sessions only 7 days before its reload start. The task reloads each
--           day four times and the LAST reload is the narrowest, so a query in a keep-alive, pooled or
--           service session opened 7-10 days earlier was relabelled (unknown) for good.
--
--   ~ SP_LOAD_SECURITY_FACTS re-derived from V105, byte-identical except: the d <= 3 DELETE and INSERT
--     share ONE bound, lo_ts = GREATEST(MIN(extract START_TIME), midnight(today - d)). GREATEST propagates
--     NULL, so an empty extract stays a no-op. The d > 3 arm and the DAYS_BACK clamp are unchanged.
--   ~ SP_LOAD_DAILY_FACTS re-derived from V101, byte-identical except: the FACT_STORAGE_DAILY arm SUMs
--     AVERAGE_DATABASE_BYTES / AVERAGE_FAILSAFE_BYTES per name-day. The V101 task_attempts CTE stays.
--   ~ SP_LOAD_APP_COST re-derived from V077, byte-identical except: the DELETE + INSERT run in ONE
--     transaction (ROLLBACK, an APP_ERROR_LOG fact_load_failed row with PAGE AppCost, re-RAISE so the task
--     still reads FAILED), and the SESSIONS lookback is 30 days before the reload start, not 7. The
--     (unknown) label stays (SP_SCAN_SLEEP_POLLING and cloud_svc_billed_families filter on it).
--   ~ SP_LOAD_STORAGE_TRUTH re-derived from V046, byte-identical except the same one-transaction wrap
--     (PAGE StorageTruth). In both, the SOURCE_FRESHNESS_STATE MERGE stays after the COMMIT.
--   + one bounded repair: FACT_STORAGE_DAILY name-days that the view still holds as 2+ DATABASE_ID rows
--     (365-day retention) get the SUM. Older rows cannot be recomputed and stay understated.
--
-- COST: the daily TASK_LOAD_APP_COST CALL(3) now scans 33 days of SESSIONS instead of 10 (small next to
-- its QUERY_HISTORY and QUERY_ATTRIBUTION_HISTORY scans; watch its first scheduled run in TASK_HISTORY). The
-- other loaders scan what they scanned before. The repair reads about 365 days of
-- DATABASE_STORAGE_USAGE_HISTORY once (a few thousand rows) and updates only the multi-ID name-days.
-- LATENCY: unchanged; the procs swap under the running graph (no task change, no suspend).
-- FIRST RUN: the next hourly SP_LOAD_SECURITY_FACTS(3), then the daily runs: storage truth 06:30 CT,
-- SP_LOAD_DAILY_FACTS() 06:45 CT (TASK_LOAD_DAILY, then the nightly reconcile), app cost 06:55 CT. Apply
-- outside 06:30-07:15 CT so no run straddles the swap. Nothing runs at apply time except the storage repair. Owner-run
-- heals, in a Central session after V166 is applied (OWNER_REPAIRS): SP_LOAD_SECURITY_FACTS(180) at about
-- :35 past the hour, the R2-011 gap grids, then SP_LOAD_STORAGE_TRUTH(N) only when they show holes and ONE
-- off-peak SP_LOAD_APP_COST of at least 30 days (relabels sessions, fills any R2-011 hole, atomic now).
-- ROLLBACK: re-run the base CREATE PROCEDURE of each proc (V105, V101, V077, V046). The repaired storage
-- rows can stay: they are what Snowflake bills, and the live twin already shows them.
-- Apply AFTER V165. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20166, 'V166 requires V165 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 165) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_SECURITY_FACTS  (from V105; d<=3 change reload DELETE + INSERT share one bound GREATEST(MIN(extract.START_TIME), midnight(today-d)), V166)
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
    lo_ts TIMESTAMP_LTZ;        -- V166 (R2-007): the d<=3 change reload lower bound, DELETE and INSERT
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
        -- V166 (R2-007): the DELETE and the INSERT share ONE lower bound, the later of the extract
        -- first row and midnight(today - d). V100 deleted from MIN(START_TIME) alone while the INSERT
        -- re-read only START_TIME >= today - d, so whenever the extract reached further back (a
        -- swallowed extract failure across midnight keeps the old untrimmed fill; a backfill-wide
        -- extract; a manual d of 1 or 2, DAYS_BACK 0 maps to 1) the rows in between were deleted,
        -- never re-read, then trimmed out of the extract for good. GREATEST() propagates NULL: an
        -- empty extract leaves lo_ts NULL and both statements match nothing (the V100 no-op is kept).
        lo_ts := (SELECT GREATEST(MIN(START_TIME), DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_LTZ)
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
         WHERE EVENT_TS >= :lo_ts;
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
            WHERE START_TIME >= :lo_ts
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

-- >>> derived:SP_LOAD_DAILY_FACTS  (from V101; FACT_STORAGE_DAILY arm AVG -> SUM per name-day, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    wm_metering TIMESTAMP_NTZ;  -- V064 rec7: per-source watermarks (was one shared DAILY_FACTS mark)
    wm_task TIMESTAMP_NTZ;
    wm_login TIMESTAMP_NTZ;
    wm_storage TIMESTAMP_NTZ;
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_task TIMESTAMP_NTZ;      -- watermark - 1d overlap (default -3d, clamp -30d)
    lo_login TIMESTAMP_NTZ;
    lo_storage TIMESTAMP_NTZ;
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run (return string)
BEGIN
    -- V064 rec7: each daily source keeps its OWN watermark so a per-table
    -- failure holds only THAT source's mark; siblings advance independently
    -- (no whole-group re-read of the costliest source on any one failure).
    SELECT MAX(WM_TS) INTO :wm_metering
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_METERING_DAILY';
    SELECT MAX(WM_TS) INTO :wm_task
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_TASK_DAILY';
    SELECT MAX(WM_TS) INTO :wm_login
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_LOGIN_DAILY';
    SELECT MAX(WM_TS) INTO :wm_storage
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_STORAGE_DAILY';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm_metering),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_task := GREATEST(COALESCE(DATEADD('day', -1, :wm_task),
                                 DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                        DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_login := GREATEST(COALESCE(DATEADD('day', -1, :wm_login),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_storage := GREATEST(COALESCE(DATEADD('day', -1, :wm_storage),
                                    DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                           DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY t
    USING (
        SELECT
            USAGE_DATE AS DAY,
            UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
            SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE,
            SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CREDITS_CLOUD_SVCS,
            SUM(COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)) AS CREDITS_ADJUSTMENT,
            SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_USED,
            SUM(COALESCE(CREDITS_BILLED,
                GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))) AS CREDITS_BILLED
        FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
        WHERE USAGE_DATE >= :lo_metering::DATE
        GROUP BY 1, 2
    ) s
    ON t.DAY = s.DAY AND t.SERVICE_TYPE = s.SERVICE_TYPE
    WHEN MATCHED THEN UPDATE SET
        CREDITS_COMPUTE = s.CREDITS_COMPUTE, CREDITS_CLOUD_SVCS = s.CREDITS_CLOUD_SVCS,
        CREDITS_ADJUSTMENT = s.CREDITS_ADJUSTMENT, CREDITS_USED = s.CREDITS_USED,
        CREDITS_BILLED = s.CREDITS_BILLED, LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, SERVICE_TYPE, CREDITS_COMPUTE, CREDITS_CLOUD_SVCS, CREDITS_ADJUSTMENT, CREDITS_USED, CREDITS_BILLED)
        VALUES (s.DAY, s.SERVICE_TYPE, s.CREDITS_COMPUTE, s.CREDITS_CLOUD_SVCS, s.CREDITS_ADJUSTMENT, s.CREDITS_USED, s.CREDITS_BILLED);

    -- V064 rec7: metering has no txn wrap -- a fact-load failure aborts the proc
    -- before this line (V063's deliberate anchor-first design), so reaching here
    -- means metering loaded; advance its own mark. The mark MERGE is GUARDED
    -- (review fix): it is a NEW statement ahead of the isolated sibling blocks, so
    -- a transient OW_LOAD_WATERMARKS lock here must not abort the proc and starve
    -- task/login/storage -- log + hold the mark + fall through instead.
    BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_METERING_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_METERING_DAILY watermark advance - facts loaded, mark held, siblings unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V064 rec7: hold metering mark, keep loading siblings
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_task::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
    WITH task_attempts AS (
        -- V101: collapse task auto-retries to the terminal attempt so RUNS/FAILED count
        -- scheduled runs, not attempts (a FAILED attempt that SUCCEEDED on retry is NOT a
        -- failure) — matching ops_sql.task_runs / task_recent_states, so the mart Task
        -- Health panel stops over-reporting failures the live tab collapses.
        SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME,
               COMPLETED_TIME, STATE, ERROR_MESSAGE
        FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
        WHERE QUERY_START_TIME >= :lo_task::DATE
        QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                   ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
    )
    SELECT
        DATE(QUERY_START_TIME),
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        COUNT(*),
        SUM(IFF(STATE = 'FAILED', 1, 0)),
        AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),
        MAX_BY(STATE, QUERY_START_TIME),
        MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)
    FROM task_attempts
    GROUP BY 1, 2, 3, 4, 5;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_TASK_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_TASK_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_LOGIN_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_login::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        (DAY, USER_NAME, COMPANY, LOGINS, FAILED_LOGINS, PASSWORD_LOGINS)
    SELECT
        DATE(EVENT_TIMESTAMP),
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME),
        COUNT(*),
        SUM(IFF(IS_SUCCESS = 'NO', 1, 0)),
        SUM(IFF(FIRST_AUTHENTICATION_FACTOR = 'PASSWORD' AND IS_SUCCESS = 'YES', 1, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= :lo_login::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_LOGIN_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_LOGIN_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_STORAGE_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_storage::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
        (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
    SELECT
        USAGE_DATE,
        DATABASE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        -- V166 (R2-009): SUM, not AVG. The view has one row per DATABASE_ID per day, and a dropped
        -- (re-created / clone-refreshed) database keeps its own rows under the same name while it
        -- holds Time Travel / fail-safe bytes. Each row is already the daily average of its own
        -- DATABASE_ID, so the name-day billed bytes are the SUM (= storage_by_database_calendar_live).
        SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
        SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= :lo_storage::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_STORAGE_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- V064 rec7: the single shared DAILY_FACTS watermark advance is GONE -- each
    -- source advances its OWN mark in its own success path above, so one
    -- table's failure holds only that table's mark (siblings stay current).
    -- The SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed
    -- failure still surfaces as a stale freshness row.

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_METERING_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        UNION ALL
        SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        UNION ALL
        SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        UNION ALL
        SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    IF (failed_any) THEN
        RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, that source''s watermark held';
    END IF;
    RETURN 'daily facts loaded';
END;
$$;

-- >>> derived:SP_LOAD_APP_COST  (from V077; DELETE+INSERT in one transaction, ROLLBACK + APP_ERROR_LOG + re-RAISE; SESSIONS lookback 7 -> 30 days, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    -- Reload the trailing window and self-trim beyond 400 days (advertised max
    -- window is 365; keep a buffer). No central purge touches this fact.
    -- V166 (R2-011): ONE transaction. Under autocommit a failed INSERT left the DELETE committed,
    -- and the next run starts a day later, so the oldest deleted day was never reloaded (a
    -- permanent hole per failed run). A failure now rolls back to the previous fill, is logged as
    -- fact_load_failed (the self-watch ERR leg keys on the first CONTEXT word) and is re-raised,
    -- so the task still reads FAILED.
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
     WHERE DAY >= :lo OR DAY < DATEADD('day', -400, CURRENT_DATE());

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY (DAY, APPLICATION, USER_NAME, COMPANY, QUERIES, CREDITS)
    WITH cred AS (
        SELECT QUERY_ID,
               SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= :lo
        GROUP BY QUERY_ID
        HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
    ),
    q AS (
        SELECT QUERY_ID, SESSION_ID, START_TIME::DATE AS DAY,
               COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME, WAREHOUSE_NAME
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= :lo
    ),
    sess AS (
        -- One row per session; widen the lookback so a query's earlier-started
        -- session still resolves. Prefer the self-reported program, else the
        -- driver family (version stripped), else '(unknown)'.
        -- V166 (C10): 30 days before lo, not 7. The task LAST reload of a day (lo = that day)
        -- narrowed the lookback to 7 days and relabelled '(unknown)' a keep-alive / pooled session
        -- the first load had resolved. Same pad as app_cost_sql.SESSION_PAD_DAYS (the live twin).
        SELECT SESSION_ID,
               COALESCE(
                   NULLIF(GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')::STRING, ''),
                   NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                   '(unknown)'
               ) AS APPLICATION
        FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
        WHERE CREATED_ON >= DATEADD('day', -30, :lo)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
    ),
    joined AS (
        SELECT q.DAY,
               COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
               q.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) AS COMPANY,
               c.CREDITS
        FROM q
        JOIN cred c ON c.QUERY_ID = q.QUERY_ID
        LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
    )
    SELECT DAY, APPLICATION, USER_NAME, COMPANY, COUNT(*), SUM(CREDITS)
    FROM joined
    GROUP BY DAY, APPLICATION, USER_NAME, COMPANY;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AppCost', 'fact_load_failed', :emsg, 'FACT_APP_COST_DAILY - previous fill retained on rollback, error re-raised', CURRENT_ROLE();
            RAISE;
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_APP_COST_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT, t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT) VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- >>> derived:SP_LOAD_STORAGE_TRUTH  (from V046; DELETE+INSERT in one transaction, ROLLBACK + APP_ERROR_LOG + re-RAISE, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    -- V166 (R2-011): one transaction, so a failed INSERT rolls the DELETE back (see SP_LOAD_APP_COST).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY WHERE DAY >= :lo;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY
        (DAY, TABLE_BYTES, STAGE_BYTES, FAILSAFE_BYTES, HYBRID_BYTES, ARCHIVE_COOL_BYTES, ARCHIVE_COLD_BYTES)
    SELECT
        USAGE_DATE,
        SUM(COALESCE(STORAGE_BYTES, 0)),
        SUM(COALESCE(STAGE_BYTES, 0)),
        SUM(COALESCE(FAILSAFE_BYTES, 0)),
        SUM(COALESCE(HYBRID_TABLE_STORAGE_BYTES, 0)),
        SUM(COALESCE(ARCHIVE_STORAGE_COOL_BYTES, 0)),
        SUM(COALESCE(ARCHIVE_STORAGE_COLD_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE
    WHERE USAGE_DATE >= :lo
    GROUP BY USAGE_DATE;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'StorageTruth', 'fact_load_failed', :emsg, 'FACT_STORAGE_ACCOUNT_DAILY - previous fill retained on rollback, error re-raised', CURRENT_ROLE();
            RAISE;
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_STORAGE_ACCOUNT_DAILY' AS SOURCE_NAME,
               MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT,
        t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- R2-009 repair (bounded, idempotent): the name-days where the view still holds 2+ DATABASE_ID rows were
-- written as the AVG; rewrite exactly those two byte columns as the SUM the loader now writes. Single-ID
-- name-days are untouched (the AVG of one row is its SUM). No DELETE, no insert, LOAD_TS unchanged.
MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
USING (
    SELECT USAGE_DATE AS DAY, DATABASE_NAME,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
) s
ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME
WHEN MATCHED THEN UPDATE SET DB_BYTES = s.DB_BYTES, FAILSAFE_BYTES = s.FAILSAFE_BYTES;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 166 AS VERSION,
       'Fact loader window integrity: SP_LOAD_SECURITY_FACTS re-derived from V105, the d<=3 change reload DELETE and INSERT share one bound GREATEST(MIN(extract START_TIME), midnight(today-d)) so a swallowed extract failure, a backfill-wide extract or a manual DAYS_BACK below 3 no longer deletes rows the INSERT never re-reads; SP_LOAD_DAILY_FACTS re-derived from V101, FACT_STORAGE_DAILY SUMs the per-DATABASE_ID rows per name-day (re-created and clone-refreshed databases were averaged) plus one bounded MERGE that repairs the multi-ID name-days still in the 365-day view; SP_LOAD_APP_COST (V077) and SP_LOAD_STORAGE_TRUTH (V046) run DELETE and INSERT in one transaction with ROLLBACK, a fact_load_failed APP_ERROR_LOG row and a re-raise (a failed run lost its oldest reloaded day for good); SP_LOAD_APP_COST resolves sessions 30 days before its reload start, not 7. No task, table or DDL change; no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 166);
