-- =====================================================================================
--  DIAG_CS_SELF_COST.sql  --  what is OVERWATCH itself costing in cloud services? (2026-09-26)
--  READ-ONLY. Run top to bottom and paste back every grid (an empty grid is an answer).
--  Why: the Cloud-services drivers panel on WH_ALFA_ADMIN shows OVERWATCH's own hourly statements
--  compiling for 3-19 s each (INSERT INTO ALERT_EVENTS 19 s x 168 runs/week, the hourly mart MERGEs).
--  Compile time is cloud-services (CS) work. Wave 2b (V156-V158) is ON HOLD until these numbers say
--  how much of the account's CS is OVERWATCH's, whether CS is actually billed (only the part above
--  10% of daily compute is), and exactly which statements to slim first.
--  Cost of this file: a few ACCOUNT_USAGE scans on an X-Small (seconds to ~1 min each).
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- (1) Account CS by day, 30 days: is CS billed (CS_BILLED_CR > 0), and when did it move?
--     CS_ADJ_CR is the free 10%-of-compute allowance (negative); CS_BILLED_CR = what was charged.
SELECT USAGE_DATE,
       ROUND(SUM(CREDITS_USED_COMPUTE), 2)                                        AS COMPUTE_CR,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 2)                                 AS CS_CR,
       ROUND(SUM(CREDITS_ADJUSTMENT_CLOUD_SERVICES), 2)                           AS CS_ADJ_CR,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES) + SUM(CREDITS_ADJUSTMENT_CLOUD_SERVICES), 2) AS CS_BILLED_CR,
       ROUND(100 * SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(SUM(CREDITS_USED_COMPUTE), 0), 1) AS CS_PCT_OF_COMPUTE
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -30, CURRENT_DATE())
GROUP BY 1 ORDER BY 1 DESC;

-- (2) OVERWATCH's OWN CS by day and source, 21 days (tasks run as SYSTEM; the app carries the SiS tag;
--     OTHER = worksheets / probes that touch DBA_MAINT_DB.OVERWATCH). Compare the days around each apply:
--     V148-V150 on 2026-09-24, V151-V155 on 2026-09-26 ~10:20.
SELECT TO_DATE(START_TIME) AS DAY,
       CASE WHEN CONTAINS(COALESCE(QUERY_TAG, ''), '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"') THEN 'APP (SiS)'
            WHEN USER_NAME = 'SYSTEM' THEN 'TASKS (SYSTEM)'
            ELSE 'OTHER (worksheets / probes)' END                AS SOURCE,
       COUNT(*)                                                   AS STATEMENTS,
       ROUND(SUM(COMPILATION_TIME) / 60000, 1)                    AS COMPILE_MIN,
       ROUND(SUM(EXECUTION_TIME) / 60000, 1)                      AS EXEC_MIN,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 3)                 AS CS_CR
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
  AND (CONTAINS(COALESCE(QUERY_TAG, ''), '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"')
       OR QUERY_TEXT ILIKE '%DBA_MAINT_DB.OVERWATCH.%')
GROUP BY 1, 2 ORDER BY 1 DESC, 2;

-- (3) OVERWATCH statement families, this week vs the week before (what grew, what is new).
--     RULE_HINT names the alert rule an ALERT_EVENTS insert belongs to; SAMPLE is the statement head.
SELECT QUERY_PARAMETERIZED_HASH                                                         AS FAMILY,
       ANY_VALUE(USER_NAME)                                                             AS USER_NAME,
       ANY_VALUE(WAREHOUSE_NAME)                                                        AS WH,
       COUNT_IF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()))                  AS RUNS_7D,
       ROUND(AVG(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()), COMPILATION_TIME, NULL)) / 1000, 1) AS AVG_COMPILE_S_7D,
       ROUND(SUM(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()), COMPILATION_TIME, 0)) / 60000, 1) AS COMPILE_MIN_7D,
       ROUND(SUM(IFF(START_TIME <  DATEADD('day', -7, CURRENT_TIMESTAMP()), COMPILATION_TIME, 0)) / 60000, 1) AS COMPILE_MIN_PRIOR_7D,
       ROUND(SUM(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()), CREDITS_USED_CLOUD_SERVICES, 0)), 3) AS CS_CR_7D,
       ROUND(SUM(IFF(START_TIME <  DATEADD('day', -7, CURRENT_TIMESTAMP()), CREDITS_USED_CLOUD_SERVICES, 0)), 3) AS CS_CR_PRIOR_7D,
       ANY_VALUE(REGEXP_SUBSTR(QUERY_TEXT, 'RULE_ID = ''([A-Z0-9_]+)''', 1, 1, 'e', 1))  AS RULE_HINT,
       ANY_VALUE(LEFT(REGEXP_REPLACE(QUERY_TEXT, '\\s+', ' '), 160))                     AS SAMPLE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
  AND QUERY_TEXT ILIKE '%DBA_MAINT_DB.OVERWATCH.%'
GROUP BY 1
ORDER BY CS_CR_7D DESC NULLS LAST, COMPILE_MIN_7D DESC
LIMIT 40;

-- (4) Which OVERWATCH tasks run how often (7 days) and for how long.
SELECT NAME, COUNT(*) AS RUNS_7D, COUNT_IF(STATE = 'SUCCEEDED') AS OK, COUNT_IF(STATE = 'FAILED') AS FAILED,
       ROUND(AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)), 1) AS AVG_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
  AND SCHEDULED_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY RUNS_7D DESC, AVG_SEC DESC;

ALTER SESSION UNSET TIMEZONE;
