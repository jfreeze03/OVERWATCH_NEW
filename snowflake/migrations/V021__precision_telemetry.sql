-- V021: alert resolution kinds (precision tracking) + fleet query telemetry.
-- Idempotent. Re-run snowflake/roles.sql afterward so viewer roles can write
-- telemetry rows and operators can set resolution kinds.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

-- Ordering guard + version row (same contract as every prior migration).
EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20021, 'BLOCKED: SCHEMA_VERSION < 20 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 20) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

-- 1) Resolution kind: how a resolved alert was closed. Powers per-rule
--    precision (ACTIONED vs NOISE) on Alerts -> Rules for threshold tuning.
--    Values: ACTIONED | NOISE | EXPECTED (maintenance/known); NULL = untagged.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    ADD COLUMN IF NOT EXISTS RESOLUTION_KIND VARCHAR(20);

-- 2) Fleet-wide query telemetry: the in-session ring buffer only shows the
--    current user's session; this table collects SLOW (>=2s) and FAILED
--    fetches across every viewer so Admin -> Performance sees the real p95.
--    Sampled + fire-and-forget from the app; a lost row is acceptable.
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY (
    AT            TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PAGE          VARCHAR(80),
    TIER          VARCHAR(20),
    QUERY_KEY     VARCHAR(120),
    ELAPSED_MS    NUMBER(12,1),
    ROWS_RETURNED NUMBER(12,0),
    OK            BOOLEAN,
    ROLE_NAME     VARCHAR(200) DEFAULT CURRENT_ROLE()
);

-- Retention: piggybacks on SP_PURGE_FACTS' ERROR_LOG_RETENTION_DAYS pass is
-- NOT automatic for this table; keep it lean here instead (90d sliding).
CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.TASK_PURGE_QUERY_TELEMETRY
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 20 6 1 * * America/Chicago'
AS
    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY
    WHERE AT < DATEADD('day', -90, CURRENT_TIMESTAMP());

ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_PURGE_QUERY_TELEMETRY RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 21 AS VERSION, 'precision kinds + fleet telemetry' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
