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
--  THIS BATCH: Ask-OVERWATCH smoke checks - confirm the marts the new Ask page
--  reads return rows on the live account (feature branch: feature/ask-overwatch).
-- ===========================================================================

-- ------------------------------------------------------------------------
-- Q1: Ask smoke 1/4 - per-user spend attribution (spend_spike_by_user)
-- LOOK FOR: Rows of DIMENSION (user) with ALLOC_CREDITS, biggest first. Non-empty = the 'which user is causing spend spikes' answer has data.
-- ------------------------------------------------------------------------
WITH scoped AS (
    SELECT KEY_NAME, EXEC_SEC, ALLOC_CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
    WHERE DAY >= DATEADD('day', -30, CURRENT_DATE()) AND DIMENSION = 'USER'
)
SELECT
    COALESCE(KEY_NAME, 'NONE') AS DIMENSION,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    SUM(ALLOC_CREDITS) / NULLIF((SELECT SUM(ALLOC_CREDITS) FROM scoped), 0) AS ELAPSED_SHARE,
    ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
FROM scoped
WHERE 1 = 1
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
-- Q4: Ask smoke 4/4 - cloud-services ratio per warehouse
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


