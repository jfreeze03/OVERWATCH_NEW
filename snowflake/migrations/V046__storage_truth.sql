-- V046: storage truth (COST_DB reconciliation R3 + audit F1b, 2026-07-14).
-- Per-database storage (FACT_STORAGE_DAILY) covers table + fail-safe only and
-- the app now prices it on the monthly average of daily bytes (F1, app-side).
-- This migration adds the ACCOUNT-LEVEL tiers Snowflake bills but we ignored:
-- stage, hybrid-table, and archive cool/cold storage, sourced from
-- ACCOUNT_USAGE.STORAGE_USAGE (one row per day, account-wide — no per-database
-- grain exists for these tiers). A new fact + loader proc + daily task keep it
-- fresh; tier rates seed into SETTINGS (Admin-editable estimates).
--
-- NOTE: STORAGE_USAGE is Snowflake's own approximation and "won't match your
-- invoice exactly" (docs) — org USAGE_IN_CURRENCY remains billing truth. This
-- fact is a NEW proc/task, not a re-derivation of the byte-locked loaders.
-- Apply AFTER V045. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20046, 'V046 requires V045 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 45) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Account-level storage by tier (average daily bytes, billing basis).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY (
    DAY                DATE          NOT NULL,
    TABLE_BYTES        NUMBER(24,0),
    STAGE_BYTES        NUMBER(24,0),
    FAILSAFE_BYTES     NUMBER(24,0),
    HYBRID_BYTES       NUMBER(24,0),
    ARCHIVE_COOL_BYTES NUMBER(24,0),
    ARCHIVE_COLD_BYTES NUMBER(24,0),
    LOAD_TS            TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
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

-- Tier rates: seed only if absent (never clobber an Admin edit). Estimates
-- from AWS US-East list pricing; hybrid/archive bill differently from standard.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('STORAGE_STAGE_USD_PER_TB_MONTH',        '23.00'),
        ('STORAGE_HYBRID_USD_PER_TB_MONTH',       '348.16'),
        ('STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH', '4.00'),
        ('STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH', '1.00')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- First fill (90d) so the panel has history immediately.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(90);

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_STORAGE_TRUTH
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 30 6 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(3);

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_STORAGE_TRUTH RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 46 AS VERSION,
       'storage truth: FACT_STORAGE_ACCOUNT_DAILY + SP_LOAD_STORAGE_TRUTH + daily task (table/stage/failsafe/hybrid/archive cool+cold from STORAGE_USAGE); per-DB storage readers moved to monthly-average billing basis (F1); tier-rate SETTINGS seeded (R3/F1b)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 46);
