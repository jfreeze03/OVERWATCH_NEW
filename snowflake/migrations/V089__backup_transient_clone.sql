-- V089__backup_transient_clone.sql
--
-- Fix SP_BACKUP_OPERATOR_TABLES: it cloned each operator table to a PERMANENT
-- *_BAK_LAST snapshot, but transient operator tables (ALERT_EVENTS, ACTION_QUEUE,
-- ...) cannot be cloned into a permanent object -- so those backups failed every
-- run (owner error log 2026-08-17: clone_failed x3 since 2026-08-02).
--
-- Re-derives the proc from the V075 base, changing only the clone target to
-- TRANSIENT (works for transient AND permanent sources; a backup needs no
-- Fail-safe). Idempotent CREATE OR REPLACE; supersedes V075's definition.
-- Owner applies in Snowsight after V088. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20089, 'V089 requires V088 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 88) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (TRANSIENT clone target, from V075)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    tables ARRAY DEFAULT [
        'SETTINGS', 'COMPANY_SCOPE', 'ALERT_CONFIG', 'ALERT_EVENTS',
        'ALERT_AUDIT', 'ACTION_QUEUE', 'SAVINGS_LEDGER', 'DEPARTMENT_MAP',
        'ALERT_ROUTES', 'REMEDIATION_LOG', 'USER_PREFS',
        'OBJECT_CHANGE_REGISTRY', 'WAREHOUSE_CHANGE_REGISTRY',
        'WAREHOUSE_CONFIG_SNAPSHOT', 'PIPELINE_SLA_CONFIG', 'DAILY_DIGEST',
        'DEPT_BUDGETS', 'INCIDENTS', 'INCIDENT_MEMBERS', 'ACTION_ACTIVITY',
        'EVIDENCE_LINKS', 'ENTITY_CATALOG', 'USER_WATCHLIST',
        'OPTIMIZATION_EXPERIMENTS', 'SLO_OBJECTIVES'
    ];
    tname VARCHAR;
    emsg VARCHAR;
    done INT DEFAULT 0;
    i INT;
BEGIN
    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO
        tname := GET(:tables, i)::VARCHAR;
        BEGIN
            -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,
            -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table
            -- ("Transient object cannot be cloned to a permanent object"),
            -- which failed those backups every run. TRANSIENT works for both
            -- transient and permanent sources and needs no Fail-safe.
            EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
                              '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
            done := done + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'BackupOperatorTables', 'clone_failed', :emsg,
                       'table ' || :tname || ' (missing on this install is fine)', CURRENT_ROLE();
        END;
    END FOR;
    RETURN 'cloned ' || :done || ' operator table(s) to *_BAK_LAST';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 89 AS VERSION,
       'SP_BACKUP_OPERATOR_TABLES clones to a TRANSIENT *_BAK_LAST target (was permanent): transient operator tables (ALERT_EVENTS, ACTION_QUEUE, ...) cannot clone into a permanent object, so those weekly backups failed every run (owner error log 2026-08-17, clone_failed x3 since 2026-08-02). TRANSIENT works for transient and permanent sources and needs no Fail-safe. Proc re-derived from V075; no data reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 89);
