-- validate.sql — post-install checks. Every row should read OK.

WITH checks AS (
    SELECT 'V001..V005 applied' AS CHECK_NAME,
           IFF((SELECT COUNT(DISTINCT VERSION) FROM OVERWATCH.CORE.SCHEMA_VERSION WHERE VERSION BETWEEN 1 AND 5) = 5,
               'OK', 'FAIL: run missing migrations') AS RESULT
    UNION ALL
    SELECT 'Settings seeded',
           IFF((SELECT COUNT(*) FROM OVERWATCH.CORE.SETTINGS
                 WHERE KEY IN ('CREDIT_PRICE_USD', 'AI_CREDIT_PRICE_USD')) = 2,
               'OK', 'FAIL: rates missing from CORE.SETTINGS')
    UNION ALL
    SELECT 'Credit rate is 3.68 unless deliberately changed',
           IFF((SELECT VALUE FROM OVERWATCH.CORE.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD') IS NOT NULL,
               'OK', 'FAIL')
    UNION ALL
    SELECT 'Company scope seeded (Trexis warehouses)',
           IFF((SELECT COUNT(*) FROM OVERWATCH.CORE.COMPANY_SCOPE
                 WHERE COMPANY = 'Trexis' AND SCOPE_TYPE = 'WAREHOUSE') = 4,
               'OK', 'FAIL: expected 4 Trexis warehouses')
    UNION ALL
    SELECT 'KEBARR1 override present',
           IFF(EXISTS (SELECT 1 FROM OVERWATCH.CORE.COMPANY_SCOPE
                        WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = 'KEBARR1' AND COMPANY = 'ALFA'),
               'OK', 'FAIL: KEBARR1 must classify as ALFA')
    UNION ALL
    SELECT 'KEBARR1 classifies as ALFA',
           IFF(OVERWATCH.CORE.COMPANY_FOR_USER('KEBARR1') = 'ALFA', 'OK', 'FAIL')
    UNION ALL
    SELECT 'TRXS_ prefix classifies as Trexis',
           IFF(OVERWATCH.CORE.COMPANY_FOR_USER('TRXS_LOADER') = 'Trexis', 'OK', 'FAIL')
    UNION ALL
    SELECT 'Resource monitor attached to OVERWATCH_WH',
           IFF(EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSES
                        WHERE NAME = 'OVERWATCH_WH' AND DELETED IS NULL),
               'OK', 'CHECK: confirm OVERWATCH_WH + OVERWATCH_RM in SHOW WAREHOUSES')
    UNION ALL
    SELECT 'Alert rules seeded',
           IFF((SELECT COUNT(*) FROM OVERWATCH.CORE.ALERT_CONFIG) >= 7, 'OK', 'FAIL')
)
SELECT * FROM checks
UNION ALL
SELECT 'Task: ' || NAME,
       IFF(STATE = 'started', 'OK', 'CHECK: task not started (' || STATE || ')')
FROM TABLE(INFORMATION_SCHEMA.TASK_DEPENDENTS(TASK_NAME => 'OVERWATCH.MART.TASK_LOAD_HOURLY', RECURSIVE => TRUE))
ORDER BY 1;
