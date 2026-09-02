-- =====================================================================
--  OVERWATCH — RUN_NEXT.sql   (owner-applied Snowsight migration handoff)
--
--  APPLY THE ONE PENDING MIGRATION: V124 (per-table storage mart).
--
--  Context: V118-V123 were applied on 2026-09-02, so MAX(VERSION) should read
--  123 before you start. V124 is a NEW, additive, self-contained mart:
--    * NEW TABLE  MART_TABLE_STORAGE_DAILY (per-table active/time-travel/
--      fail-safe/clone bytes + retention + 90d LAST_DML + company)
--    * NEW PROC   SP_LOAD_TABLE_STORAGE_MART (snapshots today, prunes history)
--    * NEW TASK   TASK_LOAD_TABLE_STORAGE (daily 07:10 America/Chicago), RESUMEd
--    * first fill runs in-file (CALL ...(14)), so the mart-first storage panels
--      serve immediately.
--  It is NOT a re-derivation of the byte-locked SP_LOAD_MARTS_V27, and it never
--  provisions or alters compute. Idempotent; safe to re-run.
--
--  HOW TO RUN: open this file on GitHub -> Copy raw -> paste into a Snowsight
--  worksheet -> Run All. Confirm MAX(VERSION)=124 at the end, then paste the two
--  RESULT blocks back into the chat.
-- =====================================================================

-- ---- PRE-FLIGHT: expect SCHEMA_VERSION_BEFORE = 123 -------------------
SELECT MAX(VERSION) AS SCHEMA_VERSION_BEFORE FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;

-- =====================================================================
-- APPLY V124__table_storage_mart.sql  (byte-identical to snowflake/migrations/)
-- =====================================================================
-- V124__table_storage_mart.sql — per-TABLE storage snapshot mart (perf audit 2026-09-02).
--
--   storage_waste (Cost > Optimize) and table_storage_breakdown (the Cost > Spend AND
--   Cost > Optimize per-database drills) each scan ACCOUNT_USAGE.TABLE_STORAGE_METRICS
--   (one row per table) + a 90-day TABLE_DML_HISTORY aggregation (LAST_DML) + a
--   TABLES retention join, LIVE, on every render of those panels. TSM is current-state
--   and low-churn (per-table on-disk / time-travel / fail-safe / clone bytes), so a
--   once-daily snapshot mart serves all of them from a fast pre-aggregated read; the
--   live builders stay as the fallback (mart-first) and the pre-V124 path.
--
--   NEW MART_TABLE_STORAGE_DAILY (DAY x TABLE_ID): the raw byte columns + retention +
--   LAST_DML + COMPANY (COMPANY_FOR_DATABASE-stamped at load, matching the live scope
--   axis), snapshotted daily. SP_LOAD_TABLE_STORAGE_MART re-snapshots today and prunes
--   history; a daily 07:10 America/Chicago task keeps it fresh. FACT_STORAGE_ACCOUNT_DAILY
--   (V046) is the ACCOUNT-level tier truth; this is its per-TABLE complement.
--
--   Additive + contained: a NEW table + NEW standalone proc + NEW task, NOT a
--   re-derivation of the byte-locked SP_LOAD_MARTS_V27. Idempotent; safe to re-run.
--   Apply AFTER V123.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20124, 'V124 requires V123 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 123) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Per-table storage snapshot. Raw bytes (the reader converts to GB / prices at $/TiB),
-- COMPANY stamped by COMPANY_FOR_DATABASE so the reader scopes by a pre-computed column
-- (the SAME axis the live builders filter by via database_company_scope).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY (
    DAY                      DATE          NOT NULL,
    TABLE_ID                 NUMBER,
    DATABASE_NAME            VARCHAR(256),
    SCHEMA_NAME              VARCHAR(256),
    TABLE_NAME               VARCHAR(256),
    COMPANY                  VARCHAR(20),
    ACTIVE_BYTES             NUMBER(24,0),
    TIME_TRAVEL_BYTES        NUMBER(24,0),
    FAILSAFE_BYTES           NUMBER(24,0),
    RETAINED_FOR_CLONE_BYTES NUMBER(24,0),
    RETENTION_DAYS           NUMBER(9,0),
    LAST_DML                 TIMESTAMP_NTZ,
    LOAD_TS                  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_TABLE_STORAGE_MART(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    keep INT;
BEGIN
    -- TSM is current-state (one row per table), so there is no backfill window: each run
    -- snapshots TODAY. DAYS_BACK bounds how many days of snapshot history to retain (the
    -- reader only reads the latest DAY; a little history is cheap and lets a growth view
    -- reuse it later). Default retain 14 days.
    keep := GREATEST(COALESCE(:DAYS_BACK, 14), 1)::INT;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY WHERE DAY = CURRENT_DATE();

    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY
        (DAY, TABLE_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, COMPANY,
         ACTIVE_BYTES, TIME_TRAVEL_BYTES, FAILSAFE_BYTES, RETAINED_FOR_CLONE_BYTES,
         RETENTION_DAYS, LAST_DML)
    SELECT
        CURRENT_DATE() AS DAY,
        m.ID AS TABLE_ID,
        m.TABLE_CATALOG AS DATABASE_NAME,
        m.TABLE_SCHEMA AS SCHEMA_NAME,
        m.TABLE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(m.TABLE_CATALOG) AS COMPANY,
        m.ACTIVE_BYTES,
        m.TIME_TRAVEL_BYTES,
        m.FAILSAFE_BYTES,
        COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) AS RETAINED_FOR_CLONE_BYTES,
        rt.RETENTION_DAYS,
        d.LAST_DML
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m
    LEFT JOIN (
        SELECT TABLE_ID, MAX(END_TIME) AS LAST_DML
        FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY
        WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
        GROUP BY 1
    ) d ON d.TABLE_ID = m.ID
    LEFT JOIN (
        SELECT TABLE_ID, MAX(RETENTION_TIME) AS RETENTION_DAYS
        FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
        WHERE DELETED IS NULL
        GROUP BY 1
    ) rt ON rt.TABLE_ID = m.ID
    WHERE m.DELETED = FALSE
      AND m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES
          + COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) > 0;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY
    WHERE DAY < DATEADD('day', -:keep, CURRENT_DATE());

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_TABLE_STORAGE_DAILY' AS SOURCE_NAME,
               MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY
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

-- First fill so the mart-first readers serve immediately (retain 14 days of history).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_TABLE_STORAGE_MART(14);

-- Daily refresh at 07:10 America/Chicago (after the 06:30-06:50 root-task cluster).
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_TABLE_STORAGE
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 10 7 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_TABLE_STORAGE_MART(14);

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_TABLE_STORAGE RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 124 AS VERSION,
       'per-table storage mart: MART_TABLE_STORAGE_DAILY + SP_LOAD_TABLE_STORAGE_MART + daily 07:10 task (per-table active/time-travel/fail-safe/clone bytes + retention + 90d LAST_DML + COMPANY_FOR_DATABASE-stamped company, snapshotted from TABLE_STORAGE_METRICS). storage_waste + table_storage_breakdown read it mart-first with the live scan as fallback, so the Spend + Optimize storage panels stop re-scanning TABLE_STORAGE_METRICS/TABLE_DML_HISTORY live on every render. NEW proc/task, no re-derivation of the byte-locked loaders.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 124);


-- ---- VERIFICATION: expect one row VERSION=124, and MAX(VERSION)=124 ---
SELECT VERSION, DESCRIPTION FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 124;
SELECT MAX(VERSION) AS SCHEMA_VERSION_AFTER,
       (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_TABLE_STORAGE_DAILY) AS MART_TABLE_STORAGE_ROWS
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
