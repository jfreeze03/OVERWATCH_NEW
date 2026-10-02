-- =====================================================================
--  OVERWATCH -- PREFLIGHT_V166_V172.sql  (READ-ONLY; run BEFORE stage 2 of RUN_NEXT.sql, which applies V166-V172)
--  App 4.609.0, the round-2 bug-hunt wave. Changes nothing and sends nothing: SELECT only, plus a session-only
--  TIMEZONE pin, which comes FIRST: ALERT_EVENTS.RAISED_AT and the other stored NTZ clocks are Central wall-clock,
--  and the V168 / V169 / V170 grids compare them with CURRENT_TIMESTAMP() / CURRENT_DATE() exactly as the scans
--  do -- in the owner's UTC worksheet those windows sit 5-6 h off. Every section below is the cluster generator's
--  output verbatim (outputs/gen_v166.py .. gen_v172.py), built from the SAME text as the migration it previews.
--  Paste every grid back; an empty grid is an answer ('no rows'). If a grid errors (a missing grant, a view this
--  account does not expose), note the error and run the rest from the next '-- P' header down.
--  Run it after (or beside) snowflake/run/PREFLIGHT_WAVE4.sql. The grids read today's data, so they work before or
--  after stage 1; rules that V162 / V163 add (SEC_TRUST_REGRESSION in P169.7, the USER kinds in P170.1) show
--  nothing for those rules until stage 1 is applied.
--
--  READ FIRST, before deciding to apply:
--    P166.1  the FACT_STORAGE_DAILY rows V166's in-migration MERGE rewrites (TB-days added per database).
--    P166.2 / P166.3  holes a failed app-cost / storage-truth run already left (OWNER_REPAIRS R166.4 / R166.5 heal them).
--    P166.4  OPTIONAL and the heaviest grid (7 days of QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY x SESSIONS): it
--            may time out on WH_ALFA_ADMIN (300 s per V002; the 2026-09-29 live probe saw 1800 s) -- check with
--            SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN, skip it, or run it
--            on a warehouse with a longer one. Its 'older than 30 days' bucket answers the C10 session-lookback
--            width question.
--    P167.1  the MART_PATTERN_COST_DAILY twin rows V167's in-migration DELETE removes (rows, credits, day range);
--            P167.2 the same-stamp multi-company groups it keeps; P167.3 MISKEYED > 0 proves the Cortex Code day-key
--            defect; P167.4 task-graph runs that cross Central midnight.
--    P168.1  stranded OPEN PERF events (WILL_AUTO_CLEAR = what the first hourly scan after V168 resolves).
--    P168.2 - P168.4  the PIPE_COPY_FAILURES key change, its carry-over duplicates (OWNER_REPAIRS R168.1, an owner
--            decision) and the SEC_NEW_ADMIN_NETWORK pairs / failures-only events.
--    P169.1 - P169.7  per-arm previews of V169; after the apply resolve their listed historic events in the Alerts UI,
--            except P169.4's next-day DQ_RECON_ERROR twins: OWNER_REPAIRS R169.1 (OPTIONAL) closes those as SUPERSEDED.
--    P170.1 vs P170.2  open proposals as V170 will classify them vs the live view; P170.3 expect ONE overload today.
--    P171.1  digest facts old (today-inclusive) vs new (7 complete days): V171 a little below V165, never ~3x;
--            P171.5 every RESULT OK (the OPS_SLOW_RENDER title CASE compiles).
--    P172.1  every RESULT OK (the Hr/Min/Sec template compiles); P172.2 - P172.5 the V172 in-migration re-stamps,
--            baseline nulls and re-freezes; P172.6 the two anomaly rules' state and the sweep's model.
-- =====================================================================

ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- FIRST: session only; the stored NTZ clocks are Central
USE ROLE SNOW_ACCOUNTADMINS;

-- #####################################################################
--  V166 PREFLIGHT  (from outputs/gen_v166.py, cluster loaders; previews snowflake/migrations/V166__fact_loader_window_integrity.sql)
-- #####################################################################
-- PREFLIGHT -- V166 section (READ-ONLY). Run before applying V166; changes nothing.
-- What the grids answer: P166.1 which FACT_STORAGE_DAILY rows V166's repair MERGE rewrites (its own source SELECT,
-- joined on its own ON clause) and how many TB-days each database gains; P166.2 / P166.3 whether a failed
-- app-cost or storage-truth run already left a hole (R2-011; V166 stops new ones, the owner-run heal fills old
-- ones); P166.4 OPTIONAL (the last 7 days of QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY, matched against
-- SESSIONS full retention) how many of the last week's attributed credits the 30-day session lookback relabels
-- from (unknown).

-- P166.1 the R2-009 repair, previewed. One row per database with a re-created or clone-refreshed day in the
--        view's 365 days. expect a handful of rows (none = nothing to repair); every value positive.
WITH s AS (
    SELECT USAGE_DATE AS DAY, DATABASE_NAME,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
)
SELECT t.DATABASE_NAME,
       COUNT(*) AS NAME_DAYS_REPAIRED,
       MIN(t.DAY) AS FIRST_DAY, MAX(t.DAY) AS LAST_DAY,
       ROUND(SUM(s.DB_BYTES - t.DB_BYTES) / POWER(1024, 4), 3) AS DB_TB_DAYS_ADDED,
       ROUND(SUM(s.FAILSAFE_BYTES - t.FAILSAFE_BYTES) / POWER(1024, 4), 3) AS FAILSAFE_TB_DAYS_ADDED
  FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
  JOIN s ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME
 GROUP BY t.DATABASE_NAME
 ORDER BY DB_TB_DAYS_ADDED DESC;

-- P166.2 R2-011 calendar gaps in the two facts. expect no rows; a row is a day a failed run deleted for good.
WITH
    f AS (
        SELECT 'FACT_APP_COST_DAILY' AS FACT, DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY GROUP BY DAY
        UNION ALL
        SELECT 'FACT_STORAGE_ACCOUNT_DAILY', DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY GROUP BY DAY
    ),
    b AS (SELECT FACT, MIN(DAY) AS LO, MAX(DAY) AS HI FROM f GROUP BY FACT),
    n AS (SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS I FROM TABLE(GENERATOR(ROWCOUNT => 401))),
    gaps AS (
        SELECT b.FACT, DATEADD('day', n.I, b.LO) AS MISSING_DAY,
               DATEDIFF('day', DATEADD('day', n.I, b.LO), CURRENT_DATE()) + 1 AS DAYS_BACK_TO_HEAL
        FROM b
        JOIN n ON DATEADD('day', n.I, b.LO) <= b.HI
        LEFT JOIN f ON f.FACT = b.FACT AND f.DAY = DATEADD('day', n.I, b.LO)
        WHERE f.DAY IS NULL
    )
SELECT FACT, MISSING_DAY, DAYS_BACK_TO_HEAL
  FROM gaps
 ORDER BY FACT, MISSING_DAY;

-- P166.3 R2-011 failed runs of the two daily tasks (ACCOUNT_USAGE, up to ~45 min behind). A FAILED run on day D
--        whose error came from the INSERT predicts a P166.2 hole at D-3.
SELECT NAME, SCHEDULED_TIME, STATE, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
   AND NAME IN ('TASK_LOAD_APP_COST', 'TASK_LOAD_STORAGE_TRUTH') AND STATE = 'FAILED'
 ORDER BY SCHEDULED_TIME;

-- P166.4 OPTIONAL C10 session-age buckets for the last 7 days of attributed queries. The 7-30 day bucket is what
--        V166 relabels from (unknown) on newly loaded days; the older-than-30 bucket is what a wider lookback would
--        add (the C10-width owner question), so SESSIONS is read over its full retention (365 days; only the
--        week's sessions are aggregated). The heaviest grid here: on WH_ALFA_ADMIN (300 s statement timeout,
--        V002) it can time out; skip it or run it on a warehouse with a longer timeout.
WITH q AS (
    SELECT QUERY_ID, SESSION_ID, START_TIME
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE START_TIME >= DATEADD('day', -7, CURRENT_DATE())
),
c AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
     WHERE START_TIME >= DATEADD('day', -7, CURRENT_DATE())
     GROUP BY 1
),
s AS (
    SELECT SESSION_ID, MIN(CREATED_ON) AS CREATED_ON
      FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
     WHERE SESSION_ID IN (SELECT SESSION_ID FROM q)
     GROUP BY 1
)
SELECT CASE WHEN s.SESSION_ID IS NULL THEN 'no SESSIONS row (stays unknown)'
            WHEN s.CREATED_ON >= DATEADD('day', -7, q.START_TIME::DATE) THEN 'within 7 days (resolved before V166)'
            WHEN s.CREATED_ON >= DATEADD('day', -30, q.START_TIME::DATE) THEN '7-30 days (fixed by V166)'
            ELSE 'older than 30 days (stays unknown)' END AS SESSION_AGE,
       COUNT(*) AS QUERIES,
       ROUND(SUM(c.CR), 2) AS CREDITS
  FROM q
  JOIN c ON c.QUERY_ID = q.QUERY_ID
  LEFT JOIN s ON s.SESSION_ID = q.SESSION_ID
 GROUP BY 1
 ORDER BY CREDITS DESC;

-- #####################################################################
--  V167 PREFLIGHT  (from outputs/gen_v167.py, cluster marts; previews snowflake/migrations/V167__mart_loader_edges_ai_coverage_pattern_reload.sql)
-- #####################################################################
-- PREFLIGHT_V167 (READ-ONLY). Run before applying V167; changes nothing.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P167.1 R2-010: the MART_PATTERN_COST_DAILY rows the in-migration twin DELETE would remove (its own USING / WHERE)
SELECT COUNT(*) AS TWIN_ROWS, ROUND(SUM(t.CREDITS_ATTRIBUTED), 2) AS TWIN_CREDITS,
       SUM(t.RUNS) AS TWIN_RUNS, MIN(t.DAY) AS FIRST_DAY, MAX(t.DAY) AS LAST_DAY,
       COUNT(DISTINCT t.COMPANY) AS COMPANIES
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
JOIN (
    SELECT DAY, QUERY_HASH, DATABASE_NAME, MAX(LOAD_TS) AS NEWEST_TS
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1
) g
  ON t.DAY = g.DAY AND t.QUERY_HASH = g.QUERY_HASH AND t.DATABASE_NAME = g.DATABASE_NAME
  AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS);

-- P167.2 R2-010: multi-company groups whose rows all share one LOAD_TS window -- legitimate, KEPT by the DELETE
SELECT COUNT(*) AS KEPT_MULTI_COMPANY_GROUPS, MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY
FROM (
    SELECT DAY, QUERY_HASH, DATABASE_NAME
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1 AND MIN(LOAD_TS) >= DATEADD('minute', -10, MAX(LOAD_TS))
) k;

-- P167.3 R2-052: do the Cortex Code views stamp a non-Central offset? MISKEYED > 0 proves the day-key defect.
SELECT 'Snowsight' AS SOURCE, TO_CHAR(USAGE_TIME, 'TZH:TZM') AS STORED_OFFSET, COUNT(*) AS ROWS_7D,
       COUNT_IF(USAGE_TIME::DATE <> CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE) AS MISKEYED
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
WHERE USAGE_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2
UNION ALL
SELECT 'CLI', TO_CHAR(USAGE_TIME, 'TZH:TZM'), COUNT(*),
       COUNT_IF(USAGE_TIME::DATE <> CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE)
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
WHERE USAGE_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2;

-- P167.4 R2-014: task-graph runs that cross Central midnight in the last 30 days (each was a phantom row candidate)
SELECT COUNT(*) AS STRADDLING_RUNS, COUNT(DISTINCT ROOT_NAME) AS PIPELINES
FROM (
    SELECT COALESCE(GRAPH_RUN_GROUP_ID::VARCHAR, QUERY_ID) AS RUN_KEY,
           MIN_BY(NAME, QUERY_START_TIME) AS ROOT_NAME,
           MIN(QUERY_START_TIME) AS FIRST_START,
           MAX(QUERY_START_TIME) AS LAST_START
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE QUERY_START_TIME >= DATEADD('day', -30, CURRENT_DATE())
      AND STATE IN ('SUCCEEDED', 'FAILED')
    GROUP BY 1
) r
WHERE DATE(FIRST_START) < DATE(LAST_START);

-- #####################################################################
--  V168 PREFLIGHT  (from outputs/gen_v168.py, cluster alerts; previews snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql)
-- #####################################################################
-- ====================================================================================================
--  V168 PREFLIGHT (read-only; run BEFORE applying V168, in a Central session). SP_ALERT_SCAN keys and sweeps.
--  P168.1 and the second P168.4 grid compare ALERT_EVENTS.RAISED_AT (Central wall-clock TIMESTAMP_NTZ) with
--  CURRENT_TIMESTAMP(): a UTC worksheet shifts them 5-6h. Each grid runs the arm's OWN text (outputs/gen_v168.py).
--  Changes nothing.
-- ====================================================================================================

-- P168.1 R2-034 census: OPEN PERF events older than 48h that V162's sweep can no longer clear. WILL_AUTO_CLEAR =
--        the ones the first hourly scan after V168 resolves AUTO_CLEARED (their scope is below CLEAR now);
--        the rest are still firing and stay OPEN, which is correct. Re-run after the first scan: WILL_AUTO_CLEAR -> 0.
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
SELECT ev.RULE_ID, COUNT(*) AS OPEN_OLDER_THAN_48H,
       COUNT_IF(f.DEDUPE_KEY IS NULL) AS WILL_AUTO_CLEAR,
       MIN(ev.RAISED_AT) AS OLDEST_RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
LEFT JOIN (SELECT DISTINCT DEDUPE_KEY FROM firing) f
       ON f.DEDUPE_KEY = ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)
WHERE ev.STATUS = 'OPEN'
  AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')
  AND ev.RAISED_AT < DATEADD('hour', -48, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY 1;

-- P168.2 R2-035: what arm [14] keys now (per table and Central failure day, yesterday + today) and whether the key
--        already exists. A NULL EXISTING_STATUS on a yesterday row is the one legitimate apply-day event (a day
--        whose full count first crossed the band). HELD_BY_A_V162_EVENT on a today row = a V162 event (title
--        '... (24h)', possibly yesterday's carry-over re-minted after midnight) that absorbs today's new failures of
--        that band until midnight (V168 FIRST RUN (1)): watch that table on Operations today.
WITH p AS (
            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,
                   TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY,
                   MAX(PIPE_NAME) AS PIPE,
                   COUNT(*) AS FAILED_FILES,
                   MAX(FIRST_ERROR_MESSAGE) AS SAMPLE_ERROR
            FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
            WHERE LAST_LOAD_TIME >= DATEADD('hour', -50, CURRENT_TIMESTAMP())   -- V168: prune only; yesterday 00:00 Central is at most 49h back (DST fall-back included)
              AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))
                  >= DATEADD('day', -1, TO_DATE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())))
              AND STATUS IN ('Load failed', 'Partially loaded')
            GROUP BY 1, 2, 3, 4
        )
SELECT p.DB, p.SCH, p.TBL, p.FAIL_DAY, p.FAILED_FILES, p.PIPE,
       'PIPE_COPY_FAILURES|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY) AS DEDUPE_KEY_PREVIEW,
       e.STATUS AS EXISTING_STATUS, e.TITLE AS EXISTING_TITLE,
       e.TITLE LIKE '%failed file load(s) (24h)' AS HELD_BY_A_V162_EVENT
FROM p
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = 'PIPE_COPY_FAILURES|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY)
ORDER BY p.FAIL_DAY DESC, p.FAILED_FILES DESC;

-- P168.3 R2-035 carry-over duplicates (last 14 days): PIPE_COPY_FAILURES events whose key day had NO failure for
--        that table (a scan-day key over yesterday's files). Still-OPEN / ACK ones may be closed with the optional
--        owner repair R168.1; SNOOZED ones are left to wake.
WITH f AS (
    SELECT TABLE_CATALOG_NAME || '.' || TABLE_SCHEMA_NAME || '.' || TABLE_NAME AS FQN,
           TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY, COUNT(*) AS N
    FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
    WHERE LAST_LOAD_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
      AND STATUS IN ('Load failed', 'Partially loaded')
    GROUP BY 1, 2
)
SELECT e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.DEDUPE_KEY, e.TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN f ON f.FQN = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
           AND f.FAIL_DAY = TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 4))
WHERE e.RULE_ID = 'PIPE_COPY_FAILURES' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
  AND f.FQN IS NULL
ORDER BY e.RAISED_AT;

-- P168.4 R2-036 / R2-039: the pairs arm [18] sees now (first seen in the last 24h), with SUCCESSES and the V168 key,
--        and every past SEC_NEW_ADMIN_NETWORK event whose 26h login window held no success (a failures-only event
--        that said 'logged in'; resolve still-OPEN ones in Alerts as EXPECTED or NOISE).
WITH nn AS (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
                   COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
                   MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
            JOIN (
                SELECT DISTINCT GRANTEE_NAME
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
            ) A ON A.GRANTEE_NAME = L.USER_NAME
            WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
            GROUP BY 1, 2
            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
        )
SELECT nn.USER_NAME, nn.CLIENT_IP, nn.FIRST_SEEN, nn.LOGINS, nn.SUCCESSES,
       'SEC_NEW_ADMIN_NETWORK|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', '|FAILED')
                   || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN)), 'YYYY-MM-DD') AS DEDUPE_KEY_PREVIEW
FROM nn
ORDER BY nn.FIRST_SEEN DESC;
SELECT e.EVENT_ID, e.RAISED_AT, e.STATUS, e.TITLE, e.METRIC_VALUE AS ATTEMPTS,
       COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
  ON L.USER_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
 AND COALESCE(L.CLIENT_IP, '(none)') = SPLIT_PART(e.DEDUPE_KEY, '|', 3)
 AND L.EVENT_TIMESTAMP BETWEEN DATEADD('hour', -26, e.RAISED_AT) AND e.RAISED_AT
WHERE e.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
GROUP BY 1, 2, 3, 4, 5
HAVING COUNT_IF(L.IS_SUCCESS = 'YES') = 0
ORDER BY e.RAISED_AT DESC;

-- #####################################################################
--  V169 PREFLIGHT  (from outputs/gen_v169.py, cluster alerts; previews snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql)
-- #####################################################################
-- ====================================================================================================
--  V169 PREFLIGHT (read-only; run BEFORE applying V169, in a Central session). SP_ALERT_SCAN_DAILY windows and keys.
--  Each grid runs the arm's OWN text (outputs/gen_v169.py); the scan's binds are its own SETTINGS reads.
--  Changes nothing.
-- ====================================================================================================

-- P169.1 R2-041: this month's pace and forecast, old (V163) vs new (V169). OLD_PACE_RATIO counts the partial day
--        in the numerator; NEW_PACE_RATIO is MTD through yesterday over the completed-days allowance.
WITH mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) + AI credits x (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS).
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN 0 ELSE CREDITS_BILLED END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS MTD_USD,
            -- V169 (R2-041): month-to-date over COMPLETE days only (DAY < today), the numerator of
            -- DAILY_RATE_USD. MTD_USD also holds the partial UTC row of today (the ~06:45 load sees part of
            -- it), which the completed-days pace allowance ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) never budgets.
            SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS MTD_COMPLETE_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS))
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
SELECT m.DAY_OF_MONTH, m.DAYS_IN_MONTH, ROUND(m.MTD_USD, 0) AS MTD_USD, ROUND(m.MTD_COMPLETE_USD, 0) AS MTD_COMPLETE_USD,
       ROUND(m.DAILY_RATE_USD, 0) AS DAILY_RATE_USD,
       ROUND(m.MTD_USD / NULLIF((SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 3) AS OLD_PACE_RATIO,
       ROUND(m.MTD_COMPLETE_USD / NULLIF((SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 3)
           AS NEW_PACE_RATIO,
       ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0) AS OLD_PROJECTION,
       ROUND(m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1), 0) AS NEW_PROJECTION,
       (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS MONTHLY_BUDGET_USD
FROM mtd m;

-- P169.2 R2-042 / R2-103: the contract settings and the [16] p subquery verbatim (CONSUMED, TERM_END, DAYS_LEFT,
--        EXHAUST_DATE), with WOULD_RAISE_NEW, plus every OPEN / ACK COST_CONTRACT_BREACH event and whether the new
--        arm would still raise it. Resolve the ones it would not in Alerts (and close a matching auto-declared incident).
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY LIKE 'CONTRACT%' ORDER BY KEY;
WITH p AS (
            SELECT TOTAL, CONSUMED, DAILY_BURN, TERM_END,
                   CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
                   DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
                           CURRENT_DATE()) AS EXHAUST_DATE
            FROM (
                SELECT
                    (SELECT IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,
                                COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0))
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())
                       AND DAY < COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), '9999-12-31'::DATE)) AS CONSUMED,
                    (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TERM_END,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
            )
        ),
thr AS (
    SELECT COALESCE(MAX(IFF(ENABLED, THRESHOLD_NUM, NULL)), 30) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_CONTRACT_BREACH'
)
SELECT p.TOTAL, p.CONSUMED, p.DAILY_BURN, p.TERM_END, p.DAYS_LEFT, p.EXHAUST_DATE,
       (p.TOTAL > 0 AND p.DAILY_BURN > 0 AND p.DAYS_LEFT <= t.THRESHOLD_NUM
        AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))) AS WOULD_RAISE_NEW,
       e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.TITLE
FROM p
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.RULE_ID = 'COST_CONTRACT_BREACH' AND e.STATUS IN ('OPEN', 'ACK');

-- P169.3 R2-044: databases with more than one DATABASE_ID on a day of the last 3 (a drop / re-create), and the
--        growth the old (by name) and new (live id) series compute for them.
WITH g_old AS (
            SELECT DATABASE_NAME, USAGE_DATE,
                   AVERAGE_DATABASE_BYTES / POWER(1024, 3) AS CUR_GB,
                   LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE)
                       / POWER(1024, 3) AS PREV_GB,
                   (AVERAGE_DATABASE_BYTES
                    - LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE))
                       / POWER(1024, 3) AS GROWTH_GB
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE DESC) = 1
        ),
g_new AS (
            -- V169 (R2-044): one series per LIVE database id. A dropped or re-created predecessor keeps
            -- reporting rows (DELETED set) under the same DATABASE_NAME while its Time Travel and Fail-safe
            -- bytes remain, and a by-name window paired the two ids on the same day arbitrarily. A re-created
            -- database has no PREV on its first day and does not raise.
            SELECT DATABASE_NAME, USAGE_DATE,
                   AVERAGE_DATABASE_BYTES / POWER(1024, 3) AS CUR_GB,
                   LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE)
                       / POWER(1024, 3) AS PREV_GB,
                   (AVERAGE_DATABASE_BYTES
                    - LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE))
                       / POWER(1024, 3) AS GROWTH_GB
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
              AND DELETED IS NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE DESC) = 1
        ),
multi AS (
    SELECT DATABASE_NAME, USAGE_DATE, COUNT(DISTINCT DATABASE_ID) AS IDS,
           COUNT(DISTINCT IFF(DELETED IS NULL, DATABASE_ID, NULL)) AS LIVE_IDS
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(DISTINCT DATABASE_ID) > 1
)
SELECT m.DATABASE_NAME, m.USAGE_DATE, m.IDS, m.LIVE_IDS,
       ROUND(o.GROWTH_GB, 1) AS OLD_GROWTH_GB, ROUND(n.GROWTH_GB, 1) AS NEW_GROWTH_GB
FROM multi m
LEFT JOIN g_old o ON o.DATABASE_NAME = m.DATABASE_NAME
LEFT JOIN g_new n ON n.DATABASE_NAME = m.DATABASE_NAME
ORDER BY m.USAGE_DATE DESC, m.DATABASE_NAME;

-- P169.4 R2-020 / R2-043: the key the next scan writes (newest error-cycle date), the newest load's HOUR and whether
--        that key exists. FOLDS_INTO_OLDER_EVENT = the key was raised BEFORE the newest cycle loaded: after V169
--        that cycle is not paged (V169 FIRST RUN (a), once at the transition; a NEWEST_LOAD_HOUR after the ~07:00
--        scan makes it likely around the apply). Then the DQ_RECON_ERROR events of the last 90 days in raise order
--        (a next-day twin of the same TITLE is a duplicate page; close a still-OPEN / ACK older twin as SUPERSEDED
--        with the OPTIONAL owner repair R169.1 -- the Alerts RESOLVE radios cannot set that machine-close kind).
WITH n AS (
    SELECT MAX(LATEST_LOAD) AS NEWEST_LOAD FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
)
SELECT n.NEWEST_LOAD, HOUR(n.NEWEST_LOAD) AS NEWEST_LOAD_HOUR,
       'DQ_RECON_ERROR|' || TO_VARCHAR(COALESCE(TO_DATE(n.NEWEST_LOAD), CURRENT_DATE())) AS NEXT_KEY,
       e.STATUS AS EXISTING_STATUS, e.RAISED_AT AS EXISTING_RAISED_AT,
       e.RAISED_AT < n.NEWEST_LOAD AS FOLDS_INTO_OLDER_EVENT
FROM n
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = 'DQ_RECON_ERROR|' || TO_VARCHAR(COALESCE(TO_DATE(n.NEWEST_LOAD), CURRENT_DATE()));
SELECT EVENT_ID, DEDUPE_KEY, RAISED_AT, STATUS, RESOLUTION_KIND, TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'DQ_RECON_ERROR' AND RAISED_AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())
ORDER BY RAISED_AT;

-- P169.5 R2-047: the last 14 complete Central days of TRUE egress (the arm's own xfer CTE, 30-day read) against the
--        rule threshold, the largest destination per day, and whether the next-morning event exists.
WITH clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        xfer AS (
            -- V169 (R2-047): one row per Central day and destination. TRUE egress only -- the predicate of the
            -- Security > Egress drill (security_sql.egress_baseline): a same-region internal transfer moves no
            -- data out of the account.
            SELECT CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY,
                   COALESCE(TARGET_REGION, '(same region)') AS DEST,
                   SUM(BYTES_TRANSFERRED) AS BYTES
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
              AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)
            GROUP BY 1, 2
        ),
thr AS (
    SELECT COALESCE(MAX(IFF(ENABLED, THRESHOLD_NUM, NULL)), 100) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_EGRESS_SPIKE'
)
SELECT x.DAY, ROUND(SUM(x.BYTES) / POWER(1024, 3), 1) AS GB, MAX_BY(x.DEST, x.BYTES) AS TOP_REGION,
       ROUND(SUM(x.BYTES) / POWER(1024, 3), 1) >= MAX(t.THRESHOLD_NUM) AS OVER_THRESHOLD,
       MAX(IFF(e.EVENT_ID IS NULL, 0, 1)) = 1 AS EVENT_ON_NEXT_DAY
FROM xfer x
CROSS JOIN clk k
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = 'COST_EGRESS_SPIKE|' || TO_VARCHAR(DATEADD('day', 1, x.DAY))
WHERE x.DAY < k.TODAY AND x.DAY >= DATEADD('day', -14, k.TODAY)
GROUP BY x.DAY
ORDER BY x.DAY DESC;

-- P169.6 R1-071: the warehouses [24] raises for the FIRST time (a NULL snapshot timer = never suspends), with the
--        monthly USD and the rule threshold: the arm's own CTE chain.
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        win AS (
            SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
                   COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
            FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
            CROSS JOIN clk
            WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
              AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
        ),
        cov AS (
            SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win
        ),
        idle AS (
            SELECT WAREHOUSE_NAME,
                   SUM(BILLED_HOURS) AS METERED_HOURS,
                   GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
                   SUM(CREDITS_TOTAL) AS TOTAL_CREDITS,
                   SUM(IDLE_CR) AS IDLE_CREDITS
            FROM win
            GROUP BY WAREHOUSE_NAME
            HAVING SUM(CREDITS_TOTAL) > 0
        ),
        scored AS (
            SELECT i.WAREHOUSE_NAME, i.TOTAL_CREDITS, i.IDLE_CREDITS, v.COVERED_DAYS,
                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,
                   GREATEST(i.IDLE_CREDITS
                            - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0)
                              * COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS
            FROM idle i
            CROSS JOIN cov v
        ),
        newest AS (
            SELECT MAX(SNAPSHOT_AT) AS BATCH_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())
        ),
        cur AS (
            SELECT s.WAREHOUSE_NAME, s.AUTO_SUSPEND, s.SNAPSHOT_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT s
            JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1
        ),
        opp AS (
            SELECT s.WAREHOUSE_NAME, s.TOTAL_CREDITS, s.IDLE_CREDITS, s.COVERED_DAYS, s.IDLE_PCT,
                   s.RECOVERABLE_CREDITS, COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
                   GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
            FROM scored s
            JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
            WHERE s.COVERED_DAYS >= 7
              AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
              AND (COALESCE(w.AUTO_SUSPEND, 0) <= 0 OR w.AUTO_SUSPEND > 60)
        )
SELECT o.WAREHOUSE_NAME, o.MONTHLY_USD, o.IDLE_PCT, o.COVERED_DAYS, o.TARGET_SEC, c.THRESHOLD_NUM,
       o.MONTHLY_USD >= c.THRESHOLD_NUM AS WOULD_RAISE
FROM opp o
JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(o.WAREHOUSE_NAME)
JOIN cfg c ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY'
WHERE w.AUTO_SUSPEND IS NULL
ORDER BY o.MONTHLY_USD DESC;

-- P169.7 R1-233: SEC_TRUST_REGRESSION events raised on no rise (METRIC_VALUE < 1) and the rule threshold. Close any
--        as EXPECTED in Alerts ('threshold-0 artifact'); reset a THRESHOLD_NUM <= 0 to 1 (the floor makes it cosmetic).
SELECT (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND METRIC_VALUE < 1) AS NO_RISE_EVENTS,
       (SELECT MAX(THRESHOLD_NUM) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION') AS THRESHOLD_NUM;

-- #####################################################################
--  V170 PREFLIGHT  (from outputs/gen_v170.py, cluster incidents; previews snowflake/migrations/V170__incident_declare_actor_and_proposals.sql)
-- #####################################################################
-- PREFLIGHT V170 (read-only after the session pin; run BEFORE applying V170, any role that reads
-- DBA_MAINT_DB.OVERWATCH). Every statement after the pin is a SELECT. P170.1 is the V170 INCIDENT_PROPOSALS body
-- verbatim, run as a query, so what you preview is what the view will return after the apply; compare it with the
-- live view in P170.2. The pin comes first because ALERT_EVENTS.RAISED_AT is Central wall-clock NTZ and both grids
-- keep alerts with RAISED_AT >= CURRENT_TIMESTAMP() - 2 days: in a UTC worksheet that 48h window sits about 5h
-- off from the one the app (account timezone Central) sees.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P170.1  open proposals as V170 will classify and score them (ENTITY_KIND / CONFIDENCE / EVIDENCE).
WITH raw_alerts AS (
    SELECT e.EVENT_ID,
           e.RULE_ID,
           e.COMPANY,
           e.SEVERITY,
           e.TITLE,
           e.RAISED_AT,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2) AS ENTITY_RAW
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND NOT EXISTS (
          SELECT 1
          FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID
      )
),
classified AS (
    SELECT r.*,
           CASE
             WHEN r.RULE_ID IN ('COST_WH_DAILY_CREDITS', 'PERF_QUEUED_MINUTES',
                                'PERF_SPILL_GB', 'COST_CLOUD_SVC_RATIO',
                                'WH_CHANGE_REGRESSION', 'COST_IDLE_OPPORTUNITY',
                                'COST_SLEEP_POLLING') THEN 'WAREHOUSE'
             WHEN r.RULE_ID IN ('PIPE_TASK_FAILURES', 'PIPE_COPY_FAILURES',
                                'PERF_CHANGE_REGRESSION', 'PIPE_DT_FAILURES',
                                'PIPE_VOLUME_DROP', 'DQ_BREACH', 'DQ_SCHEMA_DRIFT') THEN 'OBJECT'
             WHEN r.RULE_ID = 'COST_STORAGE_SURGE' THEN 'DATABASE'
             WHEN r.RULE_ID = 'COST_SERVERLESS_CREEP' THEN 'SERVICE'
             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK', 'SEC_FAILED_LOGINS',
                                'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT',
                                'COST_AI_USER_RUNAWAY') THEN 'USER'
             WHEN r.RULE_ID = 'COST_DEPT_BUDGET_PACE' THEN 'DEPARTMENT'
             WHEN COALESCE(r.ENTITY_RAW, '') = ''
               OR TRY_TO_DATE(r.ENTITY_RAW) IS NOT NULL
               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH', 'EXH', 'ALL')
               THEN 'ACCOUNT'
             ELSE 'SCOPE'
           END AS ENTITY_KIND
    FROM raw_alerts r
),
open_alerts AS (
    SELECT c.*,
           IFF(c.ENTITY_KIND = 'ACCOUNT', 'ACCOUNT', c.ENTITY_RAW) AS ENTITY_NAME
    FROM classified c
),
proposal_groups AS (
    SELECT FAMILY,
           COMPANY,
           ENTITY_KIND,
           ENTITY_NAME,
           MAX_BY(TITLE, RAISED_AT) AS SUGGESTED_TITLE,
           DECODE(MAX(CASE UPPER(SEVERITY)
                        WHEN 'CRITICAL' THEN 3 WHEN 'HIGH' THEN 2 ELSE 1 END),
                  3, 'CRITICAL', 2, 'HIGH', 'MEDIUM') AS SEVERITY,
           MIN(RAISED_AT) AS FIRST_TS,
           MAX(RAISED_AT) AS LAST_TS,
           COUNT(*) AS ALERTS
    FROM open_alerts
    GROUP BY FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME
),
warehouse_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(w.CHANGE_ID) AS MATCHED_WH_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY w
      ON g.ENTITY_KIND = 'WAREHOUSE'
     AND UPPER(w.WAREHOUSE_NAME) = UPPER(g.ENTITY_NAME)
     AND w.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
object_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(o.CHANGE_ID) AS MATCHED_OBJECT_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY o
      ON ((g.ENTITY_KIND = 'OBJECT' AND UPPER(o.OBJECT_NAME) = UPPER(g.ENTITY_NAME))
          OR (g.ENTITY_KIND = 'DATABASE'
              AND UPPER(o.DATABASE_NAME) = UPPER(g.ENTITY_NAME)))
     AND o.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
task_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COALESCE(SUM(t.FAILED), 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY t
      ON g.ENTITY_KIND = 'OBJECT'
     AND UPPER(COALESCE(t.DATABASE_NAME, '') || '.' ||
               COALESCE(t.SCHEMA_NAME, '') || '.' || t.TASK_NAME) = UPPER(g.ENTITY_NAME)
     AND t.DAY BETWEEN DATEADD('day', -1, DATE(g.FIRST_TS))
                   AND DATEADD('day', 1, DATE(g.LAST_TS))
    GROUP BY 1, 2, 3, 4
),
evidence AS (
    SELECT g.*,
           COALESCE(w.MATCHED_WH_CHANGES, 0) AS MATCHED_WH_CHANGES,
           COALESCE(o.MATCHED_OBJECT_CHANGES, 0) AS MATCHED_OBJECT_CHANGES,
           COALESCE(t.MATCHED_TASK_FAILURES, 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN warehouse_evidence w
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN object_evidence o
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN task_evidence t
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
)
SELECT FAMILY || '|' || COMPANY || '|' || ENTITY_KIND || '|' || ENTITY_NAME AS PROPOSAL_KEY,
       SUGGESTED_TITLE,
       SEVERITY,
       COMPANY,
       ENTITY_KIND,
       ENTITY_NAME,
       FIRST_TS,
       LAST_TS,
       ALERTS,
       MATCHED_WH_CHANGES,
       MATCHED_WH_CHANGES AS NEARBY_WH_CHANGES,
       MATCHED_OBJECT_CHANGES,
       MATCHED_TASK_FAILURES,
       CASE
         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES
              + IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES) > 0 THEN 'HIGH'
         WHEN ENTITY_KIND <> 'ACCOUNT' AND ALERTS >= 2 THEN 'MEDIUM'
         ELSE 'LOW'
       END AS CONFIDENCE,
       'alerts=' || ALERTS ||
       '; matched warehouse changes=' || MATCHED_WH_CHANGES ||
       '; matched object changes=' || MATCHED_OBJECT_CHANGES ||
       IFF(FAMILY = 'PIPE_TASK_FAILURES', '; task failures (alert source, not corroboration)=',
           '; matched task failures=') || MATCHED_TASK_FAILURES AS EVIDENCE
FROM evidence
ORDER BY ENTITY_KIND, CONFIDENCE, PROPOSAL_KEY;

-- P170.2  the same proposals as the live (V072) view returns them today; EXH / ALL rows read ENTITY_KIND SCOPE.
SELECT PROPOSAL_KEY, ENTITY_KIND, ENTITY_NAME, CONFIDENCE, ALERTS, EVIDENCE
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS
ORDER BY ENTITY_KIND, CONFIDENCE, PROPOSAL_KEY;

-- P170.3  SP_INCIDENT_DECLARE overloads before the apply: expect ONE row (the V131 4-arg); V170 adds the 5-arg.
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, CREATED, LAST_ALTERED
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_INCIDENT_DECLARE'
ORDER BY ARGUMENT_SIGNATURE;

-- #####################################################################
--  V171 PREFLIGHT  (from outputs/gen_v171.py, cluster ops-dq; previews snowflake/migrations/V171__ops_selfwatch_digest_refgaps_seed.sql)
-- #####################################################################
-- PREFLIGHT -- V171 section (READ-ONLY). Run before applying V171; changes nothing.
-- P171.1 the digest facts today (V165: the board's today-INCLUSIVE 7-day KPI rows) beside V171's (the 7 complete
--        days ending yesterday). expect V171_SPEND_USD a little below V165_SPEND_USD (today's partial day left
--        out) and never about 3x it (that would be every DAILY_SPEND window counted). Central days, any session.
WITH old_kpi AS (
        SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)) AS V165_SPEND_USD,
               MAX(IFF(METRIC = 'CREDITS', VALUE, NULL)) AS V165_CREDITS,
               MAX(IFF(METRIC = 'QUERIES', VALUE, NULL)) AS V165_QUERIES,
               MAX(IFF(METRIC = 'FAILED_QUERIES', VALUE, NULL)) AS V165_FAILED_Q,
               MAX(IFF(METRIC = 'QUEUED_MINUTES', VALUE, NULL)) AS V165_QUEUED_MIN,
               MAX(IFF(METRIC = 'SPILL_GB', VALUE, NULL)) AS V165_SPILL_GB,
               MAX(IFF(METRIC = 'TASK_RUNS', VALUE, NULL)) AS V165_TASK_RUNS,
               MAX(IFF(METRIC = 'TASK_FAILURES', VALUE, NULL)) AS V165_TASK_FAIL
        FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI'
    ),
    new_1 AS (
        SELECT SUM(VALUE_USD) AS V171_SPEND_USD, SUM(VALUE) AS V171_CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE PANEL = 'DAILY_SPEND' AND METRIC = 'CREDITS' AND COMPANY = 'ALL' AND WINDOW_DAYS = 7
      AND PERIOD_START >= DATEADD('day', -7, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AND PERIOD_START < CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE
    ),
    new_2 AS (
        SELECT SUM(QUERY_COUNT) AS V171_QUERIES, SUM(FAILED_COUNT) AS V171_FAILED_Q, ROUND(SUM(QUEUED_SEC_SUM) / 60, 1) AS V171_QUEUED_MIN, ROUND(SUM(SPILL_REMOTE_GB), 2) AS V171_SPILL_GB
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    WHERE DAY >= DATEADD('day', -7, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AND DAY < CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE
    ),
    new_3 AS (
        SELECT SUM(RUNS) AS V171_TASK_RUNS, SUM(FAILED) AS V171_TASK_FAIL
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
    WHERE DAY >= DATEADD('day', -7, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AND DAY < CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE
    )
SELECT *
  FROM old_kpi CROSS JOIN new_1 CROSS JOIN new_2 CROSS JOIN new_3;

-- P171.2 the model the scheduled digest will run (V171 reads CORTEX_MODEL like the app). expect RUNS_AS equal to
--        STORED_VALUE; when they differ, the stored value is blank, padded, mixed-case or invalid (fix it on
--        Admin > Settings or leave it: V171 runs RUNS_AS either way).
SELECT MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)) AS STORED_VALUE,
       LENGTH(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL))) AS LEN,
       (SELECT IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')
          FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)) AS RUNS_AS
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

-- P171.3 the 30-day history V171 R2-019 / R2-104 changes. ref_gap_scan_failed = the whole scan threw (one bad
--        check, or a coercion like 'Numeric value ... is not recognized', silenced every check). expect none.
SELECT ERROR_TYPE, COUNT(*) AS N, MAX(LOGGED_AT) AS LAST_AT, MAX(LEFT(ERROR_MESSAGE, 200)) AS SAMPLE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE ERROR_TYPE IN ('ref_gap_scan_failed', 'ref_gap_check_failed', 'render_check_unavailable', 'digest_ai_failed')
   AND LOGGED_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())
 GROUP BY ERROR_TYPE
 ORDER BY N DESC;

-- P171.4 CREDIT_PRICE_OVERRIDE rows before the seed. expect N 0 (the seed inserts FALSE) or 1 (kept as is).
SELECT COUNT(*) AS N, LISTAGG(VALUE, ', ') AS VALUES_SEEN
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY = 'CREDIT_PRICE_OVERRIDE';

-- P171.5 the K3 OPS_SLOW_RENDER title (the proc's exact TITLE text) on FLOAT p95 literals, the type the proc's
--        ROUND(APPROX_PERCENTILE(..)) gives. expect every RESULT OK: the CASE compiles and reads like the app
--        (Hr/Min/Sec) before the Monday 05:30 canary run uses it.
SELECT 'P171.5 title p95 ' || r.LBL AS CHECK_NAME, r.WANT,
               r.PAGE || ' p95 first paint '
               || CASE WHEN (r.P95_S::NUMBER(18, 1)) IS NULL THEN '?'
                       WHEN ROUND((r.P95_S::NUMBER(18, 1)) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (r.P95_S::NUMBER(18, 1)) < 1 THEN ROUND((r.P95_S::NUMBER(18, 1)) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (r.P95_S::NUMBER(18, 1)) < 10 THEN TO_VARCHAR(ROUND((r.P95_S::NUMBER(18, 1)), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' (7d, n=' || r.N || ')' AS GOT,
       IFF(GOT = r.WANT, 'OK', 'FAIL: the title renders differently') AS RESULT
FROM (SELECT column1 AS LBL, column2::FLOAT AS P95_S, 'Overview' AS PAGE, 25 AS N, column3 AS WANT
      FROM VALUES
         ('8.1', 8.1, 'Overview p95 first paint 8.1s (7d, n=25)'),
         ('9.9', 9.9, 'Overview p95 first paint 9.9s (7d, n=25)'),
         ('12.3', 12.3, 'Overview p95 first paint 12s (7d, n=25)'),
         ('45.0', 45.0, 'Overview p95 first paint 45s (7d, n=25)'),
         ('59.4', 59.4, 'Overview p95 first paint 59s (7d, n=25)'),
         ('59.5', 59.5, 'Overview p95 first paint 1m (7d, n=25)'),
         ('94.5', 94.5, 'Overview p95 first paint 1m 34s (7d, n=25)'),
         ('95.5', 95.5, 'Overview p95 first paint 1m 36s (7d, n=25)'),
         ('119.6', 119.6, 'Overview p95 first paint 2m (7d, n=25)'),
         ('3599.6', 3599.6, 'Overview p95 first paint 1h (7d, n=25)'),
         ('3690.0', 3690.0, 'Overview p95 first paint 1h 1m (7d, n=25)'),
         ('8700.0', 8700.0, 'Overview p95 first paint 2h 25m (7d, n=25)'),
         ('0.4', 0.4, 'Overview p95 first paint 400ms (7d, n=25)'),
         ('0.0', 0.0, 'Overview p95 first paint 0s (7d, n=25)'),
         ('NULL', NULL, 'Overview p95 first paint ? (7d, n=25)')) r
ORDER BY 1;

-- #####################################################################
--  V172 PREFLIGHT  (from outputs/gen_v172.py, cluster detection; previews snowflake/migrations/V172__detection_scans_company_and_accuracy.sql)
-- #####################################################################
-- PREFLIGHT V172 (P172.1-P172.6) -- READ-ONLY preview of V172. Changes nothing. Run any time before the apply.

-- P172.1 the R1-124 HD template (the exact CASE text both scans use) on fixed-point literals. Every RESULT OK
--        proves the template compiles and renders like the app's humanize_duration before the 06:40 scan runs it.
SELECT 'P172.1 HD(' || t.LBL || ')' AS CHECK_NAME, t.WANT,
       CASE WHEN (t.S) IS NULL THEN '?'
            WHEN ROUND((t.S) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
            WHEN (t.S) < 1 THEN ROUND((t.S) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
            WHEN (t.S) < 10 THEN TO_VARCHAR(ROUND((t.S), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
            ELSE TRIM(IFF(ROUND((t.S), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((t.S), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                      || IFF(MOD(FLOOR(ROUND((t.S), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((t.S), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                      || IFF(ROUND((t.S), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((t.S), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((t.S), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
       END AS GOT,
       IFF(GOT = t.WANT, 'OK', 'FAIL: the template renders differently') AS RESULT
FROM (SELECT column1 AS LBL, column2::NUMBER(18, 2) AS S, column3 AS WANT
      FROM VALUES
         ('1800', 1800, '30m'),
         ('8700', 8700, '2h 25m'),
         ('95.5', 95.5, '1m 36s'),
         ('94.5', 94.5, '1m 34s'),
         ('0.5', 0.5, '500ms'),
         ('5.04', 5.04, '5.0s'),
         ('9.96', 9.96, '10.0s'),
         ('45', 45, '45s'),
         ('3630', 3630, '1h'),
         ('3690', 3690, '1h 1m'),
         ('86400', 86400, '24h'),
         ('0', 0, '0s'),
         ('NULL', NULL, '?')) t
ORDER BY 1;

-- P172.2 (R1) OBJECT_CHANGE_REGISTRY rows whose COMPANY the apply re-stamps (old -> new; UNKNOWN = an unmapped
--        database: map it in Spend & Attribution > Unmapped entities to keep it in a company scope).
SELECT r.COMPANY AS OLD_COMPANY, m.CO AS NEW_COMPANY, COUNT(*) AS REGISTRY_ROWS,
       COUNT_IF(CURRENT_DATE() <= r.TRACKING_UNTIL) AS STILL_TRACKING,
       LISTAGG(DISTINCT r.DATABASE_NAME, ', ') WITHIN GROUP (ORDER BY r.DATABASE_NAME) AS DATABASES
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
JOIN (
      SELECT d.DATABASE_NAME, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME) AS CO
      FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d
) m ON m.DATABASE_NAME = r.DATABASE_NAME
WHERE r.COMPANY IS DISTINCT FROM m.CO
GROUP BY 1, 2
ORDER BY 3 DESC;

-- P172.3 (R1b + R2) live events of the five rules and the company the apply gives them. INCIDENT_LINKED rows are
--        NOT re-stamped (they keep their incident's company); see R172.2 after the apply.
SELECT y.RULE_ID, y.STATUS, y.OLD_COMPANY, y.NEW_COMPANY, y.INCIDENT_LINKED, COUNT(*) AS EVENTS
FROM (
    SELECT x.RULE_ID, x.STATUS, x.COMPANY AS OLD_COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS NEW_COMPANY, x.INCIDENT_LINKED
    FROM (
        SELECT e.RULE_ID, e.STATUS, e.COMPANY, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB,
               IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                           WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID), 'YES', 'NO') AS INCIDENT_LINKED
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP',
                            'DQ_BREACH')
          AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
    ) x
) y
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3;

-- P172.4 (R3) still-tracking PROCEDURE rows whose frozen baseline the apply nulls (the suffix-collision set), with
--        the procedure names that blended in. The next 06:50 scan re-freezes them; CHANGE_AGE_DAYS > 6 = a shorter
--        baseline (the scan reaches 20 days back).
SELECT r.OBJECT_NAME, r.CHANGE_SEEN_AT, DATEDIFF('day', r.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) AS CHANGE_AGE_DAYS,
       r.BASELINE_CALLS, r.VERDICT,
       LISTAGG(DISTINCT p.PROCEDURE_CATALOG || '.' || p.PROCEDURE_SCHEMA || '.' || p.PROCEDURE_NAME, ', ')
           WITHIN GROUP (ORDER BY p.PROCEDURE_CATALOG || '.' || p.PROCEDURE_SCHEMA || '.' || p.PROCEDURE_NAME)
           AS COLLIDING_PROCEDURES
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
      JOIN SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES p
        ON ENDSWITH(UPPER(p.PROCEDURE_NAME), UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3)))
       AND UPPER(p.PROCEDURE_NAME) <> UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3))
WHERE r.OBJECT_TYPE = 'PROCEDURE'
  AND CURRENT_DATE() <= r.TRACKING_UNTIL
GROUP BY 1, 2, 3, 4, 5
ORDER BY 2;

-- P172.5 (R4) still-tracking TASK rows: the frozen attempt-based baseline vs the terminal-attempt re-freeze the apply
--        writes (credits/call rescaled by OLD_CALLS / NEW_CALLS).
SELECT r.OBJECT_NAME, r.CHANGE_SEEN_AT, r.VERDICT,
       MAX(r.BASELINE_CALLS) AS OLD_CALLS, COUNT(*) AS NEW_CALLS,
       MAX(r.BASELINE_FAILS) AS OLD_FAILS, COUNT_IF(h.STATE = 'FAILED') AS NEW_FAILS,
       MAX(r.BASELINE_CREDITS_PER_CALL) AS OLD_CR_PER_RUN,
       MAX(r.BASELINE_CREDITS_PER_CALL) * MAX(r.BASELINE_CALLS) / NULLIF(COUNT(*), 0) AS NEW_CR_PER_RUN
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
      JOIN (SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE SCHEDULED_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
              AND STATE IN ('SUCCEEDED', 'FAILED')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                       ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1) h
        ON h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
       AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
       AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NOT NULL AND r.BASELINE_CALLS > 0
  AND CURRENT_DATE() <= r.TRACKING_UNTIL
GROUP BY 1, 2, 3
ORDER BY 2;

-- P172.6 (R1-227 + CORTEX-NULLIF) the two anomaly rules' state, and the model the sweep's pre-explain will run.
SELECT 'P172.6 ' || c.RULE_ID AS CHECK_NAME, IFF(c.ENABLED, 'ENABLED', 'DISABLED: V172 stops it booking') AS RESULT,
       c.THRESHOLD_NUM, c.UPDATED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
WHERE c.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP')
UNION ALL
SELECT 'P172.6 CORTEX_MODEL runs as', IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b'), NULL, NULL
FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
      FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);

ALTER SESSION UNSET TIMEZONE;
