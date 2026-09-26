-- =====================================================================================
--  DIAG_CS_ACCOUNT_DRIVERS.sql  --  who is spending the account's cloud-services credits? (2026-09-26)
--  READ-ONLY. Run top to bottom and paste back every grid.
--  Why: DIAG_CS_SELF_COST showed the account bills 11-22 CS credits EVERY day (it is always above the
--  free 10%-of-compute allowance), with spikes on 2026-09-16 (64.05 CS / 48.71 billed) and 2026-09-09
--  (43.83 / 27.60). OVERWATCH's own statements are ~0.5-0.6 CS credits/day (~2% of it) and flat for 3
--  weeks, so the driver is elsewhere. This finds it: by service type, warehouse, user + client
--  application, and the exact statement families -- with the two spike days broken out.
--  Days here are UTC days, to line up with METERING_DAILY_HISTORY.USAGE_DATE.
--  Cost: METERING / WAREHOUSE_METERING are small; grids 3-5 scan 7 days of QUERY_HISTORY and grid 4
--  scans 3 single days (expect up to a minute or two each on an X-Small).
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- (1) CS by service type (warehouses vs serverless / other services), 30 days, last 7 days, spike days.
SELECT SERVICE_TYPE,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 2)                                                          AS CS_30D,
       ROUND(SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED_CLOUD_SERVICES, 0)), 2)  AS CS_7D,
       ROUND(SUM(IFF(USAGE_DATE IN ('2026-09-09'::DATE, '2026-09-16'::DATE), CREDITS_USED_CLOUD_SERVICES, 0)), 2) AS CS_SPIKE_DAYS,
       ROUND(SUM(CREDITS_USED_COMPUTE), 2)                                                                 AS COMPUTE_30D
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -30, CURRENT_DATE())
GROUP BY 1 ORDER BY CS_30D DESC;

-- (2) The top 5 warehouses by CS on each day, 21 days (spots which warehouse made 09-09 and 09-16).
SELECT TO_DATE(CONVERT_TIMEZONE('UTC', START_TIME)) AS UTC_DAY, WAREHOUSE_NAME,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 2) AS CS_CR,
       ROUND(SUM(CREDITS_USED_COMPUTE), 2)        AS COMPUTE_CR
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
GROUP BY 1, 2
QUALIFY ROW_NUMBER() OVER (PARTITION BY UTC_DAY ORDER BY SUM(CREDITS_USED_CLOUD_SERVICES) DESC) <= 5
ORDER BY 1 DESC, 3 DESC;

-- (3) Last 7 days: CS by warehouse x user x client application (the same application identifier the
--     app's Operations chatter panel uses). PCT_OF_QUERY_CS = share of all query-attributed CS.
WITH q AS (
    SELECT q.SESSION_ID, COALESCE(q.WAREHOUSE_NAME, '(no warehouse)') AS WH,
           COALESCE(q.USER_NAME, 'UNKNOWN') AS USER_NAME,
           q.COMPILATION_TIME, COALESCE(q.CREDITS_USED_CLOUD_SERVICES, 0) AS CS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE q.START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
),
sess AS (
    SELECT SESSION_ID,
           COALESCE(NULLIF(GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')::STRING, ''),
                    NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                    '(unknown)') AS APPLICATION
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE CREATED_ON >= DATEADD('day', -14, CURRENT_TIMESTAMP())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
)
SELECT q.WH, q.USER_NAME, COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
       COUNT(*)                                           AS STATEMENTS,
       ROUND(SUM(q.COMPILATION_TIME) / 60000, 1)          AS COMPILE_MIN,
       ROUND(SUM(q.CS), 2)                                AS CS_CR_7D,
       ROUND(100 * RATIO_TO_REPORT(SUM(q.CS)) OVER (), 1) AS PCT_OF_QUERY_CS
FROM q LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
GROUP BY 1, 2, 3
ORDER BY CS_CR_7D DESC
LIMIT 25;

-- (4) The two spike days against a normal weekday: top 12 warehouse x user x statement-type groups each.
SELECT TO_DATE(CONVERT_TIMEZONE('UTC', START_TIME))       AS UTC_DAY,
       COALESCE(WAREHOUSE_NAME, '(no warehouse)')         AS WH,
       COALESCE(USER_NAME, 'UNKNOWN')                     AS USER_NAME,
       QUERY_TYPE,
       COUNT(*)                                           AS STATEMENTS,
       ROUND(SUM(COMPILATION_TIME) / 60000, 1)            AS COMPILE_MIN,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 2)         AS CS_CR
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE (START_TIME >= '2026-09-09 00:00:00 +00:00'::TIMESTAMP_TZ AND START_TIME < '2026-09-10 00:00:00 +00:00'::TIMESTAMP_TZ)
   OR (START_TIME >= '2026-09-16 00:00:00 +00:00'::TIMESTAMP_TZ AND START_TIME < '2026-09-17 00:00:00 +00:00'::TIMESTAMP_TZ)
   OR (START_TIME >= '2026-09-23 00:00:00 +00:00'::TIMESTAMP_TZ AND START_TIME < '2026-09-24 00:00:00 +00:00'::TIMESTAMP_TZ)
GROUP BY 1, 2, 3, 4
QUALIFY ROW_NUMBER() OVER (PARTITION BY UTC_DAY ORDER BY SUM(CREDITS_USED_CLOUD_SERVICES) DESC) <= 12
ORDER BY 1 DESC, CS_CR DESC;

-- (5) The 25 statement families that spent the most CS in the last 7 days, account-wide.
SELECT QUERY_PARAMETERIZED_HASH                                   AS FAMILY,
       ANY_VALUE(COALESCE(WAREHOUSE_NAME, '(no warehouse)'))      AS WH,
       ANY_VALUE(USER_NAME)                                       AS USER_NAME,
       ANY_VALUE(QUERY_TYPE)                                      AS QUERY_TYPE,
       COUNT(*)                                                   AS RUNS_7D,
       ROUND(AVG(COMPILATION_TIME) / 1000, 2)                     AS AVG_COMPILE_S,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 3)                 AS CS_CR_7D,
       ANY_VALUE(LEFT(REGEXP_REPLACE(QUERY_TEXT, '\\s+', ' '), 120)) AS SAMPLE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
GROUP BY 1
ORDER BY CS_CR_7D DESC
LIMIT 25;
