-- V094__fact_query_hourly_boundary_dedupe.sql
--
-- Fix a permanent FACT_QUERY_HOURLY duplicate. SP_LOAD_QH_EXTRACT's hourly refresh
-- (V062) DELETEs on the hour-truncated column HOUR_TS against a NON-truncated instant
-- but re-INSERTs DATE_TRUNC('hour', START_TIME) filtered by that same instant, so the
-- boundary hour H0 is never deleted yet is re-inserted PARTIAL each run -- two rows
-- accumulate per grain for every hour older than ~48h, and readers that SUM the grain
-- over a window >=2 days double-count (~2x QUERY_COUNT / FAILED_COUNT / ELAPSED /
-- QUEUED / SPILL). The sibling SP_LOAD_OPS_DIAG already truncates both bounds (the B10
-- fix); this arm was missed.
--
-- Re-derives SP_LOAD_QH_EXTRACT from V062 byte-identically plus two edits: the DELETE
-- and INSERT bounds are truncated to the hour (DATE_TRUNC('hour', DATEADD(...))), so
-- the boundary hour is deleted AND fully rebuilt each run. The watermark first-run
-- fallback is deliberately unchanged. Then a one-time dedup keeps the highest-
-- QUERY_COUNT row per grain (the complete hour is a superset of the partial slice) --
-- SP_NIGHTLY_RECONCILE does not rebuild this fact, so the historical duplicates need an
-- explicit cleanup.
--
-- Procedure re-derivation + one-time data cleanup: no schema change, no new object, no
-- backfill. Owner applies in Snowsight after V093. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20094, 'V094 requires V093 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 93) THEN
        RAISE not_ready;
    END IF;
END;
$$;

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
     WHERE HOUR_TS >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()));

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
        SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)),
        SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95),
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3)
    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    WHERE START_TIME >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))
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
                   SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
                   SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000 AS ELAPSED_SEC_SUM,
                   SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000 AS QUEUED_SEC_SUM,
                   SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            -- Day-aligned (audit #6): only WHOLE days inside the 72h extract,
            -- so an aging day freezes COMPLETE, not at its last partial hour.
            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())
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

-- One-time dedup of the rows the boundary-hour bug already left: keep the highest-
-- QUERY_COUNT row per grain (the complete hour dominates the partial slice), break ties
-- on the newest LOAD_TS. INSERT OVERWRITE reads the pre-overwrite snapshot, so the
-- self-referential dedup is safe (the standard Snowflake pattern). No-op once clean.
INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
    (HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
     FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB, LOAD_TS)
SELECT HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
       FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB, LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY
    ORDER BY QUERY_COUNT DESC, LOAD_TS DESC) = 1;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 94 AS VERSION,
       'FACT_QUERY_HOURLY boundary-hour dedupe: SP_LOAD_QH_EXTRACT re-derived from V062 so the FACT_QUERY_HOURLY DELETE and INSERT bounds are both hour-truncated (DATE_TRUNC(hour, DATEADD(hour,-48,...))) like the sibling SP_LOAD_OPS_DIAG, so the boundary hour is deleted and fully rebuilt each run instead of leaving a permanent partial duplicate that doubled multi-day query facts. Plus a one-time INSERT OVERWRITE dedup keeping the highest-QUERY_COUNT row per grain. Proc + data cleanup, no schema change, no backfill.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 94);
