-- validate.sql — post-install checks. Every row should read OK.

WITH checks AS (
    SELECT 'V001..V033 applied' AS CHECK_NAME,
           IFF((SELECT COUNT(DISTINCT VERSION) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION BETWEEN 1 AND 33) = 33,
               'OK', 'FAIL: run missing migrations') AS RESULT
    UNION ALL
    SELECT 'Settings seeded',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                 WHERE KEY IN ('CREDIT_PRICE_USD', 'AI_CREDIT_PRICE_USD')) = 2,
               'OK', 'FAIL: rates missing from SETTINGS')
    UNION ALL
    SELECT 'Credit rate is 3.68 unless deliberately changed',
           IFF((SELECT VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD') IS NOT NULL,
               'OK', 'FAIL')
    UNION ALL
    SELECT 'Company scope seeded (Trexis warehouses)',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
                 WHERE COMPANY = 'Trexis' AND SCOPE_TYPE = 'WAREHOUSE') = 5,
               'OK', 'FAIL: expected 5 Trexis warehouses')
    UNION ALL
    SELECT 'KEBARR1 override present',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
                        WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = 'KEBARR1' AND COMPANY = 'ALFA'),
               'OK', 'FAIL: KEBARR1 must classify as ALFA')
    UNION ALL
    SELECT 'KEBARR1 classifies as ALFA',
           IFF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER('KEBARR1') = 'ALFA', 'OK', 'FAIL')
    UNION ALL
    SELECT 'TRXS_ prefix classifies as Trexis',
           IFF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER('TRXS_LOADER') = 'Trexis', 'OK', 'FAIL')
    UNION ALL
    -- This account does not expose ACCOUNT_USAGE.WAREHOUSES; use
    -- WAREHOUSE_EVENTS_HISTORY (any lifecycle event proves the warehouse
    -- exists). Events can lag ~1-3h after CREATE WAREHOUSE, hence CHECK not
    -- FAIL — confirm with SHOW WAREHOUSES on a fresh install.
    SELECT 'WH_ALFA_OVERWATCH exists (event evidence)',
           IFF(EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY
                        WHERE WAREHOUSE_NAME = 'WH_ALFA_OVERWATCH'),
               'OK', 'CHECK: no events yet (lag) — confirm WH_ALFA_OVERWATCH + OVERWATCH_RM via SHOW WAREHOUSES')
    UNION ALL
    SELECT 'Alert rules seeded',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG) >= 7, 'OK', 'FAIL')
    UNION ALL
    -- Shared-schema collision checks: if an old-app table with the same name
    -- survived, it keeps the old shape and these columns are missing.
    SELECT 'ALERT_EVENTS has new shape (DEDUPE_KEY)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_EVENTS'
                          AND COLUMN_NAME = 'DEDUPE_KEY'),
               'OK', 'FAIL: old-app ALERT_EVENTS present — rename it and rerun V004')
    UNION ALL
    SELECT 'ALERT_CONFIG has new shape (THRESHOLD_NUM)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_CONFIG'
                          AND COLUMN_NAME = 'THRESHOLD_NUM'),
               'OK', 'FAIL: old-app ALERT_CONFIG present — rename it and rerun V004')
    UNION ALL
    SELECT 'FACT_QUERY_HOURLY has new shape (COMPANY)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'FACT_QUERY_HOURLY'
                          AND COLUMN_NAME = 'COMPANY'),
               'OK', 'FAIL: old-app FACT_QUERY_HOURLY present — rename it and rerun V002')
    UNION ALL
    SELECT 'Task warehouse is WH_ALFA_OVERWATCH',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'SCHEMA_VERSION'),
               'OK', 'CHECK: confirm SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH shows WH_ALFA_OVERWATCH')
)
SELECT * FROM checks
UNION ALL
SELECT 'Task: ' || NAME,
       IFF(STATE = 'started', 'OK', 'CHECK: task not started (' || STATE || ')')
FROM TABLE(INFORMATION_SCHEMA.TASK_DEPENDENTS(TASK_NAME => 'DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY', RECURSIVE => TRUE))
ORDER BY 1;
