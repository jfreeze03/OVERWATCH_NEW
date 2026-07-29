-- V055__cloud_services_breakdown.sql — persist per-query cloud-services credits
-- so the alert (COST_CLOUD_SVC_RATIO) becomes self-explaining.
--
--   Owner ask 2026-07-28: the cloud-services ratio sits >21% on two warehouses;
--   drill into the components (which query shapes / users burn the credits) to
--   lower the cost. The Spend page already shows the ratio, the rebate, and the
--   compile-heavy / by-type views (live). Missing is a persisted shape+user
--   breakdown — metadata storms of tiny queries never surface in compile-heavy.
--
--   1. OW_QH_EXTRACT gains CREDITS_USED_CLOUD_SERVICES (additive; the extract
--      already scans QUERY_HISTORY once, so no new scan).
--   2. SP_LOAD_QH_EXTRACT re-derived (V042) to fill the column and cascade an
--      isolated CALL into the mart loader.
--   3. MART_CLOUD_SVC_DAILY — CS credits at day/company/warehouse/user/role/
--      query_type/parameterized_hash grain, CS>0 only.
--   4. SP_LOAD_CLOUD_SVC_MART — MERGE trailing 3 days from the extract.
--   5. SP_PURGE_FACTS re-derived (V054) to trim the new mart.
--
--   Additive columns + one new table/proc + two proc swaps. Apply AFTER V054.
--   Idempotent; safe to re-run.
--
-- Derivation law: SP_LOAD_QH_EXTRACT from V042 + column/cascade edits;
-- SP_PURGE_FACTS from V054 + one retention DELETE; the test re-derives and
-- byte-compares. MART_CLOUD_SVC_DAILY + SP_LOAD_CLOUD_SVC_MART are hand-authored.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20055, 'V055 requires V054 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 54) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- 1. Additive column on the QH extract (TRANSIENT; safe to leave on rollback).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    ADD COLUMN IF NOT EXISTS CREDITS_USED_CLOUD_SERVICES FLOAT;

-- 3. Cloud-services breakdown mart (shape + user grain, CS>0 only).
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY (
    DAY                       DATE          NOT NULL,
    COMPANY                   VARCHAR(50),
    WAREHOUSE_NAME            VARCHAR(300)  NOT NULL,
    USER_NAME                 VARCHAR(300)  NOT NULL,
    ROLE_NAME                 VARCHAR(300)  NOT NULL,
    QUERY_TYPE                VARCHAR(100)  NOT NULL,
    QUERY_PARAMETERIZED_HASH  VARCHAR(64)   NOT NULL,
    SAMPLE_TEXT               VARCHAR(200),
    RUNS                      NUMBER,
    CS_CREDITS                FLOAT,
    EXEC_SEC_SUM              FLOAT,
    COMPILE_SEC_SUM           FLOAT,
    CACHE_PCT_SUM            FLOAT,
    LOAD_TS                   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- 4. Mart loader — from the extract (no QUERY_HISTORY rescan), trailing 3 days,
--    MERGE-accumulated. COMPANY via the warehouse UDF (V030 shape law: the UDF
--    runs on a plain column outside the aggregation).
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY t
    USING (
        SELECT g.DAY,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
               g.WAREHOUSE_NAME, g.USER_NAME, g.ROLE_NAME, g.QUERY_TYPE,
               g.QUERY_PARAMETERIZED_HASH, g.SAMPLE_TEXT, g.RUNS, g.CS_CREDITS,
               g.EXEC_SEC_SUM, g.COMPILE_SEC_SUM, g.CACHE_PCT_SUM
        FROM (
            SELECT DATE(START_TIME) AS DAY,
                   COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                   COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                   COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                   COALESCE(QUERY_TYPE, 'UNKNOWN') AS QUERY_TYPE,
                   COALESCE(QUERY_PARAMETERIZED_HASH, 'n/a') AS QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 160)) AS SAMPLE_TEXT,
                   COUNT(*) AS RUNS,
                   SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CS_CREDITS,
                   SUM(COALESCE(EXECUTION_TIME, 0)) / 1000 AS EXEC_SEC_SUM,
                   SUM(COALESCE(COMPILATION_TIME, 0)) / 1000 AS COMPILE_SEC_SUM,
                   SUM(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)) AS CACHE_PCT_SUM
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            -- Day-ALIGNED window (CURRENT_DATE, not CURRENT_TIMESTAMP): only
            -- merge whole calendar days that are fully inside the extract's 72h
            -- retention (today + the 2 prior full days). A rolling -3d TIMESTAMP
            -- window would overwrite an aging day with a shrinking PARTIAL count
            -- as the window slid off it, then freeze it partial (no backfill
            -- owns this mart's history). Aligned, a day is only ever written
            -- complete, so it freezes complete when it ages out.
            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())
              AND COALESCE(CREDITS_USED_CLOUD_SERVICES, 0) > 0
            GROUP BY 1, 2, 3, 4, 5, 6
        ) g
    ) s
    ON  t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
    AND t.USER_NAME = s.USER_NAME AND t.ROLE_NAME = s.ROLE_NAME
    AND t.QUERY_TYPE = s.QUERY_TYPE
    AND t.QUERY_PARAMETERIZED_HASH = s.QUERY_PARAMETERIZED_HASH
    WHEN MATCHED THEN UPDATE SET
        COMPANY = s.COMPANY, SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS,
        CS_CREDITS = s.CS_CREDITS, EXEC_SEC_SUM = s.EXEC_SEC_SUM,
        COMPILE_SEC_SUM = s.COMPILE_SEC_SUM, CACHE_PCT_SUM = s.CACHE_PCT_SUM,
        LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, COMPANY, WAREHOUSE_NAME, USER_NAME, ROLE_NAME, QUERY_TYPE,
         QUERY_PARAMETERIZED_HASH, SAMPLE_TEXT, RUNS, CS_CREDITS, EXEC_SEC_SUM,
         COMPILE_SEC_SUM, CACHE_PCT_SUM)
    VALUES
        (s.DAY, s.COMPANY, s.WAREHOUSE_NAME, s.USER_NAME, s.ROLE_NAME, s.QUERY_TYPE,
         s.QUERY_PARAMETERIZED_HASH, s.SAMPLE_TEXT, s.RUNS, s.CS_CREDITS, s.EXEC_SEC_SUM,
         s.COMPILE_SEC_SUM, s.CACHE_PCT_SUM);
    RETURN 'cloud-svc mart merged (' || :SQLROWCOUNT || ' rows)';
END;
$$;

-- 2. Re-derived extract loader — fills the new column + cascades the mart.
-- >>> derived:SP_LOAD_QH_EXTRACT
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo TIMESTAMP_NTZ;  -- reload lower bound
    d INT;
    emsg VARCHAR;
    ok BOOLEAN DEFAULT FALSE;  -- r22 #7: extract arm committed this cycle
BEGIN
    -- DAYS_BACK > 0 = explicit backfill window; 0 or NULL = watermark mode.
    -- The tasks pass 0 (never a bare NULL — no signature-resolution
    -- questions on any runtime).
    IF (COALESCE(DAYS_BACK, 0) > 0) THEN
        d := GREATEST(1, LEAST(DAYS_BACK, 400))::INT;
        lo := DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_NTZ;
    ELSE
        -- watermark - 45 min (ACCOUNT_USAGE lag overlap), first run 48h,
        -- catch-up clamped at the 3-day retention (wider gaps: backfill).
        SELECT GREATEST(
                   COALESCE(DATEADD('minute', -45, MAX(WM_TS)),
                            DATEADD('hour', -48, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ),
                   DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
          INTO :lo
        FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
        WHERE SOURCE = 'QH_EXTRACT';
    END IF;

    -- The one QUERY_HISTORY scan of the hourly cycle. Retention trim rides
    -- the same DELETE; an explicit backfill keeps its wider window until the
    -- next watermark-mode run trims back to 3 days. Both arms carry V017
    -- isolation (v4.36.1): a failed extract fill must not fail the task —
    -- the facts keep their last load and the freshness labels say so.
    -- r22 #7: the arm is one TRANSACTION — a failed INSERT rolls the DELETE
    -- back (no hole; consumers really do read the previous fill) and the
    -- watermark below only advances on COMMIT.
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
     WHERE START_TIME >= :lo
        OR START_TIME < LEAST(:lo, DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ);

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        (QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
         USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE,
         TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME, QUEUED_OVERLOAD_TIME,
         QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE, BYTES_SCANNED,
         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT,
         CREDITS_USED_CLOUD_SERVICES)
    SELECT QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
           USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE::VARCHAR,
           LEFT(ERROR_MESSAGE, 200), TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME,
           QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE,
           BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH,
           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= :lo;
    COMMIT;
    ok := TRUE;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'extract_load_failed', :emsg, 'OW_QH_EXTRACT - consumers read the previous fill', CURRENT_ROLE();
    END;

    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        (HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    SELECT
        DATE_TRUNC('hour', START_TIME),
        WAREHOUSE_NAME,
        DATABASE_NAME,
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME),
        COUNT(*),
        SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)),
        SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95),
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3)
    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
    GROUP BY 1, 2, 3, 4, 5;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_HOURLY - extract unaffected', CURRENT_ROLE();
    END;

    -- r22 #1: the day-grain query fact — same dims as the hourly fact, 1/24th
    -- the rows, backfillable a full year (backfill_365.sql owns history; this
    -- arm keeps the trailing 3 days current). Company via the UDF on a plain
    -- column OUTSIDE the aggregation (V030 shape law). 'FAIL' matches the
    -- V002 hourly-fact convention.
    BEGIN
    BEGIN TRANSACTION;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY t
    USING (
        SELECT g.DAY, g.WAREHOUSE_NAME, g.DATABASE_NAME, g.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
               g.QUERY_COUNT, g.FAILED_COUNT, g.ELAPSED_SEC_SUM, g.QUEUED_SEC_SUM, g.SPILL_REMOTE_GB
        FROM (
            SELECT DATE(START_TIME) AS DAY,
                   COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                   COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                   COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                   COUNT(*) AS QUERY_COUNT,
                   SUM(IFF(EXECUTION_STATUS = 'FAIL', 1, 0)) AS FAILED_COUNT,
                   SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000 AS ELAPSED_SEC_SUM,
                   SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000 AS QUEUED_SEC_SUM,
                   SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
            GROUP BY 1, 2, 3, 4
        ) g
    ) s
    ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
       AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
    WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERY_COUNT = s.QUERY_COUNT,
        FAILED_COUNT = s.FAILED_COUNT, ELAPSED_SEC_SUM = s.ELAPSED_SEC_SUM,
        QUEUED_SEC_SUM = s.QUEUED_SEC_SUM, SPILL_REMOTE_GB = s.SPILL_REMOTE_GB,
        LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.COMPANY, s.QUERY_COUNT,
            s.FAILED_COUNT, s.ELAPSED_SEC_SUM, s.QUEUED_SEC_SUM, s.SPILL_REMOTE_GB);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- V055: cloud-services breakdown mart, from the extract just filled.
    -- Isolated (V017): its failure must not break the extract or the
    -- watermark — consumers keep the previous mart fill.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'cloud_svc_mart_failed', :emsg, 'MART_CLOUD_SVC_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- R5: advance the watermark; R6: loader-owned freshness — ONLY when the
    -- extract arm committed (r22 #7: a failed cycle must re-cover its window).
    IF (ok) THEN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'QH_EXTRACT' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OW_QH_EXTRACT' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        UNION ALL
        SELECT 'FACT_QUERY_HOURLY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        UNION ALL
        SELECT 'FACT_QUERY_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');
    END IF;

    RETURN 'qh extract + query facts loaded (extract committed: ' || :ok || ')';
END;
$$;

-- 5. Re-derived purge — trims the new mart on the daily-fact retention floor.
-- >>> derived:SP_PURGE_FACTS
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    hourly_days FLOAT;
    daily_days FLOAT;
    err_days FLOAT;
    usage_days FLOAT;
    total INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_HOURLY', VALUE, NULL))), 400),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_DAILY', VALUE, NULL))), 800),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'ERROR_LOG_RETENTION_DAYS', VALUE, NULL))), 180),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'APP_USAGE_RETENTION_DAYS', VALUE, NULL))), 365)
      INTO :hourly_days, :daily_days, :err_days, :usage_days
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    hourly_days := GREATEST(hourly_days, 90);
    daily_days := GREATEST(daily_days, 365);
    err_days := GREATEST(err_days, 30);
    usage_days := GREATEST(usage_days, 90);

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
     WHERE LOGGED_AT < DATEADD('day', -1 * :err_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE
     WHERE AT < DATEADD('day', -1 * :usage_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;


    -- V042 (r22 #10): the V027 mart family and the V041 loader-pass tables
    -- join retention — same settings-driven windows, hour grain vs day grain.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
     WHERE EVENT_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;

    RETURN 'purged ' || :total || ' row(s)';
END;
$$;

-- First fill: refill the trailing 3 days of the extract WITH the CS column, which
-- cascades into MART_CLOUD_SVC_DAILY. The hourly task keeps both current.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 55 AS VERSION,
       'Per-query cloud-services credits persisted (OW_QH_EXTRACT column + MART_CLOUD_SVC_DAILY at shape/user grain) so the cloud-services ratio alert can be drilled to its driving query shapes and users' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 55);
