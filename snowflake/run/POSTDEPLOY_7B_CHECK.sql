-- =====================================================================================
--  POSTDEPLOY_7B_CHECK.sql  --  Next-Fifty #7 Slice B post-deploy confirmation (READ-ONLY)
--
--  RUN ONLY AFTER v4.590.0 is deployed (`snow streamlit deploy --replace`), someone has used the
--  app for a few minutes, and ~1 hour has passed (ACCOUNT_USAGE.QUERY_HISTORY lags up to 45 min).
--  Before the deploy every query here returns NO ROWS -- that is expected, not a failure.
--  Separate from RUN_NEXT.sql (the pending V148 -> V149 -> V150 there are untouched).
--  Paste back all four grids.
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- (1) DECISIVE: do the app's production statements carry the per-statement tag?
--     Expect rows for every hour someone had the app open. NO rows after using the app = the SiS
--     runtime dropped statement_params (the owner's-rights proc probe was only a proxy) -> report it.
SELECT DATE_TRUNC('hour', START_TIME)                               AS HR,
       COALESCE(WAREHOUSE_NAME, '(cloud services)')                 AS WAREHOUSE,
       COUNT(*)                                                     AS APP_STATEMENTS,
       COUNT(DISTINCT USER_NAME)                                    AS VIEWERS,
       COUNT(DISTINCT REGEXP_SUBSTR(QUERY_TAG, 'page=([^|]+)', 1, 1, 'e')) AS PAGES
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE QUERY_TAG LIKE 'OVERWATCH|%'
   AND START_TIME >= DATEADD('day', -1, CURRENT_TIMESTAMP())
 GROUP BY 1, 2
 ORDER BY 1 DESC, 2;

-- (2) EVIDENCE for turning on more per-statement timeouts (today only Cortex has one). Per tier: how
--     long the app's statements really take, and how many WOULD have been cancelled by the tier's
--     ceiling (live/metadata 30s, recent/hourly 120s, historical 180s). WOULD_BE_CANCELLED = 0 over a
--     representative week means that tier's timeout is safe to enable.
WITH t AS (
    -- the Ask page's Cortex narration runs as a LIVE-tier read; split it out so it can't mask the live tier
    SELECT REGEXP_SUBSTR(QUERY_TAG, 'tier=([^|]+)', 1, 1, 'e')
           || IFF(QUERY_TEXT ILIKE '%SNOWFLAKE.CORTEX.%' AND QUERY_TAG NOT LIKE '%tier=cortex%', ' (cortex call)', '') AS TIER,
           TOTAL_ELAPSED_TIME / 1000.0                         AS ELAPSED_SEC,
           EXECUTION_STATUS
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE QUERY_TAG LIKE 'OVERWATCH|%'
       AND START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
)
SELECT TIER,
       COUNT(*)                                            AS STATEMENTS,
       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS')             AS FAILED,
       ROUND(MEDIAN(ELAPSED_SEC), 2)                       AS P50_SEC,
       ROUND(APPROX_PERCENTILE(ELAPSED_SEC, 0.95), 2)      AS P95_SEC,
       ROUND(APPROX_PERCENTILE(ELAPSED_SEC, 0.99), 2)      AS P99_SEC,
       ROUND(MAX(ELAPSED_SEC), 2)                          AS MAX_SEC,
       CASE TIER WHEN 'live' THEN 30 WHEN 'metadata' THEN 30 WHEN 'recent' THEN 120
                 WHEN 'hourly' THEN 120 WHEN 'historical' THEN 180 WHEN 'cortex' THEN 90 END AS CEILING_SEC,
       COUNT_IF(ELAPSED_SEC > CASE TIER WHEN 'live' THEN 30 WHEN 'metadata' THEN 30 WHEN 'recent' THEN 120
                                       WHEN 'hourly' THEN 120 WHEN 'historical' THEN 180 WHEN 'cortex' THEN 90
                                  END)                     AS WOULD_BE_CANCELLED
  FROM t
 GROUP BY 1
 ORDER BY 2 DESC;

-- (3) The slowest tagged statements this week (which page / tier, for any WOULD_BE_CANCELLED above).
SELECT START_TIME, QUERY_TAG, ROUND(TOTAL_ELAPSED_TIME / 1000.0, 1) AS ELAPSED_SEC, EXECUTION_STATUS,
       COALESCE(WAREHOUSE_NAME, '(cloud services)') AS WAREHOUSE, LEFT(QUERY_TEXT, 120) AS QUERY_HEAD
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE QUERY_TAG LIKE 'OVERWATCH|%'
   AND START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
 ORDER BY TOTAL_ELAPSED_TIME DESC
 LIMIT 15;

-- (4) The app statements NO client-side tag can reach, beside the tagged ones on the app warehouse:
--     the Streamlit runtime's session statement and the connector's untagged async result fetch
--     (select * from table(result_scan('<qid>')) after every async read). Admin > App self-cost books
--     them as APP RUNTIME (SiS) / APP RESULT FETCH (untagged). FETCH ~= TAGGED on cache-miss reads is
--     expected; FETCH = 0 means the SiS runtime fetches results another way (report it either way).
SELECT DATE_TRUNC('hour', START_TIME) AS HR,
       COUNT_IF(QUERY_TAG LIKE 'OVERWATCH|%')                                               AS TAGGED,
       COUNT_IF(STARTSWITH(UPPER(COALESCE(QUERY_TEXT, '')), 'EXECUTE STREAMLIT')
                AND CONTAINS(UPPER(QUERY_TEXT), 'OVERWATCH_APP'))                            AS RUNTIME,
       COUNT_IF(STARTSWITH(LOWER(COALESCE(QUERY_TEXT, '')), 'select * from table(result_scan(''')
                AND COALESCE(QUERY_TAG, '') = '')                                            AS UNTAGGED_FETCH
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE WAREHOUSE_NAME = 'WH_ALFA_ADMIN'
   AND START_TIME >= DATEADD('day', -1, CURRENT_TIMESTAMP())
 GROUP BY 1
 ORDER BY 1 DESC;
