-- ===========================================================================
--  OWNER ACTION: apply migration V091 (auto-resolve cleared alerts) in Snowsight
--  Open snowflake/migrations/V091__alert_auto_clear.sql on GitHub -> Copy raw
--  contents -> paste into a Snowsight worksheet -> run. Apply AFTER V090 (you are
--  current through V090). It ADDs 2 ALERT_CONFIG columns, seeds 3 PERF rules, and
--  re-derives SP_ALERT_SCAN with the auto-clear sweep. Scanner not fired at apply.
--  https://github.com/jfreeze03/OVERWATCH_NEW/blob/main/snowflake/migrations/V091__alert_auto_clear.sql
-- ===========================================================================

-- ===========================================================================
--  OVERWATCH  -  RUN BOX  (RUN_NEXT.sql)          updated: 2026-08-25
-- ===========================================================================
--  WHAT THIS IS
--    The single rolling file for SQL Claude wants you to run in Snowsight.
--    It is overwritten each time there is something new to run.
--
--  HOW TO USE  (no retyping)
--    1. Open this file on GitHub on your work computer (bookmark the URL).
--    2. Click the "Copy raw contents" button (top-right of the file view).
--    3. Paste into a Snowsight worksheet and run the query you need.
--    4. Paste the result back to Claude (or just say what it showed).
--
--  Each block below is independent: run one at a time. All object names are
--  fully qualified (DBA_MAINT_DB.OVERWATCH.*), so no USE DATABASE/SCHEMA needed.
--
--  THIS BATCH: Ask-OVERWATCH smoke checks for all FOUR answerers (live on main,
--  v4.287.0) - confirm each mart the Ask page reads returns rows on this account.
-- ===========================================================================

-- ------------------------------------------------------------------------
-- Q1: Ask smoke 1/4 - per-user spend attribution (spend_spike_by_user)
-- LOOK FOR: Rows of DIMENSION (user) with ALLOC_CREDITS, biggest first. Non-empty = the 'which user is causing spend spikes' answer has data. Same builder the Cost & Contract page serves, so the numbers reconcile.
-- ------------------------------------------------------------------------
WITH cov AS (
    SELECT MIN(DAY) AS FIRST_DAY,
           COUNT(DISTINCT CASE
                   WHEN DAY >= DATEADD('day', -30, CURRENT_DATE())
                    AND DAY < CURRENT_DATE() THEN DAY END) AS WINDOW_DAYS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY x
    WHERE 1 = 1
),
scoped AS (
    SELECT x.USER_NAME AS KEY_NAME, x.DATABASE_NAME, x.EXEC_SEC, x.ALLOC_CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY x
    WHERE x.DAY >= DATEADD('day', -30, CURRENT_DATE()) AND x.DAY < CURRENT_DATE()
)
SELECT
    COALESCE(KEY_NAME, 'NONE') AS DIMENSION,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    SUM(ALLOC_CREDITS) / NULLIF((SELECT SUM(ALLOC_CREDITS) FROM scoped), 0) AS ELAPSED_SHARE,
    ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
FROM scoped
WHERE 1 = 1
  AND (SELECT FIRST_DAY FROM cov) <= DATEADD('day', -30, CURRENT_DATE())
  AND (SELECT WINDOW_DAYS FROM cov) >= 30
GROUP BY KEY_NAME
ORDER BY ALLOC_CREDITS DESC
LIMIT 100
;

-- RESULT Q1: (paste back to Claude / note what it showed)


-- ------------------------------------------------------------------------
-- Q2: Ask smoke 2/4 - top cloud-services query shapes (cloud_services_spike_by_query)
-- LOOK FOR: Rows of QUERY_TYPE / SAMPLE_TEXT / RUNS / CS_CREDITS, biggest first. Non-empty = the 'which query spikes cloud services' answer has data.
-- ------------------------------------------------------------------------
SELECT
    QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(QUERY_TYPE) AS QUERY_TYPE,
    ANY_VALUE(SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(RUNS) AS RUNS,
    ROUND(SUM(CS_CREDITS), 4) AS CS_CREDITS,
    ROUND(SUM(CS_CREDITS) / NULLIF(SUM(RUNS), 0) * 1000, 4) AS CS_PER_1K_RUNS,
    ROUND(SUM(EXEC_SEC_SUM) / NULLIF(SUM(RUNS), 0), 3) AS AVG_EXEC_S,
    ROUND(SUM(CACHE_PCT_SUM) / NULLIF(SUM(RUNS), 0) * 100, 0) AS AVG_CACHE_PCT
FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
GROUP BY QUERY_PARAMETERIZED_HASH
ORDER BY CS_CREDITS DESC
LIMIT 30
;

-- RESULT Q2: (paste back to Claude / note what it showed)


-- ------------------------------------------------------------------------
-- Q3: Ask smoke 3/4 - cloud-services credits by user
-- LOOK FOR: Rows of USER_NAME / CS_CREDITS. Names the heaviest cloud-services user.
-- ------------------------------------------------------------------------
SELECT
    USER_NAME,
    ANY_VALUE(ROLE_NAME) AS ROLE_NAME,
    SUM(RUNS) AS RUNS,
    ROUND(SUM(CS_CREDITS), 4) AS CS_CREDITS,
    ROUND(SUM(CS_CREDITS) / NULLIF(SUM(RUNS), 0) * 1000, 4) AS CS_PER_1K_RUNS
FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
GROUP BY USER_NAME
ORDER BY CS_CREDITS DESC
LIMIT 25
;

-- RESULT Q3: (paste back to Claude / note what it showed)


-- ------------------------------------------------------------------------
-- Q4: Ask smoke 4/6 - cloud-services ratio per warehouse
-- LOOK FOR: Rows of WAREHOUSE_NAME / CLOUD_SVC_PCT / STATUS (ELEVATED/WATCH/NORMAL). Flags which warehouses have an elevated cloud-services share.
-- ------------------------------------------------------------------------
SELECT
    WAREHOUSE_NAME,
    ROUND(SUM(CREDITS_COMPUTE), 2) AS COMPUTE_CREDITS,
    ROUND(SUM(CREDITS_TOTAL - CREDITS_COMPUTE), 2) AS CLOUD_SVC_CREDITS,
    ROUND(SUM(CREDITS_TOTAL), 2) AS TOTAL_CREDITS,
    ROUND(SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) * 100, 1) AS CLOUD_SVC_PCT,
    CASE
        WHEN SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) > 0.20 THEN 'ELEVATED'
        WHEN SUM(CREDITS_TOTAL - CREDITS_COMPUTE) / NULLIF(SUM(CREDITS_TOTAL), 0) > 0.10 THEN 'WATCH'
        ELSE 'NORMAL'
    END AS STATUS
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
WHERE DAY >= DATEADD('day', -30, CURRENT_DATE()) AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
GROUP BY 1
HAVING SUM(CREDITS_TOTAL) >= 0.5
ORDER BY CLOUD_SVC_PCT DESC
LIMIT 500
;

-- RESULT Q4: (paste back to Claude / note what it showed)


-- ------------------------------------------------------------------------
-- Q5: Ask smoke 5/6 - idle warehouse waste (warehouse_idle_waste)
-- LOOK FOR: Rows of WAREHOUSE_NAME / IDLE_HOURS / IDLE_CREDITS (biggest idle first). Non-empty = the 'which warehouse is wasting credits' answer has data.
-- ------------------------------------------------------------------------
WITH q_spans AS (
    SELECT WAREHOUSE_NAME,
           DATE_TRUNC('hour', START_TIME) AS H0,
           DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -30, CURRENT_DATE())
      AND WAREHOUSE_NAME IS NOT NULL
      AND 1 = 1
),
query_hours AS (
    SELECT DISTINCT s.WAREHOUSE_NAME, DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS
    FROM q_spans s
    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 25))) g
      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1
)
SELECT
    M.WAREHOUSE_NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(M.WAREHOUSE_NAME) AS COMPANY,
    COUNT(*) AS METERED_HOURS,
    SUM(IFF(Q.HOUR_TS IS NULL, 1, 0)) AS IDLE_HOURS,
    SUM(COALESCE(M.CREDITS_USED, 0)) AS TOTAL_CREDITS,
    SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
LEFT JOIN query_hours Q
       ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
      AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
WHERE M.START_TIME >= DATEADD('day', -30, CURRENT_DATE())
  AND M.WAREHOUSE_ID > 0
GROUP BY 1, 2
HAVING SUM(COALESCE(M.CREDITS_USED, 0)) > 0
ORDER BY IDLE_CREDITS DESC
LIMIT 100
;

-- RESULT Q5: (paste back to Claude / note what it showed)


-- ------------------------------------------------------------------------
-- Q6: Ask smoke 6/6 - task runs/failures per day (task_failures)
-- LOOK FOR: Rows of TASK_NAME / RUNS / FAILED (FAILED desc). Any FAILED>0 rows = the 'which task is failing most' answer has data; all FAILED=0 = good news.
-- ------------------------------------------------------------------------
SELECT DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR
FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
ORDER BY FAILED DESC, DAY DESC
;

-- RESULT Q6: (paste back to Claude / note what it showed)


