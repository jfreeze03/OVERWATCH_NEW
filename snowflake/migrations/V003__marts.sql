-- V003__marts.sql — first-paint executive board, source freshness, task chain.
-- Idempotent.

-- ---------------------------------------------------------------------------
-- MART_EXEC_BOARD: the one deliberate first-paint aggregate. Rebuilt hourly
-- for (ALFA, Trexis, ALL) x (7, 30) day windows. Panel/metric/dimension rows
-- keep the reader trivial and the page instant.
-- ---------------------------------------------------------------------------
CREATE TRANSIENT TABLE IF NOT EXISTS OVERWATCH.MART.MART_EXEC_BOARD (
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

CREATE OR REPLACE PROCEDURE OVERWATCH.MART.SP_REFRESH_EXEC_BOARD()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(VALUE)), 3.68) INTO :credit_price
    FROM OVERWATCH.CORE.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD';

    DELETE FROM OVERWATCH.MART.MART_EXEC_BOARD;

    -- One scoped fact view per company label, reused by every panel below.
    INSERT INTO OVERWATCH.MART.MART_EXEC_BOARD
        (COMPANY, WINDOW_DAYS, PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER)
    WITH scopes AS (
        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'
    ),
    windows AS (
        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 30
    ),
    wh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DAY, f.WAREHOUSE_NAME,
               f.CREDITS_TOTAL
        FROM OVERWATCH.MART.FACT_WAREHOUSE_DAILY f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    qh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.*
        FROM OVERWATCH.MART.FACT_QUERY_HOURLY f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.HOUR_TS >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.*
        FROM OVERWATCH.MART.FACT_TASK_DAILY f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    )
    -- KPI panel -------------------------------------------------------------
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'CREDITS', NULL, NULL,
           SUM(CREDITS_TOTAL), ROUND(SUM(CREDITS_TOTAL) * :credit_price, 2), 'credits', 10
    FROM wh GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUERIES', NULL, NULL,
           SUM(QUERY_COUNT), NULL, 'count', 20
    FROM qh GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'FAILED_QUERIES', NULL, NULL,
           SUM(FAILED_COUNT), NULL, 'count', 30
    FROM qh GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUEUED_MINUTES', NULL, NULL,
           ROUND(SUM(QUEUED_SEC_SUM) / 60, 1), NULL, 'minutes', 40
    FROM qh GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'SPILL_GB', NULL, NULL,
           ROUND(SUM(SPILL_REMOTE_GB), 2), NULL, 'gb', 50
    FROM qh GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_RUNS', NULL, NULL,
           SUM(RUNS), NULL, 'count', 60
    FROM tk GROUP BY 1, 2
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_FAILURES', NULL, NULL,
           SUM(FAILED), NULL, 'count', 70
    FROM tk GROUP BY 1, 2
    -- Daily spend panel -----------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'DAILY_SPEND', 'CREDITS', NULL, DAY,
           SUM(CREDITS_TOTAL), ROUND(SUM(CREDITS_TOTAL) * :credit_price, 2), 'credits/day', 10
    FROM wh GROUP BY 1, 2, DAY
    -- Cost drivers ------------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER', 'CREDITS', WAREHOUSE_NAME, NULL,
           SUM(CREDITS_TOTAL), ROUND(SUM(CREDITS_TOTAL) * :credit_price, 2), 'credits', 10
    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME
    -- Warehouse pressure ------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'PRESSURE_QUEUE', 'QUEUED_SEC', WAREHOUSE_NAME, NULL,
           ROUND(SUM(QUEUED_SEC_SUM), 1), NULL, 'seconds', 10
    FROM qh WHERE WAREHOUSE_NAME IS NOT NULL GROUP BY 1, 2, WAREHOUSE_NAME
    HAVING SUM(QUEUED_SEC_SUM) > 0
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'PRESSURE_SPILL', 'SPILL_GB', WAREHOUSE_NAME, NULL,
           ROUND(SUM(SPILL_REMOTE_GB), 2), NULL, 'gb', 10
    FROM qh WHERE WAREHOUSE_NAME IS NOT NULL GROUP BY 1, 2, WAREHOUSE_NAME
    HAVING SUM(SPILL_REMOTE_GB) > 0
    -- Database mix ------------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'DB_MIX', 'QUERIES', COALESCE(DATABASE_NAME, 'UNKNOWN'), NULL,
           SUM(QUERY_COUNT), NULL, 'count', 10
    FROM qh GROUP BY 1, 2, COALESCE(DATABASE_NAME, 'UNKNOWN');

    RETURN 'exec board refreshed';
END;
$$;

-- ---------------------------------------------------------------------------
-- Source freshness (drives honest staleness labels in the app)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW OVERWATCH.MART.MART_SOURCE_FRESHNESS AS
SELECT 'FACT_QUERY_HOURLY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT,
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0 AS HOURS_SINCE_LOAD
FROM OVERWATCH.MART.FACT_QUERY_HOURLY
UNION ALL
SELECT 'FACT_WAREHOUSE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.FACT_WAREHOUSE_DAILY
UNION ALL
SELECT 'FACT_METERING_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.FACT_METERING_DAILY
UNION ALL
SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.FACT_TASK_DAILY
UNION ALL
SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.FACT_LOGIN_DAILY
UNION ALL
SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.FACT_STORAGE_DAILY
UNION ALL
SELECT 'MART_EXEC_BOARD', MAX(REFRESHED_AT), COUNT(*),
       DATEDIFF('minute', MAX(REFRESHED_AT), CURRENT_TIMESTAMP()) / 60.0
FROM OVERWATCH.MART.MART_EXEC_BOARD;

-- ---------------------------------------------------------------------------
-- Chain: refresh the board right after each hourly fact load
-- ---------------------------------------------------------------------------
CREATE TASK IF NOT EXISTS OVERWATCH.MART.TASK_REFRESH_EXEC_BOARD
    WAREHOUSE = OVERWATCH_WH
    AFTER OVERWATCH.MART.TASK_LOAD_HOURLY
AS
    CALL OVERWATCH.MART.SP_REFRESH_EXEC_BOARD();

MERGE INTO OVERWATCH.CORE.SCHEMA_VERSION t
USING (SELECT 3 AS VERSION, 'marts: exec board + refresh proc/task, source freshness view' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);

SELECT 'V003 applied' AS STATUS;
