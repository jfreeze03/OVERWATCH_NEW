-- teardown.sql — drop OVERWATCH objects for a clean drop-and-restore cycle.
--
-- SAFETY MODEL
--   * DBA_MAINT_DB.OVERWATCH is SHARED with the previous app's objects.
--     This script NEVER drops the schema or database — only the objects the
--     V001..V005 migrations create, each by fully qualified name.
--   * Section A (default): rebuildable objects only — tasks, procs, functions,
--     views, transient fact/mart tables. Re-running V001..V005 restores them
--     and the loaders repopulate from ACCOUNT_USAGE. No operator data is lost.
--   * Section B (commented out): OPERATOR DATA — settings, alert lifecycle,
--     action queue, savings ledger, audit/error logs. Only uncomment for a
--     true factory reset, and take the clone backups first.
--   * Section C (commented out): shared infrastructure — warehouse, resource
--     monitor, Streamlit app object, roles.
--
-- RESTORE
--   1. Re-run snowflake/migrations/V001..V005 in order, then roles.sql.
--   2. Run snowflake/validate.sql — every row should be OK.
--   3. Accidentally dropped a permanent table? Time Travel has your back:
--        UNDROP TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER;
--      (works within the retention window; transient tables have 0-1 days).
--
-- Run as the deployment role that owns the objects (see DEPLOYMENT.md).

-- ===========================================================================
-- 0. PREFLIGHT — stop all scheduled work before dropping anything
-- ===========================================================================
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY       SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY        SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN        SUSPEND;
ALTER ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS  SUSPEND;
ALTER ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS SUSPEND;

-- ===========================================================================
-- A. REBUILDABLE OBJECTS (safe teardown — the default)
-- ===========================================================================

-- A1. Task chain (children first, then roots)
DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD;
DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN;
DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY;
DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY;

-- A2. Native alert objects (opt-in delivery templates)
DROP ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS;
DROP ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS;

-- A3. Procedures
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();

-- A4. Functions
DROP FUNCTION IF EXISTS DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(VARCHAR);
DROP FUNCTION IF EXISTS DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(VARCHAR);
DROP FUNCTION IF EXISTS DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(VARCHAR);

-- A5. Views
DROP VIEW IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS;
DROP VIEW IF EXISTS DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_STATUS;

-- A6. Transient facts + marts (rebuilt from ACCOUNT_USAGE by the loaders)
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY;

-- ===========================================================================
-- B. OPERATOR DATA — DESTRUCTIVE. Uncomment only for a factory reset.
--    Take the clone backups FIRST; they are your Time-Travel-independent copy.
-- ===========================================================================

-- B0. Backups (run these, verify row counts, then proceed)
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.SETTINGS_BAK_20260707       CLONE DBA_MAINT_DB.OVERWATCH.SETTINGS;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE_BAK_20260707  CLONE DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_20260707   CLONE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS_BAK_20260707   CLONE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT_BAK_20260707    CLONE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE_BAK_20260707   CLONE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER_BAK_20260707 CLONE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG_BAK_20260707  CLONE DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION_BAK_20260707 CLONE DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
-- CREATE TABLE DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_CONFIG_BAK_20260707 CLONE DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_CONFIG;

-- B1. Drops (order irrelevant; nothing references these)
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SETTINGS;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_CONFIG;

-- To restore operator data after re-running migrations, INSERT ... SELECT from
-- the *_BAK_* clones (column lists match), then drop the clones.

-- ===========================================================================
-- C. SHARED INFRASTRUCTURE — uncomment only if you really mean it.
-- ===========================================================================
-- The warehouse also serves the Streamlit app; the resource monitor caps it;
-- roles may be granted into your role hierarchy.
-- DROP STREAMLIT IF EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;
-- ALTER WAREHOUSE IF EXISTS WH_ALFA_OVERWATCH SET RESOURCE_MONITOR = NULL;
-- DROP WAREHOUSE IF EXISTS WH_ALFA_OVERWATCH;
-- DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;
-- DROP ROLE IF EXISTS OVERWATCH_OPERATOR;
-- DROP ROLE IF EXISTS OVERWATCH_MONITOR;

-- ===========================================================================
-- VERIFY — after Section A, only operator-data tables should remain;
-- after A+B, this should return zero rows.
-- ===========================================================================
SELECT TABLE_NAME AS REMAINING_OVERWATCH_OBJECT, TABLE_TYPE
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'OVERWATCH'
  AND TABLE_NAME IN (
      'MART_EXEC_BOARD', 'MART_SOURCE_FRESHNESS',
      'FACT_METERING_DAILY', 'FACT_WAREHOUSE_DAILY', 'FACT_QUERY_HOURLY',
      'FACT_TASK_DAILY', 'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY',
      'SETTINGS', 'COMPANY_SCOPE', 'SCHEMA_VERSION', 'APP_ERROR_LOG',
      'ALERT_CONFIG', 'ALERT_EVENTS', 'ALERT_AUDIT',
      'ACTION_QUEUE', 'SAVINGS_LEDGER',
      'PIPELINE_SLA_CONFIG', 'PIPELINE_SLA_STATUS'
  )
ORDER BY TABLE_NAME;
