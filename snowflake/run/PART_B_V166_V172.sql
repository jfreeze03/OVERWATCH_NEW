-- =====================================================================
--  OVERWATCH -- PART_B_V166_V172.sql  (READ-ONLY verify for stage 2 of RUN_NEXT.sql: V166 -> V172)
--  Changes nothing: SELECT only after the session pin, which comes FIRST (correction 5): the V167 - V172 grids
--  compare Central wall-clock NTZ stamps (RAISED_AT, LOGGED_AT, APPLIED_AT, DETECTED_AT) with CURRENT_TIMESTAMP()
--  / CURRENT_DATE(); a UTC worksheet reads them 5-6 h off. In a new worksheet run the two lines below first.
--  Each section is the cluster generator's PART B verbatim (outputs/gen_v166.py .. gen_v172.py); the last
--  section (ALL.1 / ALL.2) is added for the package. Paste every grid back; an empty grid is an answer.
--
--  WHEN TO RUN WHAT
--    NOW, right after stage 2:  V166.1, V166.2 | V167.1, V167.2, V167.3 | V168.1, V168.2 | V169.1, V169.2 |
--                               V170.1 - V170.5 | V171.1 - V171.4 | V172.1, V172.2 | ALL.1
--    After OWNER_REPAIRS PART 1 (the V167 AI re-key):  V167.4 (DRIFT ~0 per Central day)
--    After the next :07 Central hourly scan:  V168.3, V168.4
--    The next morning:  V166.3, V166.4 (after the 06:30 / 06:55 CT loaders) | V169.3, V169.4 (after the ~07:00 CT
--                       daily scan) | V171.5 (after the 07:20 CT digest), V171.6 (after the daily scan) |
--                       V172.3 (after the 06:40 / 06:50 CT change scans), V172.4 (after the 07:00 CT sweep)
--    After the first manual declare from the 4.609.0 app made 30 s or more after the apply (Control Room's SQL
--    preview ends with a 5th argument, the viewer):  V170.6
--    The next day (FACT_SECURITY_CHANGE loads from the hourly extract):  ALL.2 (labels the expected temp-table row
--    of OWNER_REPAIRS PART 1.1)
--  V170.4 / V170.5 are owner worklists (close member-less / duplicate incidents in Control Room); V167.4 drift is
--  expected between the app deploy and OWNER_REPAIRS PART 1 (R2-052).
-- =====================================================================

ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- FIRST: session only; the stored NTZ clocks are Central
USE ROLE SNOW_ACCOUNTADMINS;

-- #####################################################################
--  V166 PART B  (from outputs/gen_v166.py, cluster loaders; verifies snowflake/migrations/V166__fact_loader_window_integrity.sql)
-- #####################################################################
-- PART B -- V166 verify (READ-ONLY). Run after applying V166 (a Central session; the integrator's combined
-- PART B opens with ALTER SESSION SET TIMEZONE). Every CHECK should read OK; paste the grids back.
-- V166.1 the four procs are the V166 text.
SELECT 'V166.1 SP_LOAD_SECURITY_FACTS is the V166 proc' AS CHECK_NAME,
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(FLOAT)'), 'lo_ts TIMESTAMP_LTZ;') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(FLOAT)'), 'WHERE EVENT_TS >= :lo_ts;') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(FLOAT)'), 'WHERE START_TIME >= :lo_ts') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(FLOAT)'), 'WHERE EVENT_TS >= (SELECT MIN(START_TIME)') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(FLOAT)'), 'GREATEST_IGNORE_NULLS'),
           'OK', 'FAIL: not the V166 text') AS RESULT
UNION ALL
SELECT 'V166.1 SP_LOAD_DAILY_FACTS is the V166 proc',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()'), 'SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0))') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()'), 'SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()'), 'WITH task_attempts AS (') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()'), 'AVG(COALESCE(AVERAGE_DATABASE_BYTES') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()'), 'AVG(COALESCE(AVERAGE_FAILSAFE_BYTES'),
           'OK', 'FAIL: not the V166 text')
UNION ALL
SELECT 'V166.1 SP_LOAD_APP_COST is the V166 proc',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(FLOAT)'), 'BEGIN TRANSACTION;') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(FLOAT)'), 'fact_load_failed') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(FLOAT)'), 'AppCost') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(FLOAT)'), '-30, :lo)') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(FLOAT)'), '-7, :lo)'),
           'OK', 'FAIL: not the V166 text')
UNION ALL
SELECT 'V166.1 SP_LOAD_STORAGE_TRUTH is the V166 proc',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(FLOAT)'), 'BEGIN TRANSACTION;') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(FLOAT)'), 'fact_load_failed') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(FLOAT)'), 'StorageTruth'),
           'OK', 'FAIL: not the V166 text');

-- V166.2 the R2-009 repair landed: every multi-ID name-day older than 2 days now holds the SUM (the MERGE's own
--        source SELECT and ON clause). expect OK; MISMATCHED > 0 means the MERGE did not run (re-run the file).
WITH s AS (
    SELECT USAGE_DATE AS DAY, DATABASE_NAME,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
)
SELECT 'V166.2 FACT_STORAGE_DAILY multi-ID name-days hold the SUM' AS CHECK_NAME,
       COUNT(*) AS MULTI_ID_NAME_DAYS,
       COUNT_IF(ABS(t.DB_BYTES - s.DB_BYTES) > 1 OR ABS(t.FAILSAFE_BYTES - s.FAILSAFE_BYTES) > 1) AS MISMATCHED,
       IFF(COUNT_IF(ABS(t.DB_BYTES - s.DB_BYTES) > 1 OR ABS(t.FAILSAFE_BYTES - s.FAILSAFE_BYTES) > 1) = 0,
           'OK', 'FAIL: AVG rows remain') AS RESULT
  FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
  JOIN s ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME
 WHERE s.DAY < DATEADD('day', -2, CURRENT_DATE());

-- V166.3 the next morning (after the 06:30 / 06:55 CT runs; ACCOUNT_USAGE is up to ~45 min behind). expect
--        SUCCEEDED for both tasks, and note TASK_LOAD_APP_COST's duration (the SESSIONS scan grew from 10 to 33
--        days). RETURN_VALUE stays NULL: a task that CALLs a proc does not publish its return string.
SELECT NAME, SCHEDULED_TIME, STATE, DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SEC,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
   AND NAME IN ('TASK_LOAD_APP_COST', 'TASK_LOAD_STORAGE_TRUTH')
   AND SCHEDULED_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
 ORDER BY NAME, SCHEDULED_TIME DESC;

-- V166.4 any loader failure V166 now logs (expect no rows). A row means a run rolled back to its previous
--        fill and the task read FAILED; the self-watch ERR leg raises OPS_PIPELINE_DEGRADED for it.
SELECT LOGGED_AT, PAGE, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE PAGE IN ('AppCost', 'StorageTruth') AND ERROR_TYPE = 'fact_load_failed'
   AND LOGGED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
 ORDER BY LOGGED_AT DESC;

-- #####################################################################
--  V167 PART B  (from outputs/gen_v167.py, cluster marts; verifies snowflake/migrations/V167__mart_loader_edges_ai_coverage_pattern_reload.sql)
-- #####################################################################
-- PART B -- V167 (READ-ONLY). After the apply; changes nothing.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- V167.1 the watermark column exists and what each loader has stamped so far (NULL until its next run)
SELECT SOURCE_NAME, COVERAGE_FROM, STATUS, SNAPSHOT_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('FACT_AI_USAGE_DAILY', 'MART_PATTERN_COST_DAILY')
ORDER BY SOURCE_NAME;

-- V167.2 the deployed bodies carry the V167 deltas (expect every CHECK_n TRUE)
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'COALESCE(END_TIME, START_TIME) >= DATEADD') AS CHECK_1,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'CONVERT_TIMEZONE(''America/Chicago'', c.USAGE_TIME)::DATE AS DAY') AS CHECK_2,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)') AS CHECK_3,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE()'), 'AND LOAD_TS < :recon_start') AS CHECK_4,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(FLOAT)'), 'BEGIN TRANSACTION') AS CHECK_5,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(FLOAT)'), 'AS COVERAGE_FROM') AS CHECK_6;

-- V167.3 R2-010: stale-company twins left in the pattern mart (expect 0 after the apply)
SELECT COUNT(*) AS TWIN_ROWS_LEFT
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
JOIN (
    SELECT DAY, QUERY_HASH, DATABASE_NAME, MAX(LOAD_TS) AS NEWEST_TS
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1
) g
  ON t.DAY = g.DAY AND t.QUERY_HASH = g.QUERY_HASH AND t.DATABASE_NAME = g.DATABASE_NAME
  AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS);

-- V167.4 R2-052: after OWNER_REPAIRS step 1, Cortex Code fact days vs the live views per Central day, D-31..D-4
SELECT COALESCE(f.DAY, l.DAY) AS DAY, f.CREDITS AS FACT_CREDITS, l.CREDITS AS LIVE_CREDITS,
       ROUND(COALESCE(f.CREDITS, 0) - COALESCE(l.CREDITS, 0), 4) AS DRIFT
FROM (
    SELECT DAY, SUM(CREDITS) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE IN ('Snowsight', 'CLI')
      AND DAY >= DATEADD('day', -31, CURRENT_DATE()) AND DAY < DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1
) f
FULL OUTER JOIN (
    SELECT CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE AS DAY, SUM(TOKEN_CREDITS) AS CREDITS
    FROM (
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
        UNION ALL
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
    ) c
    WHERE CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE >= DATEADD('day', -31, CURRENT_DATE())
      AND CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE < DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1
) l ON l.DAY = f.DAY
ORDER BY 1;

-- #####################################################################
--  V168 PART B  (from outputs/gen_v168.py, cluster alerts; verifies snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql)
-- #####################################################################
-- PART B -- V168 verify (READ-ONLY; run in a Central session: V168.4 compares RAISED_AT, Central wall-clock, with
-- CURRENT_TIMESTAMP()). V168.1 + V168.2 right after the apply; V168.3 + V168.4 after the next :07 Central hourly
-- scan. Every RESULT should read OK (or the count named); paste the grids back.
SELECT 'V168.1 SCHEMA_VERSION has 168' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168) = 1,
           'OK', 'FAIL: V168 did not finish') AS RESULT
UNION ALL
SELECT 'V168.1 PIPE_COPY_FAILURES NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = 'Failed COPY / Snowpipe file loads per Central failure day (threshold = allowed failures)', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'PIPE_COPY_FAILURES')
UNION ALL
SELECT 'V168.1 SEC_NEW_ADMIN_NETWORK NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = 'Admin login attempt from a network unseen in 90 days (the title says whether any succeeded)', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: alert scan v14 (V168:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v14 (V168:'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: AS FAIL_DAY', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'AS FAIL_DAY'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: GROUP BY 1, 2, 3, 4', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'GROUP BY 1, 2, 3, 4'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: AS SUCCESSES', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'AS SUCCESSES'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: (0 successful)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '(0 successful)'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: LEFT(nn.USER_NAME, 200)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'LEFT(nn.USER_NAME, 200)'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: Changes (Recent grant changes)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'Changes (Recent grant changes)'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: ____-__-__', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '____-__-__'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: /14 rule blocks ok', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '/14 rule blocks ok'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: SP_SCAN_ETL_CYCLE', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SP_SCAN_ETL_CYCLE'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL has: AS RERAISED', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'AS RERAISED'), 'OK', 'FAIL: not the V168 body')
UNION ALL
SELECT 'V168.2 hourly scan DDL lacks: failed file load(s) (24h)', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'failed file load(s) (24h)'), 'OK', 'FAIL: still V162 text')
UNION ALL
SELECT 'V168.2 hourly scan DDL lacks: ev.RAISED_AT >= DATEADD', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'ev.RAISED_AT >= DATEADD'), 'OK', 'FAIL: still V162 text')
UNION ALL
SELECT 'V168.2 hourly scan DDL lacks: budget_usd', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'budget_usd'), 'OK', 'FAIL: still V162 text')
UNION ALL
SELECT 'V168.2 hourly scan DDL lacks: alert scan v13 (V162:', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v13 (V162:'), 'OK', 'FAIL: still V162 text');

-- V168.3 after the next hourly scan: the heartbeat reads 14/14 and no rule block failed.
SELECT 'V168.3 hourly scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row') AS RESULT
UNION ALL
SELECT 'V168.3 no rule_block_failed / sweep failure (3h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan'
               AND ERROR_TYPE IN ('rule_block_failed', 'supersede_sweep_failed', 'autoclear_sweep_failed')
               AND LOGGED_AT >= DATEADD('hour', -3, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE');

-- V168.4 after the next hourly scan: the R2-034 census (PREFLIGHT P168.1) has nothing left to clear.
WITH cfg AS (
                   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                   WHERE ENABLED AND AUTO_CLEAR_ENABLED
),
firing AS (
               SELECT c.RULE_ID || '|' || q.COMPANY AS DEDUPE_KEY
               FROM cfg c
               JOIN (
                   SELECT COMPANY,
                          IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   GROUP BY COMPANY
                   HAVING SUM(QUERY_COUNT) >= 20
               ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT'
                  AND q.FAIL_PCT >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES'
                  AND q.QUEUED_MIN >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_SPILL_GB'
                  AND q.SPILL_GB >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
)
SELECT 'V168.4 stranded OPEN PERF events left to clear' AS CHECK_NAME,
       IFF(COUNT_IF(f.DEDUPE_KEY IS NULL) = 0, 'OK', 'CHECK: the sweep has not run or failed') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
LEFT JOIN (SELECT DISTINCT DEDUPE_KEY FROM firing) f
       ON f.DEDUPE_KEY = ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)
WHERE ev.STATUS = 'OPEN'
  AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')
  AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP());

-- #####################################################################
--  V169 PART B  (from outputs/gen_v169.py, cluster alerts; verifies snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql)
-- #####################################################################
-- PART B -- V169 verify (READ-ONLY; run in a Central session). Run V169.1 + V169.2 right after the apply and
-- the grids V169.3 + V169.4 the next morning, after the ~07:00 Central daily scan. Every RESULT should read OK;
-- paste the grids back.
SELECT 'V169.1 SCHEMA_VERSION has 169' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169) = 1,
           'OK', 'FAIL: V169 did not finish') AS RESULT
UNION ALL
SELECT 'V169.1 COST_EGRESS_SPIKE NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = 'True egress (cross-region or cross-cloud) above threshold GB on the previous complete Central day', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_EGRESS_SPIKE')
UNION ALL
SELECT 'V169.2 daily scan DDL has: alert scan daily v6 (V169:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'alert scan daily v6 (V169:'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS MTD_COMPLETE_USD', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS MTD_COMPLETE_USD'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS TERM_END', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS TERM_END'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: PARTITION BY DATABASE_ID', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'PARTITION BY DATABASE_ID'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AND DELETED IS NULL', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AND DELETED IS NULL'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS NEWEST_LOAD', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS NEWEST_LOAD'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: TARGET_CLOUD IS NOT NULL', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'TARGET_CLOUD IS NOT NULL'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS GB_DAY', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS GB_DAY'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: /14 rule blocks ok (daily)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), '/14 rule blocks ok (daily)'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS DAILY_BURN', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS DAILY_BURN'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL has: AS RERAISED', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AS RERAISED'), 'OK', 'FAIL: not the V169 body')
UNION ALL
SELECT 'V169.2 daily scan DDL lacks: GB_24H', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'GB_24H'), 'OK', 'FAIL: still V163 text')
UNION ALL
SELECT 'V169.2 daily scan DDL lacks: AND w.AUTO_SUSPEND IS NOT NULL', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AND w.AUTO_SUSPEND IS NOT NULL'), 'OK', 'FAIL: still V163 text')
UNION ALL
SELECT 'V169.2 daily scan DDL lacks: PARTITION BY DATABASE_NAME', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'PARTITION BY DATABASE_NAME'), 'OK', 'FAIL: still V163 text')
UNION ALL
SELECT 'V169.2 daily scan DDL lacks: alert scan daily v5 (V163:', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'alert scan daily v5 (V163:'), 'OK', 'FAIL: still V163 text');

-- V169.3 the next morning: the heartbeat reads 14/14 and none of the changed arms failed.
SELECT 'V169.3 daily scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan daily 14/14 rule blocks ok (daily)', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                'FAIL: no ALERT_SCAN_DAILY row') AS RESULT
UNION ALL
SELECT 'V169.3 no rule_block_failed for the changed arms (24h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE ERROR_TYPE IN ('rule_block_failed', 'recon_scan_failed')
               AND (CONTEXT LIKE 'rule COST_BUDGET_PACE %' OR CONTEXT LIKE 'rule COST_FORECAST_BREACH %' OR CONTEXT LIKE 'rule COST_CONTRACT_BREACH %' OR CONTEXT LIKE 'rule COST_STORAGE_SURGE %' OR CONTEXT LIKE 'rule COST_EGRESS_SPIKE %' OR CONTEXT LIKE 'rule COST_IDLE_OPPORTUNITY %' OR CONTEXT LIKE 'rule SEC_TRUST_REGRESSION %' OR CONTEXT LIKE 'rule DQ_RECON_ERROR %')
               AND LOGGED_AT >= DATEADD('hour', -24, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE for the rule');

-- V169.4 the morning after the first post-apply daily scan (and after any failing reconciliation cycle): the newest
--        error cycle in ETL_RECON_RESULTS was paged by an event raised AFTER it loaded. CHECK = it folded into an
--        older event (FIRST RUN (a) the transition into a pre-V169 scan-date key, or (b) a same-date re-run that
--        failed after that date's page) or has no event; review Operations > Pipeline SLA > Data checks >
--        Reconciliation errors. RAISED_AT and LATEST_LOAD are both TIMESTAMP_NTZ, compared as stored.
WITH n AS (
    SELECT MAX(LATEST_LOAD) AS NEWEST_LOAD,
           'DQ_RECON_ERROR|' || TO_VARCHAR(TO_DATE(MAX(LATEST_LOAD))) AS CYCLE_KEY
    FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
)
SELECT 'V169.4 newest reconciliation error cycle was paged' AS CHECK_NAME,
       CASE WHEN n.NEWEST_LOAD IS NULL THEN 'OK: no reconciliation error in the look-back'
            WHEN e.RAISED_AT IS NULL THEN 'CHECK: no event keyed ' || n.CYCLE_KEY
                 || ' (below the rule threshold, the rule disabled, or no daily scan since that load)'
            WHEN e.RAISED_AT < n.NEWEST_LOAD THEN 'CHECK: the cycle loaded ' || TO_VARCHAR(n.NEWEST_LOAD)
                 || ' folded into the event raised ' || TO_VARCHAR(e.RAISED_AT)
                 || ' and was not paged; review Reconciliation errors'
            ELSE 'OK' END AS RESULT
FROM n
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = n.CYCLE_KEY;

-- #####################################################################
--  V170 PART B  (from outputs/gen_v170.py, cluster incidents; verifies snowflake/migrations/V170__incident_declare_actor_and_proposals.sql)
-- #####################################################################
-- RUN_NEXT PART B -- V170 verify (read-only after the session pin; every other statement is a SELECT).
-- Central first (correction 5): the proposal view (V170.3) and V170.6 window Central wall-clock NTZ timestamps
-- against CURRENT_TIMESTAMP(), so a UTC worksheet would read a window about 5h off from the app.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- V170.1  both SP_INCIDENT_DECLARE overloads exist: expect TWO rows, the 4-arg and the 5-arg (P_ACTOR VARCHAR).
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, LAST_ALTERED
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_INCIDENT_DECLARE'
ORDER BY ARGUMENT_SIGNATURE;

-- V170.2  the view carries the V170 text: expect BAND_TOKENS_OK, USER_KINDS_OK and TASK_SELF_EVIDENCE_OK all TRUE.
SELECT CONTAINS(d, '''DAILY'', ''WARN'', ''CRIT'', ''MED'', ''HIGH'', ''EXH'', ''ALL''') AS BAND_TOKENS_OK,
       CONTAINS(d, '''SEC_LOGIN_TAKEOVER'', ''SEC_ADMIN_GRANT''') AS USER_KINDS_OK,
       CONTAINS(d, 'IFF(FAMILY = ''PIPE_TASK_FAILURES'', 0, MATCHED_TASK_FAILURES)') AS TASK_SELF_EVIDENCE_OK
FROM (SELECT GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS') AS d);

-- V170.3  open proposals by kind and confidence: no ENTITY_NAME of EXH or ALL is left, and PIPE_TASK_FAILURES rows
-- are LOW or MEDIUM unless a task change matched (MATCHED_OBJECT_CHANGES > 0).
SELECT ENTITY_KIND, CONFIDENCE, COUNT(*) AS PROPOSALS,
       COUNT_IF(UPPER(ENTITY_NAME) IN ('EXH', 'ALL')) AS BAND_AS_ENTITY,
       COUNT_IF(SPLIT_PART(PROPOSAL_KEY, '|', 1) = 'PIPE_TASK_FAILURES') AS TASK_FAILURE_PROPOSALS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS
GROUP BY 1, 2
ORDER BY 1, 2;

-- V170.4  (R2-030 owner list) OPEN / MITIGATED incidents with NO member: each one is a past empty declare (or the
-- auto-declare race); close each from Control Room > Incidents with its root cause. Expect no NEW rows after V170.
SELECT i.INCIDENT_ID, i.TITLE, i.COMPANY, i.STATUS, i.DETECTED_AT, i.DECLARED_BY
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
WHERE i.STATUS IN ('OPEN', 'MITIGATED')
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m WHERE m.INCIDENT_ID = i.INCIDENT_ID)
ORDER BY i.DETECTED_AT;

-- V170.5  (R2-093 owner list) open incidents holding an EXH-band member alert: compare each with any other open
-- incident of the same family (FAMILY_OPEN_INCIDENTS > 1 is the duplicate the old view allowed).
SELECT i.INCIDENT_ID, i.STATUS, i.DETECTED_AT, a.RULE_ID, a.DEDUPE_KEY,
       (SELECT COUNT(DISTINCT i2.INCIDENT_ID)
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2 ON m2.INCIDENT_ID = i2.INCIDENT_ID AND m2.MEMBER_KIND = 'ALERT'
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a2 ON a2.EVENT_ID = m2.REF_ID
        WHERE i2.STATUS IN ('OPEN', 'MITIGATED')
          AND SPLIT_PART(COALESCE(a2.DEDUPE_KEY, a2.EVENT_ID), '|', 1) = a.RULE_ID) AS FAMILY_OPEN_INCIDENTS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
WHERE SPLIT_PART(a.DEDUPE_KEY, '|', 2) = 'EXH'
  AND i.STATUS IN ('OPEN', 'MITIGATED')
ORDER BY i.DETECTED_AT;

-- V170.6  after the first manual declare from the 4.609.0 app made 30 s or more after the apply: the newest manual
-- incidents and their members name the declaring DBA (not the app owner). Auto-declared rows keep
-- SP_INCIDENT_AUTODECLARE. The app re-reads SCHEMA_VERSION on a 30 s tier while its 4 h schema cache lacks 170;
-- before you declare, Control Room's SQL preview must end with the viewer as a 5th argument (4 arguments = the
-- app does not see V170 yet: press Refresh data and look again). An incident declared before the apply, or
-- through the 4-arg CALL, keeps the app owner (history is not rewritten).
SELECT i.INCIDENT_ID, i.DETECTED_AT, i.DECLARED_BY,
       LISTAGG(DISTINCT m.LINKED_BY, ', ') AS LINKED_BY, COUNT(m.REF_ID) AS MEMBERS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
LEFT JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID
WHERE i.DECLARED_BY <> 'SP_INCIDENT_AUTODECLARE'
  AND i.DETECTED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY i.DETECTED_AT DESC
LIMIT 20;

-- #####################################################################
--  V171 PART B  (from outputs/gen_v171.py, cluster ops-dq; verifies snowflake/migrations/V171__ops_selfwatch_digest_refgaps_seed.sql)
-- #####################################################################
-- PART B -- V171 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
-- V171.1 - V171.4 now.
SELECT 'V171.1 SP_CANARY_SENTINEL is the V171 proc' AS CHECK_NAME,
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'cannot see column drift') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'render SLA check failed: ') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'sentinel v2: ') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'HALF_TO_EVEN')
           AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'Likely ACCOUNT_USAGE column drift') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 'APP_USAGE.RENDER_MS not readable') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()'), 's (7d, n='),
           'OK', 'FAIL: SP_CANARY_SENTINEL is not the V171 text') AS RESULT
UNION ALL
SELECT 'V171.2 SP_DAILY_DIGEST is the V171 proc' AS CHECK_NAME,
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'WAREHOUSE_SPEND_USD=') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'PERIOD_START < CURRENT_DATE()') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'FACT_TASK_DAILY') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'RLIKE(cm, ') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'never total spend') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'REGEXP_SUBSTR_ALL')
           AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), '7; SPEND_USD=') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'Last 7 days, all companies'),
           'OK', 'FAIL: SP_DAILY_DIGEST is not the V171 text') AS RESULT
UNION ALL
SELECT 'V171.3 SP_SCAN_REF_GAPS is the V171 proc' AS CHECK_NAME,
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), 'TO_VARCHAR(x.SRC_IDNTFTN_VAL)') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), 'ref_gap_check_failed') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), 'LET c_checks CURSOR FOR res') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), '-20662')
           AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), 'scan_sql') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()'), 'LISTAGG('),
           'OK', 'FAIL: SP_SCAN_REF_GAPS is not the V171 text') AS RESULT
UNION ALL
SELECT 'V171.4 exactly one CREDIT_PRICE_OVERRIDE row',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_OVERRIDE') = 1,
           'OK', 'FAIL: zero or several CREDIT_PRICE_OVERRIDE rows (keep one; UPDATE it, never INSERT a second)');

-- V171.5 the next morning, after the 07:20 CT run. expect FACTS to start 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD='
--        and, on a templated body, 'The 7 complete days to yesterday'.
SELECT DIGEST_DATE, MODEL, BODY_SOURCE, GROUNDING_OK, FIGURES_CHECKED, LEFT(FACTS, 300) AS FACTS_START,
       STARTSWITH(FACTS, 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=') AS V171_FACTS, LEFT(BODY, 200) AS BODY_START
  FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
 ORDER BY DIGEST_DATE DESC
 LIMIT 1;

-- V171.6 after the next daily alert scan. expect no ref_gap_scan_failed row since the apply (a partial failure
--        now logs ref_gap_check_failed with the check name in CONTEXT, and the healthy checks still alert).
SELECT LOGGED_AT, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE PAGE = 'AlertScan' AND ERROR_TYPE IN ('ref_gap_check_failed', 'ref_gap_scan_failed')
   AND LOGGED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
 ORDER BY LOGGED_AT DESC
 LIMIT 20;

-- #####################################################################
--  V172 PART B  (from outputs/gen_v172.py, cluster detection; verifies snowflake/migrations/V172__detection_scans_company_and_accuracy.sql)
-- #####################################################################
-- PART B -- V172 verify (READ-ONLY after the session pin). V172.1 + V172.2 right after the apply; V172.3 after the
-- next 06:40 / 06:50 Central change scans; V172.4 after the next TASK_ANOMALY_SWEEP (07:00 Central). Every RESULT
-- should read OK (or the count named); paste the grids back.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

SELECT 'V172.1 SCHEMA_VERSION has 172' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) = 1,
           'OK', 'FAIL: V172 did not finish') AS RESULT
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: COMPANY_FOR_DATABASE(g.DATABASE_NAME)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'COMPANY_FOR_DATABASE(g.DATABASE_NAME)'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: [[:space:]]', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), '[[:space:]]'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: HAVING COUNT(a.RID) > 0', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'HAVING COUNT(a.RID) > 0'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: AS RUN_KEY', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'AS RUN_KEY'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: HALF_TO_EVEN', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'HALF_TO_EVEN'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL has: ORDER BY COMPLETED_TIME DESC NULLS LAST', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'ORDER BY COMPLETED_TIME DESC NULLS LAST'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL lacks: CHR(10)', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'CHR(10)'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL lacks: TOTAL_CR / NULLIF(t.AFTER_CALLS', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'TOTAL_CR / NULLIF(t.AFTER_CALLS'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_WAREHOUSE_CHANGE_SCAN DDL has: HALF_TO_EVEN', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()'), 'HALF_TO_EVEN'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_WAREHOUSE_CHANGE_SCAN DDL has: FM90.0', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()'), 'FM90.0'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_WAREHOUSE_CHANGE_SCAN DDL lacks: s->', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()'), 's->'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_SCAN_SCHEMA_DRIFT DDL has: COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()'), 'COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_SCAN_SCHEMA_DRIFT DDL has: b.DEDUPE_KEY', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()'), 'b.DEDUPE_KEY'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_SCAN_SCHEMA_DRIFT DDL lacks: IFF(SPLIT_PART(a.FQN', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()'), 'IFF(SPLIT_PART(a.FQN'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_SCAN_CLOUD_SVC_ANOMALY DDL has: INTO :enabled_cnt, :zthr', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()'), 'INTO :enabled_cnt, :zthr'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_SCAN_CLOUD_SVC_ANOMALY DDL has: IF (:enabled_cnt = 0) THEN', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()'), 'IF (:enabled_cnt = 0) THEN'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_SCAN_CLOUD_SVC_ANOMALY DDL lacks: IF (:zthr IS NULL)', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()'), 'IF (:zthr IS NULL)'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL has: COMPANY_FOR_DATABASE(d.DATABASE_NAME)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'COMPANY_FOR_DATABASE(d.DATABASE_NAME)'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL has: COMPANY_FOR_DATABASE(v.DB)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'COMPANY_FOR_DATABASE(v.DB)'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL has: COMPANY_FOR_DATABASE(s.DB)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'COMPANY_FOR_DATABASE(s.DB)'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL has: Cost Intelligence > Contract & Forecast', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'Cost Intelligence > Contract & Forecast'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL has: RLIKE(cm,', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'RLIKE(cm,'), 'OK', 'FAIL: not the V172 body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL lacks: IFF(d.DATABASE_NAME LIKE', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'IFF(d.DATABASE_NAME LIKE'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL lacks: IFF(v.DB LIKE', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'IFF(v.DB LIKE'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL lacks: IFF(s.DB LIKE', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'IFF(s.DB LIKE'), 'OK', 'FAIL: still the old body')
UNION ALL
SELECT 'V172.1 SP_ANOMALY_SWEEP DDL lacks: Admin > Org spend', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'Admin > Org spend'), 'OK', 'FAIL: still the old body');

-- V172.2 the re-stamps landed: no registry row and no live, unlinked event of the five rules disagrees with the
--        V044 classification of its database.
SELECT 'V172.2 registry COMPANY = COMPANY_FOR_DATABASE' AS CHECK_NAME,
       IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || COUNT(*) || ' row(s) still on the old stamp') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
JOIN (
      SELECT d.DATABASE_NAME, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME) AS CO
      FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d
) m ON m.DATABASE_NAME = r.DATABASE_NAME
WHERE r.COMPANY IS DISTINCT FROM m.CO
UNION ALL
SELECT 'V172.2 live unlinked events of the five rules on the V044 company',
       IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || COUNT(*) || ' event(s) still on the old stamp')
FROM (
    SELECT x.COMPANY, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS CO
    FROM (
        SELECT e.COMPANY, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')
                AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
    ) x
) y
WHERE y.COMPANY IS DISTINCT FROM y.CO;

-- V172.3 after the next 06:40 / 06:50 scans: tracking rows read Hr/Min/Sec, and the R3-nulled PROCEDURE baselines
--        are re-frozen (BASELINE_FROM set again; NO_BASELINE is an honest outcome for a thin re-freeze window).
SELECT 'V172.3 object registry VERDICT_DETAIL in Hr/Min/Sec (tracking rows)' AS CHECK_NAME,
       IFF(COUNT_IF(VERDICT_DETAIL LIKE '%s->%') = 0,
           'OK (' || COUNT(*) || ' tracking row(s))', 'CHECK: ' || COUNT_IF(VERDICT_DETAIL LIKE '%s->%')
           || ' row(s) still raw -- has the 06:50 scan run since the apply?') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
WHERE CURRENT_DATE() <= TRACKING_UNTIL AND VERDICT_DETAIL IS NOT NULL
UNION ALL
SELECT 'V172.3 warehouse registry VERDICT_DETAIL in Hr/Min/Sec (tracking rows)',
       IFF(COUNT_IF(VERDICT_DETAIL LIKE '%min/d%') = 0,
           'OK (' || COUNT(*) || ' tracking row(s))', 'CHECK: ' || COUNT_IF(VERDICT_DETAIL LIKE '%min/d%')
           || ' row(s) still raw -- has the 06:40 scan run since the apply?')
FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
WHERE CURRENT_DATE() <= TRACKING_UNTIL AND VERDICT_DETAIL IS NOT NULL
UNION ALL
SELECT 'V172.3 tracking PROCEDURE rows with no frozen baseline',
       IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || COUNT(*) || ' row(s) -- has the 06:50 scan run since the apply?')
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
WHERE OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= TRACKING_UNTIL AND BASELINE_FROM IS NULL;

-- V172.4 after the next TASK_CHANGE_IMPACT_SCAN (06:50) and TASK_ANOMALY_SWEEP (07:00 Central): no guarded arm
--        started failing at the apply. A compile surprise in a re-derived arm is swallowed by its EXCEPTION and
--        logged to APP_ERROR_LOG. An arm is PAGE + ERROR_TYPE + CONTEXT (the CONTEXT tells the volume-drop and
--        DQ_BREACH arms apart). FAIL names an arm that logged since the apply and was silent in the 14 days before.
--        An arm that logged before V172 too is pre-existing, listed after OK and never failed: on an account without
--        TASK_VERSIONS, ORGANIZATION_USAGE or Cortex it logs on every run by design. R172.4 shows both sides. The
--        boundary is the apply itself: APPLIED_AT (RUN_NEXT pins Central before the apply) and LOGGED_AT (the tasks
--        run in the account zone) are both Central wall-clock, and the old bodies' last runs fall before it.
SELECT 'V172.4 no new ' || p.PAGE || ' error since the apply' AS CHECK_NAME,
       CASE WHEN MAX(a.T) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 172 row'
            WHEN COUNT_IF(x.ROWS_BEFORE = 0) > 0
                THEN 'FAIL: new since the apply (read R172.4): '
                     || LISTAGG(IFF(x.ROWS_BEFORE = 0, x.ARM, NULL), '; ')
            ELSE 'OK' END
       || IFF(COUNT_IF(x.ROWS_BEFORE > 0) > 0,
              ' -- pre-existing, logged in the 14 days before V172 too: '
              || LISTAGG(IFF(x.ROWS_BEFORE > 0, x.ARM, NULL), '; '), '') AS RESULT
FROM (SELECT column1 AS PAGE FROM VALUES ('AnomalySweep'), ('ChangeImpactScan')) p
CROSS JOIN (SELECT MAX(APPLIED_AT) AS T FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) a
LEFT JOIN (
    SELECT l.PAGE, l.ERROR_TYPE, l.CONTEXT,
           COALESCE(l.ERROR_TYPE, '?') || ' (' || COALESCE(l.CONTEXT, '?') || ')' AS ARM, MAX(a.T) AS T,
           COUNT_IF(l.LOGGED_AT < a.T) AS ROWS_BEFORE, COUNT_IF(l.LOGGED_AT >= a.T) AS ROWS_SINCE
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
    JOIN (SELECT MAX(APPLIED_AT) AS T FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) a
      ON l.LOGGED_AT >= DATEADD('day', -14, a.T)
    WHERE l.PAGE IN ('AnomalySweep', 'ChangeImpactScan')
    GROUP BY l.PAGE, l.ERROR_TYPE, l.CONTEXT
    HAVING COUNT_IF(l.LOGGED_AT >= a.T) > 0
) x ON x.PAGE = p.PAGE
GROUP BY p.PAGE
ORDER BY 1;

-- #####################################################################
--  ALL SEVEN  (added for the package; read-only)
-- #####################################################################
-- ALL.1 (now) V166 through V172 are registered: expect 7 rows (RUN_NEXT's stage-2 quick check reads 162-172).
SELECT VERSION, APPLIED_AT, APPLIED_BY
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 166 AND 172
ORDER BY VERSION;

-- ALL.2 (the next day: FACT_SECURITY_CHANGE loads from the hourly extract) the stage-2 apply session's change-risk
-- footprint, anchored on the SCHEMA_VERSION rows (Central wall clock, as RUN_NEXT's pinned session stamped them),
-- so it works any day. The window (30 minutes either side) also takes in whatever the same user ran around the
-- apply: stage 1's statements, and OWNER_REPAIRS PART 1.1, whose SP_LOAD_MARTS_V27('DAILY', 365) runs
-- CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR inside the proc (a PART 2 V167 step 3 HOURLY reload run this
-- soon adds _OW_ALLOC_BASE the same way). The security loader scores any CREATE OR REPLACE ... TABLE as
-- DESTRUCTIVE, so such a row is a MEDIUM DESTRUCTIVE CREATE_TABLE_AS_SELECT (or CREATE_TABLE) row; NOTE labels it
-- 'expected: ...' -- a loader's session scratch table, not a change.
-- expect: no DESTRUCTIVE row and no HIGH / CRITICAL row with an EMPTY NOTE; only LOW / MEDIUM CREATE and ALTER
-- rows (the 19 CREATE OR REPLACE PROCEDUREs, the V170 CREATE OR REPLACE VIEW, V167's ADD COLUMN, the ALTER SESSION
-- lines), or none; plus, when PART 1.1 ran inside the window, the labelled MEDIUM DESTRUCTIVE temp-table row.
-- Never delete these rows.
SELECT f.QUERY_TYPE, f.CHANGE_KIND, f.RISK_LEVEL,
       IFF(f.QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
             AND CONTAINS(UPPER(f.QUERY_PREVIEW), 'TEMPORARY TABLE _OW_'),
           'expected: an OWNER_REPAIRS loader CALL''s session temp table, not a change', '') AS NOTE,
       COUNT(*) AS ROWS_IN_APPLY_WINDOW,
       MIN(LEFT(f.QUERY_PREVIEW, 90)) AS EXAMPLE
FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
CROSS JOIN (SELECT MIN(APPLIED_AT) AS FIRST_AT, MAX(APPLIED_AT) AS LAST_AT, MAX(APPLIED_BY) AS BY_USER
            FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
            WHERE VERSION BETWEEN 166 AND 172) a
WHERE f.USER_NAME = a.BY_USER
  AND CONVERT_TIMEZONE('America/Chicago', f.EVENT_TS)::TIMESTAMP_NTZ
      BETWEEN DATEADD('minute', -30, a.FIRST_AT) AND DATEADD('minute', 30, a.LAST_AT)
GROUP BY 1, 2, 3, 4
ORDER BY 4, 3, 2, 1;

ALTER SESSION UNSET TIMEZONE;
