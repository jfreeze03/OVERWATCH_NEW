-- V114__anomaly_sweep_after_daily_loader.sql
--
-- TASK_ANOMALY_SWEEP ran at 06:40, five minutes BEFORE its metering loader TASK_LOAD_DAILY (06:45), so
-- SP_ANOMALY_SWEEP's account/service arm scanned yesterday's FACT_METERING_DAILY and detected a credit
-- spike a day late (the warehouse arm reads FACT_WAREHOUSE_DAILY and is unaffected) -- data-loader
-- hunt 2026-08-30. Move the sweep to 07:00, comfortably after the 06:45 daily loader (and after the
-- 06:50 change-impact and 06:55 app-cost tasks), so the SERVICE arm sees the freshly loaded prior day.
--
-- Schedule-only change (SUSPEND -> SET SCHEDULE -> RESUME); no proc, no schema change. Owner applies in
-- Snowsight after V113. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20114, 'V114 requires V113 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 113) THEN
        RAISE not_ready;
    END IF;
END;
$$;

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP
    SET SCHEDULE = 'USING CRON 0 7 * * * America/Chicago';
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 114 AS VERSION,
       'Anomaly sweep after the daily loader: TASK_ANOMALY_SWEEP cron moved from 06:40 to 07:00 (via SUSPEND / SET SCHEDULE / RESUME) so it runs AFTER TASK_LOAD_DAILY (06:45) refreshes FACT_METERING_DAILY. SP_ANOMALY_SWEEP''s account/service arm no longer scans yesterday''s metering and detects a credit spike a day late. Schedule-only change; no proc, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 114);
