-- =====================================================================================
--  DIAG_7B_TAGS.sql  --  why do the app's statements carry no QUERY_TAG after the deploy? (READ-ONLY)
--
--  Run top to bottom in ONE worksheet session (the SET below feeds the later queries). Paste back
--  all five grids, plus the version the app itself shows (status bar / Admin).
--  Three possible causes, and the grid that tells them apart:
--    A. the deployed build is not v4.590.0 (e.g. the checkout was not pulled before deploy) -> (4) rows
--       show QUERY_TAG '(empty)' AND the app shows an older version;
--    B. Streamlit-in-Snowflake drops / replaces per-statement tags -> the app shows v4.590.0, (4) shows
--       '(empty)' or a tag SiS set itself, and (3) is EMPTY;
--    C. the app never detected SiS, so it never sent the tag -> (3) shows a FAILED
--       'ALTER SESSION SET QUERY_TAG ...' from the app after the deploy.
--  Separate from RUN_NEXT.sql.
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- (1) When was the app last deployed? (`snow streamlit deploy --replace` recreates it: created_on = deploy time)
SHOW STREAMLITS LIKE 'OVERWATCH_APP' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SET DEPLOYED_AT = (SELECT MAX("created_on") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
SELECT $DEPLOYED_AT AS DEPLOYED_AT;

-- (2) Every QUERY_TAG value on statements in the app's own sessions since that deploy (the sessions that
--     ran EXECUTE STREAMLIT ... OVERWATCH_APP). 'OVERWATCH|page=...' = tagging works; '(empty)' = not sent
--     or dropped; anything else = a tag Streamlit-in-Snowflake sets itself.
WITH app_sessions AS (
    SELECT DISTINCT SESSION_ID
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE START_TIME >= $DEPLOYED_AT
       AND STARTSWITH(UPPER(QUERY_TEXT), 'EXECUTE STREAMLIT')
       AND CONTAINS(UPPER(QUERY_TEXT), 'OVERWATCH_APP')
)
SELECT LEFT(COALESCE(NULLIF(q.QUERY_TAG, ''), '(empty)'), 140) AS QUERY_TAG_SEEN,
       q.QUERY_TYPE,
       COUNT(*)                                                 AS N,
       MIN(q.START_TIME)                                        AS FIRST_AT,
       MAX(q.START_TIME)                                        AS LAST_AT,
       ANY_VALUE(LEFT(q.QUERY_TEXT, 90))                        AS EXAMPLE
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
  JOIN app_sessions s ON s.SESSION_ID = q.SESSION_ID
 WHERE q.START_TIME >= $DEPLOYED_AT
 GROUP BY 1, 2
 ORDER BY 3 DESC
 LIMIT 30;

-- (3) Did the app try (and fail) the old ALTER SESSION tag after the deploy? Any row = cause C.
SELECT START_TIME, USER_NAME, EXECUTION_STATUS, LEFT(ERROR_MESSAGE, 120) AS ERR,
       COALESCE(WAREHOUSE_NAME, '(cloud services)') AS WAREHOUSE, LEFT(QUERY_TEXT, 110) AS QUERY_HEAD
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE START_TIME >= $DEPLOYED_AT
   AND QUERY_TEXT ILIKE 'ALTER SESSION SET QUERY_TAG%'
 ORDER BY START_TIME DESC
 LIMIT 20;

-- (4) The app's own role probe (this exact text is sent only by the app, once per viewer session).
--     v4.590.0 sends it tagged 'OVERWATCH|page=session|tier=metadata'.
SELECT START_TIME, USER_NAME,
       LEFT(COALESCE(NULLIF(QUERY_TAG, ''), '(empty)'), 140) AS QUERY_TAG_SEEN,
       EXECUTION_STATUS, COALESCE(WAREHOUSE_NAME, '(cloud services)') AS WAREHOUSE, SESSION_ID
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE START_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP())
   AND QUERY_TEXT = 'SELECT CURRENT_ROLE() AS R, CURRENT_USER() AS U'
 ORDER BY START_TIME DESC
 LIMIT 20;
