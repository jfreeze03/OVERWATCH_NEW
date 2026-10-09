-- =====================================================================================
--  PROBES_COCO_FACT_GAP_2026-10-09.sql  --  READ-ONLY follow-up to PROBES_COCO_MODELS_2026-10-08.sql
--  WHY: that probe's C3 showed FACT_AI_USAGE_DAILY (what AI users, the showback, the CoCo spend tile and the runaway
--  alert read) holding 663.2889 Cortex Code credits for September against 683.7668 in Snowflake's own view: 20.4779
--  credits ($45.05) missing, all Snowsight (no Desktop exists; C2). October to yesterday matched exactly. The daily
--  loader re-reads only the last 3 days (TASK_LOAD_MARTS_V27_DAILY -> SP_LOAD_MARTS_V27('DAILY', 3)), so a run of 3+
--  days where its AI arm failed or the task did not run leaves those days missing for good. These three checks name
--  the day(s) and the cause. Nothing is created, changed or granted.
--  RUN AS SNOW_ACCOUNTADMINS, on this worksheet's warehouse (e.g. WH_ALFA_QUERY). Paste back all three grids.
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- F1 Per Central day, 2026-08-25 to yesterday: Snowflake's unified view (Snowsight + CLI), the two per-interface views
--    the loader actually reads, and FACT_AI_USAGE_DAILY, with the fact's last load time. STATUS = GAP where the fact
--    differs from the view by more than 0.001 credits; GAP rows sort first.
-- DECIDES: GAP days with FACT_CREDITS NULL or low and an old LAST_LOAD_TS = the loader missed those days (a reload of
--    that window repairs them). UNIFIED_VIEW_CREDITS <> PER_INTERFACE_VIEWS_CREDITS on a day = the two Snowflake views
--    disagree (then the loader's source is the cause, not a missed run).
WITH k AS (
    SELECT '2026-08-25'::DATE AS LO,
           CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT
),
u AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY,
           SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS CR,
           COUNT(*) AS REQUESTS
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN k
    WHERE c.USAGE_TIME >= DATEADD('day', -1, k.LO)::TIMESTAMP_TZ
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= k.LO
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.TODAY_CT
      AND LOWER(c.INTERFACE) IN ('snowsight', 'cli')
    GROUP BY 1
),
p AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', x.USAGE_TIME)::DATE AS DAY,
           SUM(COALESCE(x.TOKEN_CREDITS, 0)) AS CR
    FROM (
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
        UNION ALL
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
    ) x
    CROSS JOIN k
    WHERE x.USAGE_TIME >= DATEADD('day', -1, k.LO)::TIMESTAMP_TZ
      AND CONVERT_TIMEZONE('America/Chicago', x.USAGE_TIME)::DATE >= k.LO
      AND CONVERT_TIMEZONE('America/Chicago', x.USAGE_TIME)::DATE < k.TODAY_CT
    GROUP BY 1
),
f AS (
    SELECT t.DAY,
           SUM(COALESCE(t.CREDITS, 0)) AS CR,
           COUNT(*) AS FACT_ROWS,
           MAX(t.LOAD_TS) AS LAST_LOAD_TS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
    CROSS JOIN k
    WHERE t.SOURCE <> 'Functions'
      AND t.DAY >= k.LO
      AND t.DAY < k.TODAY_CT
    GROUP BY 1
),
d AS (
    SELECT COALESCE(u.DAY, p.DAY) AS DAY, u.CR AS U_CR, u.REQUESTS, p.CR AS P_CR
    FROM u
    FULL OUTER JOIN p ON p.DAY = u.DAY
)
SELECT COALESCE(d.DAY, f.DAY) AS DAY,
       IFF(ABS(COALESCE(d.U_CR, 0) - COALESCE(f.CR, 0)) > 0.001, 'GAP', 'ok') AS STATUS,
       d.REQUESTS,
       ROUND(d.U_CR, 4) AS UNIFIED_VIEW_CREDITS,
       ROUND(d.P_CR, 4) AS PER_INTERFACE_VIEWS_CREDITS,
       ROUND(f.CR, 4) AS FACT_CREDITS,
       ROUND(COALESCE(d.U_CR, 0) - COALESCE(f.CR, 0), 4) AS MISSING_FROM_FACT,
       f.FACT_ROWS,
       f.LAST_LOAD_TS
FROM d
FULL OUTER JOIN f ON f.DAY = d.DAY
ORDER BY STATUS, DAY;

-- F2 Did the daily loader task run each day? One row per Central day it was scheduled, 2026-08-25 on.
-- DECIDES: a GAP day from F1 with no row here, or only FAILED / SKIPPED / CANCELLED runs on it and the 2 days after,
--    = the task did not run (the 3-day window could not catch up). A SUCCEEDED run whose RETURN_VALUE lacks 'ai_code'
--    = the task ran but its AI arm failed (F3 has the error).
SELECT CONVERT_TIMEZONE('America/Chicago', h.SCHEDULED_TIME)::DATE AS RUN_DAY,
       LISTAGG(DISTINCT h.STATE, ', ') WITHIN GROUP (ORDER BY h.STATE) AS STATES,
       COUNT(*) AS RUNS,
       MAX(LEFT(h.RETURN_VALUE, 300)) AS RETURN_VALUE,
       MAX(LEFT(h.ERROR_MESSAGE, 300)) AS ERROR_MESSAGE
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
WHERE h.DATABASE_NAME = 'DBA_MAINT_DB'
  AND h.SCHEMA_NAME = 'OVERWATCH'
  AND h.NAME = 'TASK_LOAD_MARTS_V27_DAILY'
  AND h.SCHEDULED_TIME >= '2026-08-25 00:00:00 -05:00'::TIMESTAMP_TZ
GROUP BY 1
ORDER BY 1;

-- F3 The AI arm's own failures, 2026-08-25 on (APP_ERROR_LOG, written by the loader's per-arm EXCEPTION block).
-- DECIDES: rows on or just before a GAP day = the arm failed then; the message says why. 'no rows' = the arm never
--    failed, so a GAP comes from the task not running (F2) or from the views (F1).
SELECT LOGGED_AT::DATE AS DAY_LOGGED,
       ERROR_TYPE,
       LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE,
       LEFT(CONTEXT, 120) AS CONTEXT,
       COUNT(*) AS N,
       MIN(LOGGED_AT) AS FIRST_AT,
       MAX(LOGGED_AT) AS LAST_AT
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE LOGGED_AT >= '2026-08-25'::TIMESTAMP_NTZ
  AND CONTEXT LIKE 'FACT_AI_USAGE_DAILY%'
GROUP BY 1, 2, 3, 4
ORDER BY 1;

ALTER SESSION UNSET TIMEZONE;

-- ---------------------------------------------------------------------------------------------------------------------
--  REPAIR (NOT part of this probe; a WRITE). Only after F1 shows GAP days and you decide to fill them: as
--  SNOW_ACCOUNTADMINS, off-hours, re-merge the daily marts (FACT_AI_USAGE_DAILY included) for the last 50 days. It is
--  idempotent (MERGE): rows that are right stay right, missing days are filled. Then re-run F1: every day should read ok.
--  CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 50);
-- ---------------------------------------------------------------------------------------------------------------------
