-- V041__loader_efficiency.sql — the loader-efficiency pass.
-- Authority: docs/design/V041_LOADER_PASS.md (design freeze 2026-07-12);
-- evidence: docs/design/PERF_BACKLOG.md Tier A. ONE migration, eleven riders.
--
--   R1  OW_QH_EXTRACT: ONE QUERY_HISTORY scan per hourly cycle (watermark -
--       45 min, 3-day retention), projected to the union of consumer columns.
--       Rewired consumers (the design's list, exactly): FACT_QUERY_HOURLY,
--       _OW_ALLOC_BASE, tag coverage, query-family, schema-hourly,
--       role-hourly, incident-timeline DDL arm, R7 diagnostics. The
--       warehouse-efficiency q-CTE and posture ADMIN_STMTS_24H are NOT on
--       the list and deliberately stay live.
--   R2  FACT_COST_ALLOC_XDIM_DAILY persisted from _OW_ALLOC_BASE before it
--       collapses (no schema grain — cardinality; schema stays live).
--   R3  is app-side (AI users tab reads FACT_AI_USAGE_DAILY mart-first).
--   R4  Exec board v2: windows 7/14/30/60/90 (= config), single-pass KPI
--       aggregation, atomic stage->SWAP visibility; PRESSURE_QUEUE /
--       PRESSURE_SPILL / DB_MIX retired (whole-tree grep 2026-07-12: zero
--       readers).
--   R5  OW_LOAD_WATERMARKS + TASK_NIGHTLY_RECONCILE (delete-and-rebuild the
--       trailing 3 days: restated ACCOUNT_USAGE rows and disappeared groups
--       cannot survive stale MERGE rows).
--   R6  Loader-owned freshness: every SP merges its sources into
--       SOURCE_FRESHNESS_STATE (+GENERATION, +STATUS) in its own commit;
--       TASK_SNAPSHOT_FRESHNESS retired (144 wakes/day); SP_SNAPSHOT_FRESHNESS
--       kept for manual refresh.
--   R7  MART_OPS_DIAG_HOURLY from the extract: top-20/hour by elapsed +
--       failure families per hour (Operations first paint goes mart-first).
--   R8  FACT_PLATFORM_SCORE_DAILY: the retro score's four input aggregates,
--       loaded daily; weights stay in Python.
--   R9  Posture UNUSED_ROLES_90D from FACT_QUERY_ROLE_HOURLY, coverage-gated
--       (HAVING emits no row — never a lying zero — until the fact spans 90d).
--   R10 WAREHOUSE_ID > 0 joins the V27-family loader's metering source reads
--       (the V039 promise); eff-mart READER name-filters stay until the next
--       re-derivation.
--   R11 Resource-monitor totals land in the daily posture row via SHOW ->
--       RESULT_SCAN (V024's daily scan is the owner's-rights precedent).
--
-- Derivation law: SP_LOAD_MARTS_V27 below is generated VERBATIM from V031's
-- proc + the enumerated edits above (R1 x6 FROM swaps, R2 arm, R9, R10 x2,
-- R11, R6 x2); SP_LOAD_HOURLY_FACTS from V039's minus the FACT_QUERY_HOURLY
-- arm (moved into SP_LOAD_QH_EXTRACT with only its FROM swapped) plus the R6
-- write; SP_LOAD_DAILY_FACTS from V002's + R5 window bounds + R5/R6 tail.
-- tests/test_v041_loader_pass.py rebuilds each derivation and asserts
-- equality — hand-editing any copy without its origin fails there, loudly.
--
-- Ordering note (why FACT_QUERY_HOURLY moved): the design chains
-- TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_LOAD_MARTS_V27_HOURLY. The
-- root's proc runs BEFORE the extract fills, so a fact arm left in
-- SP_LOAD_HOURLY_FACTS reading the extract would trail by one cycle. The arm
-- runs inside SP_LOAD_QH_EXTRACT immediately after the fill instead.
--
-- Apply AFTER V040 -> re-run roles.sql (new objects) -> validate.sql expects
-- V001..V041 -> redeploy the app. Backfill note: the hourly marts now read
-- the extract, so wide backfills fill it first (backfill_365.sql updated):
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90);
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90);
-- Idempotent; safe to re-run.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20041, 'BLOCKED: SCHEMA_VERSION < 40 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 40) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

-- ---------------------------------------------------------------------------
-- New tables (all rebuildable — real drops in teardown.sql)
-- ---------------------------------------------------------------------------

-- R5: one row per loader; loaders read since WM_TS minus overlap.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS (
    SOURCE     VARCHAR(60)   NOT NULL PRIMARY KEY,
    WM_TS      TIMESTAMP_NTZ NOT NULL,
    UPDATED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R1: the staged QUERY_HISTORY extract — union of consumer columns, 3-day
-- retention (or the explicit backfill window). Transient: scratch by design.
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT (
    QUERY_ID                        VARCHAR(64),
    START_TIME                      TIMESTAMP_LTZ,
    WAREHOUSE_NAME                  VARCHAR(200),
    WAREHOUSE_SIZE                  VARCHAR(40),
    DATABASE_NAME                   VARCHAR(200),
    SCHEMA_NAME                     VARCHAR(200),
    USER_NAME                       VARCHAR(200),
    ROLE_NAME                       VARCHAR(200),
    QUERY_TYPE                      VARCHAR(60),
    EXECUTION_STATUS                VARCHAR(30),
    ERROR_CODE                      VARCHAR(30),
    ERROR_MESSAGE                   VARCHAR(200),
    TOTAL_ELAPSED_TIME              NUMBER(18,0),
    EXECUTION_TIME                  NUMBER(18,0),
    COMPILATION_TIME                NUMBER(18,0),
    QUEUED_OVERLOAD_TIME            NUMBER(18,0),
    QUEUED_PROVISIONING_TIME        NUMBER(18,0),
    BYTES_SPILLED_TO_REMOTE_STORAGE NUMBER(24,0),
    BYTES_SCANNED                   NUMBER(24,0),
    PERCENTAGE_SCANNED_FROM_CACHE   FLOAT,
    QUERY_TAG                       VARCHAR(2000),
    QUERY_PARAMETERIZED_HASH        VARCHAR(64),
    QUERY_TEXT                      VARCHAR(200),
    LOAD_TS                         TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R2: allocation at DAY x WAREHOUSE x DATABASE x USER (no schema grain).
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY (
    DAY            DATE          NOT NULL,
    WAREHOUSE_NAME VARCHAR(200)  NOT NULL,
    DATABASE_NAME  VARCHAR(200)  NOT NULL,
    USER_NAME      VARCHAR(200)  NOT NULL,
    EXEC_SEC       NUMBER(18,1),
    ALLOC_CREDITS  NUMBER(18,6),
    LOAD_TS        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R7: Operations first-paint diagnostics (two row kinds, one hourly loader).
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY (
    HOUR_TS          TIMESTAMP_NTZ NOT NULL,
    KIND             VARCHAR(20)   NOT NULL,  -- TOP_ELAPSED | FAIL_FAMILY
    COMPANY          VARCHAR(40)   NOT NULL DEFAULT 'ALFA',
    QUERY_ID         VARCHAR(64),
    START_TIME       TIMESTAMP_LTZ,
    USER_NAME        VARCHAR(200),
    WAREHOUSE_NAME   VARCHAR(200),
    WAREHOUSE_SIZE   VARCHAR(40),
    DATABASE_NAME    VARCHAR(200),
    QUERY_TYPE       VARCHAR(60),
    EXECUTION_STATUS VARCHAR(30),
    ELAPSED_SEC      NUMBER(18,3),
    QUEUED_SEC       NUMBER(18,3),
    SPILL_REMOTE_GB  NUMBER(18,4),
    QUERY_PREVIEW    VARCHAR(200),
    ERROR_CODE       VARCHAR(30),
    ERROR_MESSAGE    VARCHAR(200),
    FAILURES         NUMBER(12,0),
    USERS_AFFECTED   NUMBER(12,0),
    LAST_SEEN        TIMESTAMP_LTZ,
    LOAD_TS          TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R8: the retro platform score's four input aggregates, one row per day.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY (
    DAY            DATE          NOT NULL,
    CREDITS_BILLED NUMBER(18,6),
    QUERY_COUNT    NUMBER(14,0),
    FAILED_COUNT   NUMBER(14,0),
    QUEUED_SEC     NUMBER(18,1),
    SPILL_GB       NUMBER(18,3),
    TASK_RUNS      NUMBER(14,0),
    TASK_FAILED    NUMBER(14,0),
    CRIT_RAISED    NUMBER(12,0),
    HIGH_RAISED    NUMBER(12,0),
    LOAD_TS        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R4: the board's build stage (SWAP target — same shape as MART_EXEC_BOARD).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE (
    COMPANY      VARCHAR(40)   NOT NULL,
    WINDOW_DAYS  NUMBER(4,0)   NOT NULL,
    PANEL        VARCHAR(60)   NOT NULL,
    METRIC       VARCHAR(80)   NOT NULL,
    DIMENSION    VARCHAR(300),
    PERIOD_START DATE,
    VALUE        NUMBER(24,6),
    VALUE_USD    NUMBER(24,2),
    UNIT         VARCHAR(40),
    SORT_ORDER   NUMBER(6,0),
    REFRESHED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- R6: freshness state gains a generation (future cache-invalidation token)
-- and a status column.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS GENERATION NUMBER(18,0) DEFAULT 0;
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS STATUS VARCHAR(400);

-- ---------------------------------------------------------------------------
-- R1: the extract loader (+ the FACT_QUERY_HOURLY arm, moved here from
-- SP_LOAD_HOURLY_FACTS — see the ordering note in the header). DAYS_BACK NULL
-- means watermark mode (the task path); a number is the explicit backfill
-- window (backfill_365.sql).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo TIMESTAMP_NTZ;  -- reload lower bound
    d INT;
BEGIN
    IF (DAYS_BACK IS NOT NULL) THEN
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
    -- next watermark-mode run trims back to 3 days.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
     WHERE START_TIME >= :lo
        OR START_TIME < LEAST(:lo, DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ);

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        (QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
         USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE,
         TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME, QUEUED_OVERLOAD_TIME,
         QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE, BYTES_SCANNED,
         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT)
    SELECT QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
           USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE::VARCHAR,
           LEFT(ERROR_MESSAGE, 200), TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME,
           QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE,
           BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH,
           LEFT(QUERY_TEXT, 200)
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= :lo;

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

    -- R5: advance the watermark; R6: loader-owned freshness.
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
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'qh extract + hourly query fact loaded';
END;
$$;

-- ---------------------------------------------------------------------------
-- Re-derived loaders (see the derivation-law note in the header).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY t
    USING (
        SELECT
            DATE(START_TIME) AS DAY,
            WAREHOUSE_NAME,
            DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY,
            SUM(COALESCE(CREDITS_USED_COMPUTE, CREDITS_USED)) AS CREDITS_COMPUTE,
            SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_TOTAL
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE START_TIME >= DATEADD('day', -3, CURRENT_DATE())
          AND WAREHOUSE_ID > 0
        GROUP BY 1, 2, 3
    ) s
    ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
    WHEN MATCHED THEN UPDATE SET
        COMPANY = s.COMPANY, CREDITS_COMPUTE = s.CREDITS_COMPUTE,
        CREDITS_TOTAL = s.CREDITS_TOTAL, LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_COMPUTE, CREDITS_TOTAL)
        VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_COMPUTE, s.CREDITS_TOTAL);

    -- V041 R6: loader-owned freshness (FACT_QUERY_HOURLY moved to the extract).
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_WAREHOUSE_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'hourly facts loaded';
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    wm TIMESTAMP_NTZ;           -- V041 R5: last successful daily load
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_short TIMESTAMP_NTZ;     -- watermark - 1d overlap (default -3d, clamp -30d)
BEGIN
    SELECT MAX(WM_TS) INTO :wm
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_short := GREATEST(COALESCE(DATEADD('day', -1, :wm),
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

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_short::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
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
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE QUERY_START_TIME >= :lo_short::DATE
    GROUP BY 1, 2, 3, 4, 5;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_short::DATE;
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
    WHERE EVENT_TIMESTAMP >= :lo_short::DATE
    GROUP BY 1, 2, 3;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_short::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
        (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
    SELECT
        USAGE_DATE,
        DATABASE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= :lo_short::DATE
    GROUP BY 1, 2, 3;

    -- V041 R5+R6: advance the watermark; loader-owned freshness.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'DAILY_FACTS' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);

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

    RETURN 'daily facts loaded';
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
    loaded VARCHAR DEFAULT '';
    d INT;
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;

    IF (UPPER(:SCOPE) = 'HOURLY') THEN

        -- [1] warehouse efficiency ------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY t
            USING (
                WITH m AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           SUM(CREDITS_USED) AS CREDITS_TOTAL,
                           SUM(CREDITS_USED_COMPUTE) AS CREDITS_COMPUTE,
                           COUNT_IF(CREDITS_USED > 0) AS BILLED_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                ),
                q AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,
                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,
                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,
                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS,
                           COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) AS ACTIVE_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_NAME IS NOT NULL
                    GROUP BY 1, 2
                )
                SELECT COALESCE(m.DAY, q.DAY) AS DAY,
                       COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)) AS COMPANY,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0), 4) AS CREDITS_TOTAL,
                       ROUND(COALESCE(m.CREDITS_COMPUTE, 0), 4) AS CREDITS_COMPUTE,
                       COALESCE(q.QUERIES, 0) AS QUERIES,
                       COALESCE(q.FAILS, 0) AS FAILS,
                       ROUND(COALESCE(q.QUEUED_MIN, 0), 2) AS QUEUED_MIN,
                       ROUND(COALESCE(q.SPILL_GB, 0), 3) AS SPILL_GB,
                       ROUND(COALESCE(q.P95_S, 0), 1) AS P95_S,
                       ROUND(COALESCE(q.EXEC_HOURS, 0), 3) AS EXEC_HOURS,
                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,
                       COALESCE(q.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,
                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(q.ACTIVE_HOURS, 0), 0)
                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY
                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET
                COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL,
                CREDITS_COMPUTE = s.CREDITS_COMPUTE, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_MIN = s.QUEUED_MIN, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S,
                EXEC_HOURS = s.EXEC_HOURS, BILLED_HOURS = s.BILLED_HOURS,
                ACTIVE_HOURS = s.ACTIVE_HOURS, IDLE_PCT = s.IDLE_PCT,
                CREDITS_PER_QUERY = s.CREDITS_PER_QUERY, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
                 QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS, IDLE_PCT, CREDITS_PER_QUERY)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_TOTAL, s.CREDITS_COMPUTE, s.QUERIES, s.FAILS,
                    s.QUEUED_MIN, s.SPILL_GB, s.P95_S, s.EXEC_HOURS, s.BILLED_HOURS, s.ACTIVE_HOURS, s.IDLE_PCT, s.CREDITS_PER_QUERY);
            loaded := loaded || 'wh_eff ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [2] query families (top 2000/day by exec time) --------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DATE(START_TIME) AS DAY,
                       QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);
            loaded := loaded || 'qfam ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_QUERY_FAMILY_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [3] role-hour fact -------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.ROLE_NAME = s.ROLE_NAME AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC)
            VALUES (s.HOUR_TS, s.ROLE_NAME, s.WAREHOUSE_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.EXEC_SEC);
            loaded := loaded || 'role_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_ROLE_HOURLY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [4] schema-hour fact -----------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.DATABASE_NAME = s.DATABASE_NAME AND t.SCHEMA_NAME = s.SCHEMA_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS, QUEUED_SEC, SPILL_GB, P95_S)
            VALUES (s.HOUR_TS, s.DATABASE_NAME, s.SCHEMA_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.QUEUED_SEC, s.SPILL_GB, s.P95_S);
            loaded := loaded || 'schema_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [4b] tag coverage by user, day grain (v4.14 tuning trio) --------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY t
            USING (
                SELECT g.DAY, g.USER_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(g.USER_NAME) AS COMPANY,
                       g.QUERIES, g.EXEC_SEC, g.UNTAGGED_EXEC_SEC
                FROM (
                    SELECT DATE(START_TIME) AS DAY,
                           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                           COUNT(*) AS QUERIES,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC,
                           ROUND(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL,
                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2
                ) g
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES,
                EXEC_SEC = s.EXEC_SEC, UNTAGGED_EXEC_SEC = s.UNTAGGED_EXEC_SEC,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, COMPANY, QUERIES, EXEC_SEC, UNTAGGED_EXEC_SEC)
            VALUES (s.DAY, s.USER_NAME, s.COMPANY, s.QUERIES, s.EXEC_SEC, s.UNTAGGED_EXEC_SEC);
            loaded := loaded || 'tagcov ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TAG_COVERAGE_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [5] cost allocation (exec-time share of each warehouse-hour) -------
        BEGIN
            CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE AS
            WITH wh AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       SUM(CREDITS_USED) AS HOUR_CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND WAREHOUSE_ID > 0
                GROUP BY 1, 2
            ),
            q AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       USER_NAME, COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND WAREHOUSE_NAME IS NOT NULL AND COALESCE(EXECUTION_TIME, 0) > 0
                GROUP BY 1, 2, 3, 4, 5, 6
            ),
            tot AS (
                SELECT HOUR_TS, WAREHOUSE_NAME, SUM(EXEC_MS) AS TOTAL_MS FROM q GROUP BY 1, 2
            )
            SELECT DATE(q.HOUR_TS) AS DAY, q.WAREHOUSE_NAME, q.USER_NAME, q.ROLE_NAME,
                   q.DATABASE_NAME, q.SCHEMA_NAME, q.EXEC_MS,
                   wh.HOUR_CREDITS * q.EXEC_MS / NULLIF(tot.TOTAL_MS, 0) AS ALLOC_CREDITS
            FROM q
            JOIN tot ON tot.HOUR_TS = q.HOUR_TS AND tot.WAREHOUSE_NAME = q.WAREHOUSE_NAME
            JOIN wh ON wh.HOUR_TS = q.HOUR_TS AND wh.WAREHOUSE_NAME = q.WAREHOUSE_NAME;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t
            USING (
                SELECT DAY, 'USER' AS DIMENSION, USER_NAME AS KEY_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'DATABASE', DATABASE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'SCHEMA', DATABASE_NAME || '.' || SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3, DATABASE_NAME
                UNION ALL
                SELECT DAY, 'ROLE', ROLE_NAME,
                       CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END,
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
            ) s
            ON t.DAY = s.DAY AND t.DIMENSION = s.DIMENSION AND t.KEY_NAME = s.KEY_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, ALLOC_CREDITS = s.ALLOC_CREDITS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC)
            VALUES (s.DAY, s.DIMENSION, s.KEY_NAME, s.COMPANY, s.ALLOC_CREDITS, s.EXEC_SEC);
            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [5b] cross-dim allocation fact (V041 R2): persist _OW_ALLOC_BASE at
        -- DAY x WAREHOUSE x DATABASE x USER before it collapses to single-dim.
        -- NO schema grain (cardinality; schema stays live-filtered). Same
        -- expressions as [5], so the day-sums reconcile by construction.
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY t
            USING (
                SELECT DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
                FROM _OW_ALLOC_BASE
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
               AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET EXEC_SEC = s.EXEC_SEC,
                ALLOC_CREDITS = s.ALLOC_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.EXEC_SEC, s.ALLOC_CREDITS);
            loaded := loaded || 'alloc_xdim ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [6] task graphs -----------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY t
            USING (
                WITH runs AS (
                    SELECT COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
                           MIN_BY(h.NAME, h.QUERY_START_TIME) AS PIPELINE,
                           MIN_BY(h.DATABASE_NAME, h.QUERY_START_TIME) AS DATABASE_NAME,
                           MIN_BY(h.SCHEMA_NAME, h.QUERY_START_TIME) AS SCHEMA_NAME,
                           DATE(MIN(h.QUERY_START_TIME)) AS DAY,
                           COUNT(*) AS TASK_RUNS,
                           COUNT_IF(h.STATE = 'FAILED') AS FAILED_TASKS,
                           DATEDIFF('second', MIN(h.QUERY_START_TIME), MAX(h.COMPLETED_TIME)) AS WALL_SEC,
                           SUM(COALESCE(a.CREDITS, 0)) AS CREDITS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    LEFT JOIN (
                        SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND QUERY_ID IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY QUERY_ID
                    ) a ON a.QUERY_ID = h.QUERY_ID
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND h.STATE IN ('SUCCEEDED', 'FAILED')
                    GROUP BY RUN_KEY
                )
                SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
                       COUNT(*) AS GRAPH_RUNS,
                       COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
                       SUM(TASK_RUNS) AS TASK_RUNS,
                       ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
                       ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
                       ROUND(SUM(CREDITS), 4) AS WH_CREDITS
                FROM runs GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.PIPELINE = s.PIPELINE
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET GRAPH_RUNS = s.GRAPH_RUNS,
                RUNS_WITH_FAILURES = s.RUNS_WITH_FAILURES, TASK_RUNS = s.TASK_RUNS,
                AVG_WALL_SEC = s.AVG_WALL_SEC, P95_WALL_SEC = s.P95_WALL_SEC,
                WH_CREDITS = s.WH_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
                 TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
            VALUES (s.DAY, s.PIPELINE, s.DATABASE_NAME, s.SCHEMA_NAME, s.GRAPH_RUNS,
                    s.RUNS_WITH_FAILURES, s.TASK_RUNS, s.AVG_WALL_SEC, s.P95_WALL_SEC, s.WH_CREDITS);
            loaded := loaded || 'graphs ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

            INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
                (EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID)
            SELECT RAISED_AT, 'ALERT', COMPANY, SEVERITY, LEFT(TITLE, 300), EVENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
            UNION ALL
            SELECT QUERY_START_TIME, 'TASK_FAIL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE QUERY_START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'
            UNION ALL
            SELECT START_TIME, 'DDL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'INFO', LEFT(QUERY_TYPE || ' by ' || USER_NAME || ' (' || COALESCE(ROLE_NAME, '?') || ')', 300), QUERY_ID
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                                 'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
            UNION ALL
            SELECT CHANGE_SEEN_AT, 'WH_CHANGE', COMPANY, 'INFO',
                   LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' || COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 300),
                   CHANGE_ID
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            loaded := loaded || 'timeline ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS
            WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_QUERY_FAMILY_DAILY',
                                  'FACT_QUERY_ROLE_HOURLY', 'FACT_QUERY_SCHEMA_HOURLY',
                                  'MART_TAG_COVERAGE_DAILY', 'MART_COST_ALLOCATION_DAILY',
                                  'FACT_COST_ALLOC_XDIM_DAILY', 'MART_TASK_GRAPH_DAILY',
                                  'MART_INCIDENT_TIMELINE')
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = :loaded
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN

        -- [7] security posture ------------------------------------------------
        BEGIN
            -- V041 R11: SHOW -> RESULT_SCAN once daily (V024 precedent), so
            -- Security stops paying a SHOW + parse per render.
            SHOW WAREHOUSES LIMIT 500;
            CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR AS
            SELECT "name"::VARCHAR AS WAREHOUSE_NAME,
                   COALESCE("resource_monitor"::VARCHAR, 'null') AS RESOURCE_MONITOR,
                   TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND
            FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
            USING (
                SELECT CURRENT_DATE() AS DAY, 'EXPIRING_CRED_10D' AS METRIC, 'ALL' AS COMPANY,
                       COUNT(*)::NUMBER(18,2) AS VALUE
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL
                  AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'EXPIRED_CRED', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()
                UNION ALL
                SELECT CURRENT_DATE(), 'ADMIN_STMTS_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                UNION ALL
                SELECT CURRENT_DATE(), 'GRANT_CHANGES_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                UNION ALL
                -- V041 R9: unused-role posture from the role-hour fact, not a
                -- 90d QUERY_HISTORY anti-join. Coverage-gated: HAVING emits NO
                -- row (never a lying zero) until the fact spans the window.
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY q
                      WHERE q.HOUR_TS >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)
                       <= DATEADD('day', -89, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND U.DISABLED = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'BREAKGLASS_GRANTS_30D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                  AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_MONITOR', 'ALL',
                       COUNT_IF(LOWER(TRIM(RESOURCE_MONITOR)) IN ('null', '', 'none'))
                FROM _OW_WH_MONITOR
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_AUTOSUSPEND', 'ALL',
                       COUNT_IF(COALESCE(AUTO_SUSPEND, 0) <= 0)
                FROM _OW_WH_MONITOR
            ) s
            ON t.DAY = s.DAY AND t.METRIC = s.METRIC AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, METRIC, COMPANY, VALUE)
            VALUES (s.DAY, s.METRIC, s.COMPANY, s.VALUE);
            loaded := loaded || 'posture ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_SECURITY_POSTURE_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                SELECT c.USAGE_TIME::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM (
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                    UNION ALL
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI'
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                ) c
                LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u ON u.USER_ID = c.USER_ID
                GROUP BY 1, 2, 3
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, SOURCE, MODEL_NAME, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_code ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (code views) - other marts unaffected', CURRENT_ROLE();
        END;

        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                SELECT START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, SOURCE, MODEL_NAME, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_functions ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', CURRENT_ROLE();
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS
            WHERE SOURCE_NAME IN ('MART_SECURITY_POSTURE_DAILY', 'FACT_AI_USAGE_DAILY')
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = :loaded
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);

    END IF;

    RETURN 'V27 marts loaded (' || :SCOPE || ', ' || :d || 'd): ' || :loaded;
END;
$$;

-- ---------------------------------------------------------------------------
-- R7: Operations diagnostics from the extract — top-20/hour by elapsed +
-- failure families per hour. Company via COMPANY_FOR_WAREHOUSE on plain
-- columns outside any aggregation (V030 shape law).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    d INT;
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 3))::INT;  -- extract holds 3d

    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
     WHERE HOUR_TS >= DATEADD('day', -:d, CURRENT_TIMESTAMP());

    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
        (HOUR_TS, KIND, COMPANY, QUERY_ID, START_TIME, USER_NAME, WAREHOUSE_NAME,
         WAREHOUSE_SIZE, DATABASE_NAME, QUERY_TYPE, EXECUTION_STATUS, ELAPSED_SEC,
         QUEUED_SEC, SPILL_REMOTE_GB, QUERY_PREVIEW)
    SELECT DATE_TRUNC('hour', e.START_TIME)::TIMESTAMP_NTZ,
           'TOP_ELAPSED',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(e.WAREHOUSE_NAME, '')),
           e.QUERY_ID, e.START_TIME, e.USER_NAME, e.WAREHOUSE_NAME, e.WAREHOUSE_SIZE,
           e.DATABASE_NAME, e.QUERY_TYPE, e.EXECUTION_STATUS,
           e.TOTAL_ELAPSED_TIME / 1000.0,
           (COALESCE(e.QUEUED_OVERLOAD_TIME, 0) + COALESCE(e.QUEUED_PROVISIONING_TIME, 0)) / 1000.0,
           COALESCE(e.BYTES_SPILLED_TO_REMOTE_STORAGE, 0) / POWER(1024, 3),
           LEFT(e.QUERY_TEXT, 180)
    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT e
    WHERE e.START_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY DATE_TRUNC('hour', e.START_TIME)
                               ORDER BY e.TOTAL_ELAPSED_TIME DESC NULLS LAST) <= 20;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
        (HOUR_TS, KIND, COMPANY, ERROR_CODE, ERROR_MESSAGE, FAILURES, USERS_AFFECTED, LAST_SEEN)
    SELECT m.HOUR_TS, 'FAIL_FAMILY', m.COMPANY, m.ERROR_CODE, m.ERROR_MESSAGE,
           SUM(m.CNT), COUNT(DISTINCT m.USER_NAME), MAX(m.LAST_SEEN)
    FROM (
        SELECT g.HOUR_TS, g.ERROR_CODE, g.ERROR_MESSAGE, g.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
               g.CNT, g.LAST_SEEN
        FROM (
            SELECT DATE_TRUNC('hour', e.START_TIME)::TIMESTAMP_NTZ AS HOUR_TS,
                   COALESCE(e.ERROR_CODE, 'UNKNOWN') AS ERROR_CODE,
                   LEFT(COALESCE(e.ERROR_MESSAGE, 'Unknown error'), 140) AS ERROR_MESSAGE,
                   COALESCE(e.USER_NAME, 'UNKNOWN') AS USER_NAME,
                   COALESCE(e.WAREHOUSE_NAME, '') AS WAREHOUSE_NAME,
                   COUNT(*) AS CNT,
                   MAX(e.START_TIME) AS LAST_SEEN
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT e
            WHERE e.START_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
              AND e.EXECUTION_STATUS = 'FAIL'
            GROUP BY 1, 2, 3, 4, 5
        ) g
    ) m
    GROUP BY m.HOUR_TS, m.COMPANY, m.ERROR_CODE, m.ERROR_MESSAGE;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_OPS_DIAG_HOURLY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'ops diagnostics loaded (' || :d || 'd)';
END;
$$;

-- ---------------------------------------------------------------------------
-- R8: platform-score inputs, one row per day (internal facts only — cheap).
-- Weights stay in Python (configurable without a reload).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    d INT;
BEGIN
    d := GREATEST(7, LEAST(COALESCE(DAYS_BACK, 30), 120))::INT;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY t
    USING (
        WITH spend AS (
            SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED
            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())
            GROUP BY DAY
        ),
        q AS (
            SELECT DATE(HOUR_TS) AS DAY,
                   SUM(QUERY_COUNT) AS QUERY_COUNT,
                   SUM(FAILED_COUNT) AS FAILED_COUNT,
                   SUM(QUEUED_SEC_SUM) AS QUEUED_SEC,
                   SUM(SPILL_REMOTE_GB) AS SPILL_GB
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('day', -:d, CURRENT_DATE())
            GROUP BY 1
        ),
        tk AS (
            SELECT DAY, SUM(RUNS) AS TASK_RUNS, SUM(FAILED) AS TASK_FAILED
            FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
            WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())
            GROUP BY DAY
        ),
        a AS (
            SELECT DATE(RAISED_AT) AS DAY,
                   COUNT_IF(UPPER(SEVERITY) = 'CRITICAL') AS CRIT_RAISED,
                   COUNT_IF(UPPER(SEVERITY) = 'HIGH') AS HIGH_RAISED
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('day', -:d, CURRENT_DATE())
            GROUP BY 1
        )
        SELECT spend.DAY,
               spend.CREDITS_BILLED,
               COALESCE(q.QUERY_COUNT, 0)  AS QUERY_COUNT,
               COALESCE(q.FAILED_COUNT, 0) AS FAILED_COUNT,
               COALESCE(q.QUEUED_SEC, 0)   AS QUEUED_SEC,
               COALESCE(q.SPILL_GB, 0)     AS SPILL_GB,
               COALESCE(tk.TASK_RUNS, 0)   AS TASK_RUNS,
               COALESCE(tk.TASK_FAILED, 0) AS TASK_FAILED,
               COALESCE(a.CRIT_RAISED, 0)  AS CRIT_RAISED,
               COALESCE(a.HIGH_RAISED, 0)  AS HIGH_RAISED
        FROM spend
        LEFT JOIN q ON q.DAY = spend.DAY
        LEFT JOIN tk ON tk.DAY = spend.DAY
        LEFT JOIN a ON a.DAY = spend.DAY
    ) s
    ON t.DAY = s.DAY
    WHEN MATCHED THEN UPDATE SET
        CREDITS_BILLED = s.CREDITS_BILLED, QUERY_COUNT = s.QUERY_COUNT,
        FAILED_COUNT = s.FAILED_COUNT, QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB,
        TASK_RUNS = s.TASK_RUNS, TASK_FAILED = s.TASK_FAILED, CRIT_RAISED = s.CRIT_RAISED,
        HIGH_RAISED = s.HIGH_RAISED, LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, CREDITS_BILLED, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB,
         TASK_RUNS, TASK_FAILED, CRIT_RAISED, HIGH_RAISED)
    VALUES (s.DAY, s.CREDITS_BILLED, s.QUERY_COUNT, s.FAILED_COUNT, s.QUEUED_SEC,
            s.SPILL_GB, s.TASK_RUNS, s.TASK_FAILED, s.CRIT_RAISED, s.HIGH_RAISED);

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_PLATFORM_SCORE_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'platform score inputs loaded (' || :d || 'd)';
END;
$$;

-- ---------------------------------------------------------------------------
-- R4: exec board v2 — all five config windows, single-pass KPI aggregation,
-- atomic stage -> SWAP visibility. The three panels with zero readers
-- (PRESSURE_QUEUE, PRESSURE_SPILL, DB_MIX) are no longer produced.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(VALUE)), 3.68) INTO :credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD';

    -- Build into the stage; readers keep the old board until the SWAP (the
    -- V003 DELETE+INSERT gap stranded Overview on the live fallback hourly).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE
        (COMPANY, WINDOW_DAYS, PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER)
    WITH scopes AS (
        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'
    ),
    windows AS (
        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
        UNION ALL SELECT 60 UNION ALL SELECT 90
    ),
    -- Aggregate each fact ONCE at (COMPANY, DAY[, dim]) grain; the
    -- scope-window expansion joins these small frames, never the raw facts.
    wh_daily AS (
        SELECT COMPANY, DAY, WAREHOUSE_NAME, SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY 1, 2, 3
    ),
    qh_daily AS (
        SELECT COMPANY, DATE(HOUR_TS) AS DAY,
               SUM(QUERY_COUNT) AS QUERIES, SUM(FAILED_COUNT) AS FAILED,
               SUM(QUEUED_SEC_SUM) AS QUEUED_SEC, SUM(SPILL_REMOTE_GB) AS SPILL_GB
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        WHERE HOUR_TS >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    wh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DAY, f.WAREHOUSE_NAME, f.CREDITS
        FROM wh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    qh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS,
               f.QUERIES, f.FAILED, f.QUEUED_SEC, f.SPILL_GB
        FROM qh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.RUNS, f.FAILED
        FROM tk_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    -- One aggregation pass per source; the KPI arms below just unpivot these.
    wh_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(CREDITS) AS CREDITS
        FROM wh GROUP BY 1, 2
    ),
    qh_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(QUERIES) AS QUERIES, SUM(FAILED) AS FAILED,
               SUM(QUEUED_SEC) AS QUEUED_SEC, SUM(SPILL_GB) AS SPILL_GB
        FROM qh GROUP BY 1, 2
    ),
    tk_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM tk GROUP BY 1, 2
    )
    -- KPI panel (unpivoted from the single-pass aggregates) ------------------
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'CREDITS', NULL, NULL,
           CREDITS, ROUND(CREDITS * :credit_price, 2), 'credits', 10
    FROM wh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUERIES', NULL, NULL,
           QUERIES, NULL, 'count', 20
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'FAILED_QUERIES', NULL, NULL,
           FAILED, NULL, 'count', 30
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUEUED_MINUTES', NULL, NULL,
           ROUND(QUEUED_SEC / 60, 1), NULL, 'minutes', 40
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'SPILL_GB', NULL, NULL,
           ROUND(SPILL_GB, 2), NULL, 'gb', 50
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_RUNS', NULL, NULL,
           RUNS, NULL, 'count', 60
    FROM tk_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_FAILURES', NULL, NULL,
           FAILED, NULL, 'count', 70
    FROM tk_kpi
    -- Daily spend panel -------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'DAILY_SPEND', 'CREDITS', NULL, DAY,
           SUM(CREDITS), ROUND(SUM(CREDITS) * :credit_price, 2), 'credits/day', 10
    FROM wh GROUP BY 1, 2, DAY
    -- Cost drivers ------------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER', 'CREDITS', WAREHOUSE_NAME, NULL,
           SUM(CREDITS), ROUND(SUM(CREDITS) * :credit_price, 2), 'credits', 10
    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME;

    ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
        SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_EXEC_BOARD' AS SOURCE_NAME, MAX(REFRESHED_AT) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'exec board refreshed (atomic swap)';
END;
$$;

-- ---------------------------------------------------------------------------
-- R5: the nightly reconcile — delete-and-rebuild the trailing 3 days so
-- restated ACCOUNT_USAGE rows and disappeared groups cannot survive stale
-- MERGE rows. FACT_QUERY_HOURLY self-heals (48h DELETE+INSERT each cycle)
-- and is deliberately not touched here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());

    -- Pull the watermarks back so the loaders re-cover the window.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'DAILY_FACTS');

    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(NULL);
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);

    RETURN 'nightly reconcile complete (trailing 3 days rebuilt)';
END;
$$;

-- ---------------------------------------------------------------------------
-- Freshness view: re-emitted with the four new arms (still the manual
-- SP_SNAPSHOT_FRESHNESS source and the pre-V040 fallback).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS
SELECT 'FACT_QUERY_HOURLY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT,
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0 AS HOURS_SINCE_LOAD
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
UNION ALL
SELECT 'FACT_WAREHOUSE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
UNION ALL
SELECT 'FACT_METERING_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
UNION ALL
SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
UNION ALL
SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
UNION ALL
SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
UNION ALL
SELECT 'MART_EXEC_BOARD', MAX(REFRESHED_AT), COUNT(*),
       DATEDIFF('minute', MAX(REFRESHED_AT), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
UNION ALL
SELECT 'MART_WAREHOUSE_EFFICIENCY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
UNION ALL
SELECT 'MART_QUERY_FAMILY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
UNION ALL
SELECT 'FACT_QUERY_ROLE_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
UNION ALL
SELECT 'FACT_QUERY_SCHEMA_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
UNION ALL
SELECT 'MART_COST_ALLOCATION_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
UNION ALL
SELECT 'MART_TASK_GRAPH_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
UNION ALL
SELECT 'MART_SECURITY_POSTURE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
UNION ALL
SELECT 'MART_INCIDENT_TIMELINE', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
UNION ALL
SELECT 'FACT_AI_USAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
UNION ALL
SELECT 'MART_TAG_COVERAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
UNION ALL
SELECT 'MART_LOCK_WAIT_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY
UNION ALL
SELECT 'MART_PATTERN_COST_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
UNION ALL
SELECT 'OW_QH_EXTRACT', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
UNION ALL
SELECT 'FACT_COST_ALLOC_XDIM_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
UNION ALL
SELECT 'MART_OPS_DIAG_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
UNION ALL
SELECT 'FACT_PLATFORM_SCORE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY;

-- ---------------------------------------------------------------------------
-- Task graph (r15 #6 phasing). Roots suspended for child surgery; EVERY task
-- this migration touches gets an explicit RESUME below, and the file ends
-- with SYSTEM$TASK_DEPENDENTS_ENABLE on BOTH roots — the 07-12 alert-outage
-- class (children left suspended) must be impossible to reintroduce here.
-- ---------------------------------------------------------------------------
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY SUSPEND;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(NULL);

-- Re-pointed: the V27 hourly marts consume the extract, so they run after it.
CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_OPS_DIAG_HOURLY
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(2);

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE();

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);

-- R6: the 10-minute snapshot task retires (144 wakes/day); the loaders own
-- their freshness rows now. SP_SNAPSHOT_FRESHNESS stays for manual refresh.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS SUSPEND;
DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS;

-- ---------------------------------------------------------------------------
-- First fills (3d extract covers every consumer window: 48h fact, 2d marts)
-- ---------------------------------------------------------------------------
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 3);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(2);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

-- ---------------------------------------------------------------------------
-- Resume everything this migration touched, then belt-and-braces both roots'
-- whole dependent trees.
-- ---------------------------------------------------------------------------
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_OPS_DIAG_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 41 AS VERSION, 'loader efficiency: staged QH extract + watermarks + nightly reconcile, xdim alloc fact, exec board v2 (5 windows, atomic swap), loader-owned freshness (+GENERATION), ops-diag + platform-score marts, posture from role fact + monitor counts, pseudo-wh filter in V27 sources' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 41);
