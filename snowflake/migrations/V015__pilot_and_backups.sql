-- V015__pilot_and_backups.sql — Dynamic Table pilot + weekly operator backups.
--
-- 1. MART_SPEND_ROLLUP_DT: one deliberately low-risk Dynamic Table so the
--    MERGE-vs-DT question is answered with measured refresh cost (see it in
--    Operations > Pipeline SLA's DT health panel and METERING service types)
--    instead of argument. Sources OUR fact (DTs cannot source SNOWFLAKE
--    share views — no change tracking there). Additive: nothing reads it yet.
-- 2. SP_BACKUP_OPERATOR_TABLES + weekly task: zero-copy clones of every
--    operator-editable table to <NAME>_BAK_LAST. Time Travel covers the
--    fine grain; the weekly clone survives retention windows. Idempotent.

ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY SET CHANGE_TRACKING = TRUE;

CREATE DYNAMIC TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_SPEND_ROLLUP_DT
    TARGET_LAG = '6 hours'
    WAREHOUSE = WH_ALFA_OVERWATCH
    AS
SELECT DATE_TRUNC('month', DAY)::DATE AS MONTH,
       SERVICE_TYPE,
       SUM(CREDITS_USED) AS CREDITS_USED,
       SUM(CREDITS_BILLED) AS CREDITS_BILLED,
       COUNT(DISTINCT DAY) AS DAYS_LOADED
FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
GROUP BY 1, 2;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    tables ARRAY DEFAULT ['SETTINGS', 'COMPANY_SCOPE', 'ALERT_CONFIG', 'ALERT_EVENTS',
                          'ALERT_AUDIT', 'ACTION_QUEUE', 'SAVINGS_LEDGER', 'DEPARTMENT_MAP',
                          'ALERT_ROUTES', 'REMEDIATION_LOG', 'USER_PREFS',
                          'OBJECT_CHANGE_REGISTRY', 'PIPELINE_SLA_CONFIG', 'DAILY_DIGEST'];
    tname VARCHAR;
    emsg VARCHAR;
    done INT DEFAULT 0;
    i INT;
BEGIN
    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO
        tname := GET(:tables, i)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
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

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 40 5 * * 0 America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 15 AS VERSION,
       'DT pilot (spend rollup) + weekly operator-table clone backups' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
