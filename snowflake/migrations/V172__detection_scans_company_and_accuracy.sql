-- V172__detection_scans_company_and_accuracy.sql
--
-- The detection scans classify company by the V044 rule and measure what they claim (V166-V172 wave, detection
-- cluster). Five scan procs re-derived, each from its current definer, plus bounded one-time repairs.
--
-- WHY:
--   (R2-023) SP_CHANGE_IMPACT_SCAN stamped COMPANY with a raw TRXS%/ALFA guess: an unmapped database read ALFA,
--     a COMPANY_SCOPE override was ignored, and the Trexis / UNKNOWN scopes never saw those changes or their
--     PERF_CHANGE_REGRESSION alerts (and an auto-declared incident copied the wrong company).
--   (R2-024) SP_ANOMALY_SWEEP (PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH) and SP_SCAN_SCHEMA_DRIFT made the
--     same guess; SP_ALERT_SCAN dropped it in V067 #22. They now call COMPANY_FOR_DATABASE inside the SP_ALERT_SCAN
--     b (...) wrapper.
--   (R2-021) the scan matched procedure calls by a bare 'NAME(' suffix, so RUN_SP_LOAD counted as SP_LOAD in the
--     frozen baseline, the after window and credits/call; the Operations drill was anchored in v4.271 and the scan
--     was not. Now 'CALL<name>(' or '.<name>(' over a whitespace-class strip, the drill's rule.
--   (R2-025) TASK runs counted every auto-retry attempt: 7 of 14 runs retried once read as 21 runs, 7 failures,
--     REGRESSED (CRITICAL at 50%) while every scheduled run succeeded. Runs, fails and p95 now count the terminal
--     attempt per scheduled run (the drill, V101, V126); retry compute still counts toward credits/call.
--   (R2-022) the AFTER credits/call divided attributed credits by ALL runs, including runs QUERY_ATTRIBUTION_HISTORY
--     had not reached yet (up to ~8h): a nightly job's +55% read +44% (no alert) and a fresh hourly change read a
--     false IMPROVED. Now settled (>8h) runs only, numerator and denominator over the same runs, per scheduled run.
--   (R1-124) VERDICT_DETAIL (and so ALERT_EVENTS.DETAIL and the Teams / email lines) printed raw seconds and
--     minutes/day ('p95 1800.0s->2400.0s'); now Hr/Min/Sec ('p95 30m -> 40m', 'queue 2h 25m -> 3h 20m/day').
--   (R1-227) SP_SCAN_CLOUD_SVC_ANOMALY's disabled-rule guard tested a COALESCEd threshold for NULL and never fired;
--     a disabled COST_CLOUD_SVC_ANOMALY still booked (at 3.5) and was delivered. Rider: a disabled
--     COST_ANOMALY_SWEEP stops booking too (the other sweep arms still run).
--   (R2-095) the COST_ORG_ACCOUNT_CREEP DETAIL pointed at a retired 'Admin > Org spend'.
--   (CORTEX-NULLIF) the sweep's Cortex pre-explain read CORTEX_MODEL raw: a blank, padded, mixed-case or invalid
--     value reached COMPLETE and the AI explanation failed. Now normalized like app.core.ai.normalize_model.
--
--   ~ SP_CHANGE_IMPACT_SCAN      from V140 (R2-023, R2-021, R2-025, R2-022, R1-124)
--   ~ SP_WAREHOUSE_CHANGE_SCAN   from V109 (R1-124)
--   ~ SP_SCAN_SCHEMA_DRIFT       from V133 (R2-024)
--   ~ SP_SCAN_CLOUD_SVC_ANOMALY  from V150 (R1-227)
--   ~ SP_ANOMALY_SWEEP           from V150 (R2-024, R2-095, R1-227 rider, CORTEX-NULLIF); RETURN stays 'v3'
--   + repairs R1-R4 below (OVERWATCH tables only; reads: one ACCOUNT_USAGE.PROCEDURES join, 30 days of
--     TASK_HISTORY).
--
-- COST: the daily change-impact scan's two TASK count legs window 21 days of TASK_HISTORY (QUALIFY) instead of a
-- raw join; the anchored match runs REGEXP_REPLACE only on rows that pass the unchanged ILIKE pre-filter. The
-- repairs read ACCOUNT_USAGE.PROCEDURES once and 30 days of TASK_HISTORY once; seconds.
-- FIRST RUN: nothing runs at apply time. The next TASK_WAREHOUSE_CHANGE_SCAN (06:40 Central) and
-- TASK_CHANGE_IMPACT_SCAN (06:50) rewrite tracking rows' VERDICT_DETAIL and re-freeze the nulled PROCEDURE
-- baselines; the next TASK_ANOMALY_SWEEP books with the new company. Verdicts on collision- or retry-affected
-- objects change on that run (intended). Already-raised alerts are not re-raised (dedupe keys unchanged).
-- DELIVERY: re-raising is not delivery. SP_NOTIFY_WEBHOOK (V164) sends an OPEN event to a route only when the
-- route's COMPANY_FILTER is ALL or the event's COMPANY, once per (EVENT_ID, ROUTE_ID) in ALERT_DELIVERIES, and
-- V034 set every existing route to 'ALFA'. (a) On a database with no COMPANY_SCOPE row that is not TRXS_ / ALFA% /
-- ADMIN, PERF_CHANGE_REGRESSION, PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT alerts are
-- now UNKNOWN and stop posting to an ALFA-only route (the first three seed HIGH, PIPE_DT_FAILURES is CRITICAL at
-- 5+ failures), with no undelivered_expired row either (the watchdog reads the same filter). To keep them
-- posting, map the database (Cost Intelligence > Spend & Attribution > Unmapped entities) or add an ALL or
-- UNKNOWN route. (b) An OPEN event the R1b / R2 re-stamps move to a company another enabled route carries becomes
-- eligible there: the next TASK_ALERT_NOTIFY run sends it once if it is still inside the send window (24h; 7d
-- for CRITICAL). An older one raised within 7 days is not sent there: V164's watchdog logs an undelivered_expired
-- row for that route instead, then another every 24h (it skips a pair logged in the last 24h) while the event
-- stays OPEN and undelivered there, until it is 7 days old.
-- ROLLBACK (order matters): 1. Re-run the base CREATEs (V140 SP_CHANGE_IMPACT_SCAN, V109
-- SP_WAREHOUSE_CHANGE_SCAN, V133 SP_SCAN_SCHEMA_DRIFT, V150 SP_SCAN_CLOUD_SVC_ANOMALY + SP_ANOMALY_SWEEP). The
-- re-stamped COMPANY values stay (they are the corrected values). 2. Right after V140's CREATE, before the next
-- change-impact scan (06:50 Central, or Operations' Run change-impact scan now), null the still-tracking
-- TASK and PROCEDURE baselines:
--     UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
--        SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
--            BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
--      WHERE OBJECT_TYPE IN ('TASK', 'PROCEDURE') AND CURRENT_DATE() <= TRACKING_UNTIL AND NOT ALERTED;
-- Why: R4 and the V172 scan froze them per scheduled run (terminal attempt) and by the anchored CALL match, but
-- V140's AFTER legs count every attempt and the bare suffix match, and V140 re-freezes only a NULL baseline.
-- Kept, a task with 7 of 14 runs retried once on both sides reads 14 runs / 0 failed before vs 21 / 7 after:
-- REGRESSED, a false PERF_CHANGE_REGRESSION page; a rescaled credits/call reads a false IMPROVED. Nulled, V140's
-- next scan re-freezes them on its own basis (over its 20-day reach: a change older than 6 days gets a shorter
-- baseline). An ALERTED row keeps the baseline its alert was raised on (V140 alerts a row once). Check any
-- PERF_CHANGE_REGRESSION raised between steps 1 and 2 before acting on it.
-- Apply AFTER V171. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20172, 'V172 requires V171 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 171) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_CHANGE_IMPACT_SCAN  (from V140; COMPANY via COMPANY_FOR_DATABASE (R2-023), anchored CALL match (R2-021), TASK runs collapse retries (R2-025), settled per-run AFTER credits/call (R2-022), p95 in Hr/Min/Sec (R1-124), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    pct FLOAT;                 -- regression threshold, % increase (ALERT_CONFIG)
    min_calls FLOAT DEFAULT 5; -- both windows need this many runs for a verdict
    trk_lo TIMESTAMP_NTZ;      -- v2: oldest still-tracking change (prunes the scans)
    emsg VARCHAR;
BEGIN
    -- v2 (2026-07-10 tuning): the after-window joins used a blanket -18d
    -- bound even when only fresh changes were tracking. Bound them to the
    -- oldest ACTIVE row instead — nothing tracking means near-zero scan.
    SELECT COALESCE(MIN(CHANGE_SEEN_AT), CURRENT_TIMESTAMP()) INTO :trk_lo
    FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
    WHERE CURRENT_DATE() <= TRACKING_UNTIL;
    SELECT COALESCE(MAX(THRESHOLD_NUM), 50) INTO :pct
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PERF_CHANGE_REGRESSION';

    -- 1a) Register changed/replaced procedures. CREATE OR REPLACE resets
    --     CREATED = LAST_ALTERED, so replaced and brand-new procs both land
    --     here; never-called objects finalize as NO_BASELINE, never alerts.
    --     Overloads share one row (call matching is by name).
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
    USING (
        SELECT g.OBJECT_TYPE, g.DATABASE_NAME, g.SCHEMA_NAME, g.OBJECT_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): the V044 classification (COMPANY_SCOPE row, then TRXS_, then ALFA%/ADMIN, else UNKNOWN), not a raw TRXS%/ALFA guess; the UDF on a plain column outside the GROUP BY (V030 shape)
               g.CHANGE_SEEN_AT
        FROM (
        SELECT 'PROCEDURE' AS OBJECT_TYPE,
               PROCEDURE_CATALOG AS DATABASE_NAME,
               PROCEDURE_SCHEMA AS SCHEMA_NAME,
               PROCEDURE_CATALOG || '.' || PROCEDURE_SCHEMA || '.' || PROCEDURE_NAME AS OBJECT_NAME,
               MAX(LAST_ALTERED) AS CHANGE_SEEN_AT
        FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
        WHERE DELETED IS NULL
          AND PROCEDURE_CATALOG IS NOT NULL
          AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own procs are self-monitored (freshness + per-loader error log), not change-impact-tracked
          AND LAST_ALTERED >= DATEADD('day', -3, CURRENT_TIMESTAMP())
        GROUP BY 1, 2, 3, 4
        ) g
    ) s
    ON t.OBJECT_TYPE = s.OBJECT_TYPE AND t.OBJECT_NAME = s.OBJECT_NAME
       AND t.CHANGE_SEEN_AT = s.CHANGE_SEEN_AT
    WHEN NOT MATCHED THEN INSERT
        (OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, COMPANY, CHANGE_SEEN_AT, TRACKING_UNTIL)
        VALUES (s.OBJECT_TYPE, s.DATABASE_NAME, s.SCHEMA_NAME, s.OBJECT_NAME, s.COMPANY,
                s.CHANGE_SEEN_AT, DATEADD('day', 14, s.CHANGE_SEEN_AT)::DATE);

    -- 1b) Register task definition changes. TASK_VERSIONS keeps every graph
    --     version; only genuine definition/schedule/warehouse diffs register,
    --     so suspend/resume churn is ignored. Guarded: an account without
    --     TASK_VERSIONS still tracks procedures.
    BEGIN
        MERGE INTO DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
        USING (
            SELECT 'TASK' AS OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME,
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): the V044 classification, not a raw TRXS%/ALFA guess; a plain column, no aggregate
                   CHANGE_SEEN_AT
            FROM (
                SELECT DATABASE_NAME, SCHEMA_NAME,
                       DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS OBJECT_NAME,
                       GRAPH_VERSION_CREATED_ON AS CHANGE_SEEN_AT,
                       DEFINITION, SCHEDULE, WAREHOUSE_NAME,
                       LAG(DEFINITION) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                             ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_DEFINITION,
                       LAG(SCHEDULE) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                           ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_SCHEDULE,
                       LAG(WAREHOUSE_NAME) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                                 ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_WAREHOUSE
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
            )
            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())
              AND DATABASE_NAME <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own tasks are self-monitored, not change-impact-tracked
              AND PREV_DEFINITION IS NOT NULL
              AND (NOT EQUAL_NULL(DEFINITION, PREV_DEFINITION)
                   OR NOT EQUAL_NULL(SCHEDULE, PREV_SCHEDULE)
                   OR NOT EQUAL_NULL(WAREHOUSE_NAME, PREV_WAREHOUSE))
        ) s
        ON t.OBJECT_TYPE = s.OBJECT_TYPE AND t.OBJECT_NAME = s.OBJECT_NAME
           AND t.CHANGE_SEEN_AT = s.CHANGE_SEEN_AT
        WHEN NOT MATCHED THEN INSERT
            (OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, COMPANY, CHANGE_SEEN_AT, TRACKING_UNTIL)
            VALUES (s.OBJECT_TYPE, s.DATABASE_NAME, s.SCHEMA_NAME, s.OBJECT_NAME, s.COMPANY,
                    s.CHANGE_SEEN_AT, DATEADD('day', 14, s.CHANGE_SEEN_AT)::DATE);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ChangeImpactScan', 'task_versions_unavailable', :emsg,
                   'TASK registration skipped; procedures still tracked', CURRENT_ROLE();
    END;

    -- 2) Best-effort DDL evidence: who ran the CREATE/ALTER near the change.
    --    Multi-match picks one arbitrarily — evidence, not lineage.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET CHANGED_BY = d.USER_NAME,
           CHANGE_DDL = LEFT(d.QUERY_TEXT, 1000)
      FROM (
          SELECT USER_NAME, QUERY_TEXT, START_TIME
          FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
          WHERE START_TIME >= DATEADD('day', -4, CURRENT_TIMESTAMP())
            AND EXECUTION_STATUS = 'SUCCESS'
            AND (QUERY_TYPE ILIKE 'CREATE%' OR QUERY_TYPE ILIKE 'ALTER%')
      ) d
     WHERE t.CHANGE_DDL IS NULL
       AND t.CHANGE_SEEN_AT >= DATEADD('day', -4, CURRENT_TIMESTAMP())
       AND d.START_TIME BETWEEN DATEADD('hour', -3, t.CHANGE_SEEN_AT)
                            AND DATEADD('hour', 3, t.CHANGE_SEEN_AT)
       AND POSITION(SPLIT_PART(t.OBJECT_NAME, '.', 3) IN UPPER(d.QUERY_TEXT)) > 0;

    -- 3) Freeze pre-change baselines (14 days before the change, once).
    --    Procedure calls are matched by 'CALLNAME(' or '.NAME(' in whitespace-stripped CALL text
    --    (V172: was a bare 'NAME(' suffix match, so RUN_NAME co-matched NAME); a
    --    same-named proc in another schema would co-match — acceptable noise,
    --    flagged here rather than hidden.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
           BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 MEDIAN(q.TOTAL_ELAPSED_TIME) AS MED_MS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND q.START_TIME < r.CHANGE_SEEN_AT
           AND q.QUERY_TYPE = 'CALL'
           AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
           AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                         REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                            REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
          WHERE r.OBJECT_TYPE = 'PROCEDURE' AND r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
           BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(h.STATE = 'FAILED') AS FAILS,
                 MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
                 APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
            ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
           AND h.STATE IN ('SUCCEEDED', 'FAILED')
           AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
          WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- Idle-before objects: freeze an explicit zero baseline (-> NO_BASELINE).
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET BASELINE_FROM = DATEADD('day', -14, CHANGE_SEEN_AT),
           BASELINE_CALLS = 0, BASELINE_FAILS = 0
     WHERE BASELINE_FROM IS NULL;

    -- 4) Refresh post-change stats while the tracking window is open.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET AFTER_CALLS = s.CALLS, AFTER_FAILS = s.FAILS,
           AFTER_MEDIAN_MS = s.MED_MS, AFTER_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 MEDIAN(q.TOTAL_ELAPSED_TIME) AS MED_MS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
           AND q.START_TIME > r.CHANGE_SEEN_AT
           AND q.QUERY_TYPE = 'CALL'
           AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
           AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                         REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                            REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
          WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET AFTER_CALLS = s.CALLS, AFTER_FAILS = s.FAILS,
           AFTER_MEDIAN_MS = s.MED_MS, AFTER_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(h.STATE = 'FAILED') AS FAILS,
                 MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
                 APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
            ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
           AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
           AND h.STATE IN ('SUCCEEDED', 'FAILED')
           AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
          WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- 5) Measured credits/call via QUERY_ATTRIBUTION_HISTORY (~6h lag; the
    --    baseline freeze waits 8h after the change so the pre-window is
    --    complete). Guarded: without the view, runtime-only verdicts.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
           SET BASELINE_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.BASELINE_CALLS, 0)
          FROM (
              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR
              FROM (
                  SELECT r.CHANGE_ID, q.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                    ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
                   AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
                   AND q.START_TIME < r.CHANGE_SEEN_AT
                   AND q.QUERY_TYPE = 'CALL'
                   AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
                   AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                        OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                    REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
                  WHERE r.OBJECT_TYPE = 'PROCEDURE'
                    AND r.BASELINE_CREDITS_PER_CALL IS NULL AND r.BASELINE_CALLS > 0
                    AND r.CHANGE_SEEN_AT < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
                   AND h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
                   AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK'
                    AND r.BASELINE_CREDITS_PER_CALL IS NULL AND r.BASELINE_CALLS > 0
                    AND r.CHANGE_SEEN_AT < DATEADD('hour', -8, CURRENT_TIMESTAMP())
              ) x
              JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
          ) s
         WHERE t.CHANGE_ID = s.CHANGE_ID;

        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
           SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL
          FROM (
              -- V172 (R2-022 + R2-025): credits per SETTLED scheduled run. Only runs that started more than 8h
              -- ago count (QUERY_ATTRIBUTION_HISTORY lags up to ~8h, the wait the baseline already makes), and
              -- numerator and denominator cover the SAME runs: an unattributed settled run adds 0 credits, and
              -- a task run's retry attempts add their credits to ONE run (RUN_KEY), so the divisor counts
              -- scheduled runs like BASELINE_CALLS. A value is written only once at least one settled run is
              -- attributed (HAVING), as for the baseline -- a QAH gap never reads as a false IMPROVED.
              SELECT x.CHANGE_ID, SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL
              FROM (
                  SELECT r.CHANGE_ID, q.QUERY_ID, q.QUERY_ID AS RUN_KEY
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                    ON q.START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND q.START_TIME > r.CHANGE_SEEN_AT
                   AND q.START_TIME < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                   AND q.QUERY_TYPE = 'CALL'
                   AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
                   AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                        OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                    REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
                  WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID,
                         r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
                   AND h.QUERY_START_TIME < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
              ) x
              LEFT JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
              HAVING COUNT(a.RID) > 0
          ) s
         WHERE t.CHANGE_ID = s.CHANGE_ID;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ChangeImpactScan', 'attribution_unavailable', :emsg,
                   'credits/call omitted - verdicts use runtime + failure rate only', CURRENT_ROLE();
    END;

    -- 6) Verdicts (rows still inside their tracking window). Regression =
    --    credits/call up threshold% with a material absolute delta, OR p95 up
    --    threshold% and at least 30s, OR failure rate up 20 points.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET LAST_EVALUATED_AT = CURRENT_TIMESTAMP(),
           VERDICT = CASE
               WHEN BASELINE_CALLS < :min_calls THEN 'NO_BASELINE'
               WHEN COALESCE(AFTER_CALLS, 0) < :min_calls THEN 'PENDING'
               WHEN (BASELINE_CREDITS_PER_CALL > 0 AND AFTER_CREDITS_PER_CALL IS NOT NULL
                     AND AFTER_CREDITS_PER_CALL > BASELINE_CREDITS_PER_CALL * (1 + :pct / 100)
                     AND (AFTER_CREDITS_PER_CALL - BASELINE_CREDITS_PER_CALL) * AFTER_CALLS >= 0.25)
                 OR (AFTER_P95_MS > BASELINE_P95_MS * (1 + :pct / 100) AND AFTER_P95_MS >= 30000)
                 OR (AFTER_FAILS / NULLIF(AFTER_CALLS, 0)
                     >= BASELINE_FAILS / NULLIF(BASELINE_CALLS, 0) + 0.2)
                   THEN 'REGRESSED'
               WHEN (BASELINE_CREDITS_PER_CALL > 0 AND AFTER_CREDITS_PER_CALL IS NOT NULL
                     AND AFTER_CREDITS_PER_CALL < BASELINE_CREDITS_PER_CALL * 0.7)
                 OR (AFTER_P95_MS < BASELINE_P95_MS * 0.7)
                   THEN 'IMPROVED'
               ELSE 'NEUTRAL'
           END,
           VERDICT_DETAIL =
               'runs ' || COALESCE(BASELINE_CALLS::VARCHAR, '0') || '->' || COALESCE(AFTER_CALLS::VARCHAR, '0')
               || ' | fails ' || COALESCE(BASELINE_FAILS::VARCHAR, '0') || '->' || COALESCE(AFTER_FAILS::VARCHAR, '0')
               -- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like
               -- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim
               -- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.
               || ' | p95 '
               || CASE WHEN (BASELINE_P95_MS / 1000) IS NULL THEN '?'
                       WHEN ROUND((BASELINE_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (BASELINE_P95_MS / 1000) < 1 THEN ROUND((BASELINE_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (BASELINE_P95_MS / 1000) < 10 THEN TO_VARCHAR(ROUND((BASELINE_P95_MS / 1000), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (AFTER_P95_MS / 1000) IS NULL THEN '?'
                       WHEN ROUND((AFTER_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (AFTER_P95_MS / 1000) < 1 THEN ROUND((AFTER_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (AFTER_P95_MS / 1000) < 10 THEN TO_VARCHAR(ROUND((AFTER_P95_MS / 1000), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' | credits/call ' || COALESCE(ROUND(BASELINE_CREDITS_PER_CALL, 4)::VARCHAR, 'n/a')
               || '->' || COALESCE(ROUND(AFTER_CREDITS_PER_CALL, 4)::VARCHAR, 'n/a')
     WHERE CURRENT_DATE() <= TRACKING_UNTIL;

    -- Tracking ended while still thin: close it out honestly.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET VERDICT = 'INSUFFICIENT_AFTER'
     WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING';

    -- 7) One alert per confirmed regression (dedupe: object + change day).
    --    2x credits/call or a 50%+ failure rate escalates to CRITICAL.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    SELECT c.RULE_ID, r.COMPANY,
           IFF(COALESCE(r.AFTER_CREDITS_PER_CALL / NULLIF(r.BASELINE_CREDITS_PER_CALL, 0), 0) >= 2
                   OR r.AFTER_FAILS / NULLIF(r.AFTER_CALLS, 0) >= 0.5,
               'CRITICAL', c.SEVERITY),
           r.OBJECT_TYPE || ' ' || r.OBJECT_NAME || ' regressed after ' ||
               TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) || ' change',
           'Schema ' || r.DATABASE_NAME || '.' || r.SCHEMA_NAME || ' | '
               || COALESCE(r.VERDICT_DETAIL, '')
               || IFF(r.CHANGED_BY IS NOT NULL, ' | changed by ' || r.CHANGED_BY, ''),
           ROUND(COALESCE(100 * (r.AFTER_CREDITS_PER_CALL / NULLIF(r.BASELINE_CREDITS_PER_CALL, 0) - 1),
                          100 * (r.AFTER_P95_MS / NULLIF(r.BASELINE_P95_MS, 0) - 1)), 1),
           c.RULE_ID || '|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
    FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
      ON c.RULE_ID = 'PERF_CHANGE_REGRESSION' AND c.ENABLED
    WHERE r.VERDICT = 'REGRESSED' AND NOT r.ALERTED
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
      );

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET ALERTED = TRUE
     WHERE VERDICT = 'REGRESSED' AND NOT ALERTED;

    RETURN 'change impact scan complete';
END;
$$;

-- >>> derived:SP_WAREHOUSE_CHANGE_SCAN  (from V109; VERDICT_DETAIL p95 + queue in Hr/Min/Sec (R1-124), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    pct FLOAT;   -- regression threshold, % credits/day increase (ALERT_CONFIG)
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 15) INTO :pct
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'WH_CHANGE_REGRESSION';

    -- 1) Snapshot current settings (SHOW is the only source on this account:
    --    no ACCOUNT_USAGE.WAREHOUSES view — see validate.sql note).
    SHOW WAREHOUSES LIMIT 500;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
        (WAREHOUSE_NAME, COMPANY, WAREHOUSE_SIZE, AUTO_SUSPEND,
         MIN_CLUSTER_COUNT, MAX_CLUSTER_COUNT, SCALING_POLICY, AUTO_RESUME, WAREHOUSE_TYPE)
    SELECT "name",
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE("name"),
           "size",
           TRY_TO_NUMBER("auto_suspend"::VARCHAR),
           TRY_TO_NUMBER("min_cluster_count"::VARCHAR),
           TRY_TO_NUMBER("max_cluster_count"::VARCHAR),
           "scaling_policy",
           "auto_resume"::VARCHAR,
           "type"
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

    -- 2) Diff the two most recent snapshots per warehouse into the registry.
    --    One registry row per (warehouse, setting) per day; first-ever run
    --    has no prior snapshot and registers nothing.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
        (WAREHOUSE_NAME, COMPANY, SETTING, OLD_VALUE, NEW_VALUE, CHANGE_SEEN_AT, TRACKING_UNTIL)
    SELECT d.WAREHOUSE_NAME, d.COMPANY, d.SETTING, d.OLD_VALUE, d.NEW_VALUE,
           CURRENT_TIMESTAMP(), DATEADD('day', 14, CURRENT_DATE())
    FROM (
        WITH ranked AS (
            SELECT WAREHOUSE_NAME, COMPANY, WAREHOUSE_SIZE, AUTO_SUSPEND,
                   MIN_CLUSTER_COUNT, MAX_CLUSTER_COUNT, SCALING_POLICY,
                   ROW_NUMBER() OVER (PARTITION BY WAREHOUSE_NAME ORDER BY SNAPSHOT_AT DESC) AS RN
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('day', -35, CURRENT_TIMESTAMP())
        ),
        cur AS (SELECT * FROM ranked WHERE RN = 1),
        prev AS (SELECT * FROM ranked WHERE RN = 2)
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'SIZE' AS SETTING,
               prev.WAREHOUSE_SIZE AS OLD_VALUE, cur.WAREHOUSE_SIZE AS NEW_VALUE
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.WAREHOUSE_SIZE, '') <> COALESCE(prev.WAREHOUSE_SIZE, '')
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'AUTO_SUSPEND',
               prev.AUTO_SUSPEND::VARCHAR, cur.AUTO_SUSPEND::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.AUTO_SUSPEND, -1) <> COALESCE(prev.AUTO_SUSPEND, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'MIN_CLUSTERS',
               prev.MIN_CLUSTER_COUNT::VARCHAR, cur.MIN_CLUSTER_COUNT::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.MIN_CLUSTER_COUNT, -1) <> COALESCE(prev.MIN_CLUSTER_COUNT, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'MAX_CLUSTERS',
               prev.MAX_CLUSTER_COUNT::VARCHAR, cur.MAX_CLUSTER_COUNT::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.MAX_CLUSTER_COUNT, -1) <> COALESCE(prev.MAX_CLUSTER_COUNT, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'SCALING_POLICY',
               prev.SCALING_POLICY, cur.SCALING_POLICY
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.SCALING_POLICY, '') <> COALESCE(prev.SCALING_POLICY, '')
    ) d
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
        WHERE r.WAREHOUSE_NAME = d.WAREHOUSE_NAME
          AND r.SETTING = d.SETTING
          AND r.CHANGE_SEEN_AT::DATE = CURRENT_DATE()
    );

    -- 3) Freeze pre-change baselines once. $/day is exact warehouse credits
    --    (WAREHOUSE_METERING_HISTORY); the rest comes from QUERY_HISTORY.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CREDITS_PER_DAY = ROUND(s.CR / 14, 4)
      FROM (
          SELECT r.CHANGE_ID, SUM(m.CREDITS_USED) AS CR
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
            ON m.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND m.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND m.START_TIME < r.CHANGE_SEEN_AT
           AND m.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET BASELINE_QUERIES = s.QRY,
           BASELINE_P95_S = ROUND(s.P95_MS / 1000, 1),
           BASELINE_QUEUED_MIN_PER_DAY = ROUND(s.QUEUED_MS / 60000 / 14, 2),
           BASELINE_SPILL_GB_PER_DAY = ROUND(s.SPILL_B / POWER(1024, 3) / 14, 3),
           BASELINE_FAIL_PCT = ROUND(100 * s.FAILS / NULLIF(s.QRY, 0), 2)
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS QRY,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS,
                 SUM(COALESCE(q.QUEUED_OVERLOAD_TIME, 0)) AS QUEUED_MS,
                 SUM(COALESCE(q.BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) AS SPILL_B
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND q.START_TIME < r.CHANGE_SEEN_AT
           AND q.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE r.BASELINE_QUERIES IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- Idle-before warehouses: freeze an explicit zero baseline (-> NO_BASELINE).
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET BASELINE_FROM = DATEADD('day', -14, CHANGE_SEEN_AT),
           BASELINE_QUERIES = COALESCE(BASELINE_QUERIES, 0),
           BASELINE_CREDITS_PER_DAY = COALESCE(BASELINE_CREDITS_PER_DAY, 0)
     WHERE BASELINE_FROM IS NULL OR BASELINE_QUERIES IS NULL;

    -- 4) Refresh post-change stats while the tracking window is open.
    --    Per-day rates divide by the exact elapsed window (min half a day)
    --    so short after-windows compare fairly.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET AFTER_DAYS = ROUND(GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 2),
           AFTER_CREDITS_PER_DAY = ROUND(s.CR / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 4)
      FROM (
          SELECT r.CHANGE_ID, SUM(m.CREDITS_USED) AS CR
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
            ON m.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
           AND m.START_TIME > r.CHANGE_SEEN_AT
           AND m.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET AFTER_QUERIES = s.QRY,
           AFTER_P95_S = ROUND(s.P95_MS / 1000, 1),
           AFTER_QUEUED_MIN_PER_DAY = ROUND(s.QUEUED_MS / 60000 / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 2),
           AFTER_SPILL_GB_PER_DAY = ROUND(s.SPILL_B / POWER(1024, 3) / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 3),
           AFTER_FAIL_PCT = ROUND(100 * s.FAILS / NULLIF(s.QRY, 0), 2)
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS QRY,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS,
                 SUM(COALESCE(q.QUEUED_OVERLOAD_TIME, 0)) AS QUEUED_MS,
                 SUM(COALESCE(q.BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) AS SPILL_B
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
           AND q.START_TIME > r.CHANGE_SEEN_AT
           AND q.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- 5) Verdicts (rows still inside their tracking window). Regression =
    --    $/day up threshold% with >= 1 credit/day absolute, OR p95 up 25%
    --    and >= 30s, OR failure rate up 5 points, OR queueing up 50% and
    --    >= 10 min/day. Improvement requires the other axis not to have
    --    been traded away (cheaper but 3x slower is not IMPROVED).
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET LAST_EVALUATED_AT = CURRENT_TIMESTAMP(),
           VERDICT = CASE
               WHEN COALESCE(BASELINE_QUERIES, 0) < 20 THEN 'NO_BASELINE'
               WHEN COALESCE(AFTER_DAYS, 0) < 3 OR COALESCE(AFTER_QUERIES, 0) < 20 THEN 'PENDING'
               WHEN (AFTER_CREDITS_PER_DAY > BASELINE_CREDITS_PER_DAY * (1 + :pct / 100)
                     AND AFTER_CREDITS_PER_DAY - BASELINE_CREDITS_PER_DAY >= 1)
                 OR (AFTER_P95_S > COALESCE(BASELINE_P95_S, 0) * 1.25 AND AFTER_P95_S >= 30)
                 OR (COALESCE(AFTER_FAIL_PCT, 0) >= COALESCE(BASELINE_FAIL_PCT, 0) + 5)
                 OR (AFTER_QUEUED_MIN_PER_DAY > COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 1.5
                     AND AFTER_QUEUED_MIN_PER_DAY >= 10)
                   THEN 'REGRESSED'
               WHEN (AFTER_CREDITS_PER_DAY <= BASELINE_CREDITS_PER_DAY * 0.85
                     AND COALESCE(AFTER_P95_S, 0) <= COALESCE(BASELINE_P95_S, 0) * 1.10)
                 OR (COALESCE(AFTER_P95_S, 999999) <= COALESCE(BASELINE_P95_S, 0) * 0.75
                     AND AFTER_CREDITS_PER_DAY <= BASELINE_CREDITS_PER_DAY * 1.10)
                   THEN 'IMPROVED'
               ELSE 'NEUTRAL'
           END,
           VERDICT_DETAIL =
               'credits/day ' || COALESCE(ROUND(BASELINE_CREDITS_PER_DAY, 2)::VARCHAR, '?')
               || '->' || COALESCE(ROUND(AFTER_CREDITS_PER_DAY, 2)::VARCHAR, '?')
               -- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like
               -- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim
               -- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.
               || ' | p95 '
               || CASE WHEN (BASELINE_P95_S) IS NULL THEN '?'
                       WHEN ROUND((BASELINE_P95_S) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (BASELINE_P95_S) < 1 THEN ROUND((BASELINE_P95_S) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (BASELINE_P95_S) < 10 THEN TO_VARCHAR(ROUND((BASELINE_P95_S), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (AFTER_P95_S) IS NULL THEN '?'
                       WHEN ROUND((AFTER_P95_S) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (AFTER_P95_S) < 1 THEN ROUND((AFTER_P95_S) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (AFTER_P95_S) < 10 THEN TO_VARCHAR(ROUND((AFTER_P95_S), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' | queue '
               || CASE WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) IS NULL THEN '0s'
                       WHEN ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) < 1 THEN ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) < 10 THEN TO_VARCHAR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) IS NULL THEN '0s'
                       WHEN ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) < 1 THEN ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) < 10 THEN TO_VARCHAR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || '/day'
               || ' | fail ' || COALESCE(BASELINE_FAIL_PCT::VARCHAR, '0') || '->'
               || COALESCE(AFTER_FAIL_PCT::VARCHAR, '0') || '%'
               || ' | ' || COALESCE(BASELINE_QUERIES::VARCHAR, '0') || '->'
               || COALESCE(AFTER_QUERIES::VARCHAR, '0') || ' queries'
     WHERE CURRENT_DATE() <= TRACKING_UNTIL;

    -- Tracking ended while still thin: close it out honestly.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET VERDICT = 'INSUFFICIENT_AFTER'
     WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING';

    -- 6) One alert per confirmed regression (dedupe: warehouse + setting +
    --    change day). 2x credits/day escalates to CRITICAL.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    SELECT c.RULE_ID, r.COMPANY,
           IFF(COALESCE(r.AFTER_CREDITS_PER_DAY / NULLIF(r.BASELINE_CREDITS_PER_DAY, 0), 0) >= 2,
               'CRITICAL', c.SEVERITY),
           'Warehouse ' || r.WAREHOUSE_NAME || ' regressed after ' || r.SETTING || ' '
               || COALESCE(r.OLD_VALUE, '?') || '->' || COALESCE(r.NEW_VALUE, '?')
               || ' on ' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE),
           COALESCE(r.VERDICT_DETAIL, ''),
           ROUND(COALESCE(100 * (r.AFTER_CREDITS_PER_DAY / NULLIF(r.BASELINE_CREDITS_PER_DAY, 0) - 1),
                          100 * (r.AFTER_P95_S / NULLIF(r.BASELINE_P95_S, 0) - 1)), 1),
           c.RULE_ID || '|' || r.WAREHOUSE_NAME || '|' || r.SETTING || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
      ON c.RULE_ID = 'WH_CHANGE_REGRESSION' AND c.ENABLED
    WHERE r.VERDICT = 'REGRESSED' AND NOT r.ALERTED
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || r.WAREHOUSE_NAME || '|' || r.SETTING || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
      );

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET ALERTED = TRUE
     WHERE VERDICT = 'REGRESSED' AND NOT ALERTED;

    RETURN 'warehouse change scan complete';
END;
$$;

-- >>> derived:SP_SCAN_SCHEMA_DRIFT  (from V133; COMPANY via COMPANY_FOR_DATABASE in a b (...) wrapper (R2-024), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Schema-drift monitor (R23). Snapshots the current column set (name + type) of each catalog-registered
-- OBJECT table from ACCOUNT_USAGE.COLUMNS, then compares today's snapshot against the latest PRIOR
-- snapshot to detect added / removed / retyped columns, booking one DQ_SCHEMA_DRIFT alert per drifted
-- table. Metadata only -- no table-data scan, no external grants. A table with no prior snapshot (first
-- scan) establishes a baseline and never alerts. Idempotent for same-day re-runs (today's snapshot is
-- replaced, the diff is always latest-prior vs today).
--
-- SCOPE (deliberate): monitors tables registered as OBJECT entities (a specific DB.SCHEMA.TABLE carrying a
-- DATA_PRODUCT). Unlike the volume / DQ_BREACH arms it does NOT expand DATABASE-level registrations --
-- per-column daily snapshots across every table in a registered database is deferred on cost grounds.
-- Register a table as an OBJECT (Decision Studio catalog) to schema-drift-monitor it.
DECLARE
    enabled_cnt INT;
BEGIN
    SELECT COUNT(*) INTO :enabled_cnt
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'DQ_SCHEMA_DRIFT' AND ENABLED;
    IF (:enabled_cnt = 0) THEN
        RETURN 'schema-drift scan skipped (rule disabled)';
    END IF;

    -- 1) refresh today's snapshot from live column metadata (idempotent for same-day re-runs).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE();
    INSERT INTO DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT (FQN, COLUMN_NAME, DATA_TYPE, SNAPSHOT_DAY)
    SELECT UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME),
           c.COLUMN_NAME, c.DATA_TYPE, CURRENT_DATE()
    FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS c
    JOIN DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG e
      ON e.ENTITY_TYPE = 'OBJECT'
     AND UPPER(e.ENTITY_KEY) = UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME)
     AND NULLIF(TRIM(e.DATA_PRODUCT), '') IS NOT NULL
    WHERE c.DELETED IS NULL;

    -- 2) diff today's snapshot vs the latest PRIOR snapshot per table; one alert per drifted table.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED AND RULE_ID = 'DQ_SCHEMA_DRIFT'
    ),
    cur AS (
        SELECT FQN, COLUMN_NAME, DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE()
    ),
    prior_day AS (
        SELECT FQN, MAX(SNAPSHOT_DAY) AS PD
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
        WHERE SNAPSHOT_DAY < CURRENT_DATE() GROUP BY FQN
    ),
    prior AS (
        SELECT s.FQN, s.COLUMN_NAME, s.DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT s
        JOIN prior_day p ON p.FQN = s.FQN AND p.PD = s.SNAPSHOT_DAY
    ),
    changes AS (
        SELECT c.FQN AS FQN, 'added ' || c.COLUMN_NAME AS CH
        FROM cur c
        JOIN prior_day pd ON pd.FQN = c.FQN
        LEFT JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE p.COLUMN_NAME IS NULL
        UNION ALL
        -- removed columns -- but ONLY for a table still present in today's snapshot (the "cf" guard,
        -- symmetric with the added branch's prior_day guard). Without it, a table that was DROPPED or
        -- UNREGISTERED (cur has zero rows for it, but a prior snapshot lingers under 90-day retention)
        -- would report every column as "removed" and, because the dedup key rolls by date, re-fire that
        -- spurious alert daily until the snapshot ages out. A vanished table is a freshness/existence
        -- concern (the SLA + row-volume panels own it), not schema drift.
        SELECT p.FQN, 'removed ' || p.COLUMN_NAME
        FROM prior p
        JOIN (SELECT DISTINCT FQN FROM cur) cf ON cf.FQN = p.FQN
        LEFT JOIN cur c ON c.FQN = p.FQN AND c.COLUMN_NAME = p.COLUMN_NAME
        WHERE c.COLUMN_NAME IS NULL
        UNION ALL
        SELECT c.FQN, 'retyped ' || c.COLUMN_NAME || ' (' || p.DATA_TYPE || ' -> ' || c.DATA_TYPE || ')'
        FROM cur c JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE c.DATA_TYPE <> p.DATA_TYPE
    ),
    agg AS (
        SELECT FQN, COUNT(*) AS N,
               LISTAGG(CH, ', ') WITHIN GROUP (ORDER BY CH) AS CHANGES
        FROM changes GROUP BY FQN
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT cfg.RULE_ID,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN, '.', 1)),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
           cfg.SEVERITY,
           a.FQN || ' schema changed: ' || a.N || ' column change(s)',
           'Registered-table schema drift vs the prior snapshot: ' || LEFT(a.CHANGES, 1700) ||
               '. Confirm the change was intended (upstream DDL / migration) and update downstream consumers.',
           a.N,
           cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())
    FROM agg a
    CROSS JOIN cfg

    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );

    -- 3) retention: keep ~90 days of column snapshots.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
    WHERE SNAPSHOT_DAY < DATEADD('day', -90, CURRENT_DATE());

    RETURN 'schema-drift scan complete';
END;
$$;

-- >>> derived:SP_SCAN_CLOUD_SVC_ANOMALY  (from V150; the disabled-rule guard counts ENABLED rows (R1-227), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Per-warehouse cloud-services anomaly scan (Phase 2). Books one COST_CLOUD_SVC_ANOMALY alert per
-- (warehouse, day) whose gross cloud-services credits are a robust-z outlier vs the warehouse's own
-- prior-28-day baseline. This is the PER-ENTITY replacement for the fixed 10/20% CS-ratio threshold:
-- a warehouse that is CHRONICALLY compile-heavy (high but STEADY cloud services) sits in-baseline and
-- does NOT alert, while a genuine step-change (a new chatty tool, a runaway metadata loop) fires.
--
-- Same robust median/MAD modified-z engine as the COST_ANOMALY_SWEEP warehouse/service arm and the
-- app twin app/logic/anomaly.robust_zscores (0.6745; mean-absolute-deviation / 0.7979 fallback when
-- MAD collapses to 0). Source is MART_CLOUD_SVC_DAILY (gross CS credits, per family x warehouse x day;
-- CS is USAGE, never billable -- the ~10% rebate is account+day and not warehouse-decomposable).
-- Materiality is a CS-CREDIT VOLUME floor, NOT the $50 compute floor: cloud-services credits are tiny,
-- so a dollar gate would suppress real CS step-changes. The last 3 complete days are (re)scored with a
-- per-(series,day) dedup key, so a day deleted mid-reconcile is picked up on the next run.
DECLARE
    zthr FLOAT;
    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging
    enabled_cnt INT;
BEGIN
    -- V172 (R1-227): gate on the ENABLED row count like SP_SCAN_SCHEMA_DRIFT / SP_SCAN_RECON_ERRORS. V150
    -- tested :zthr IS NULL after COALESCE(.., 3.5), which never fired, so a disabled or deleted rule still
    -- scanned (at 3.5, not the tuned threshold) and SP_NOTIFY_WEBHOOK delivered what it booked.
    SELECT COUNT(*), COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :enabled_cnt, :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;
    IF (:enabled_cnt = 0) THEN
        RETURN 'cloud-services anomaly scan skipped (rule disabled)';
    END IF;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'CLOUD SVC ' || COALESCE(WAREHOUSE_NAME, 'NONE') AS SERIES, COMPANY, DAY,
               SUM(CS_CREDITS) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())
    )
    SELECT 'COST_CLOUD_SVC_ANOMALY', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' cloud-services collapsed to ', ' cloud-services spiked to ') ||
               ROUND(l.CREDITS, 2) || ' credits on ' || TO_VARCHAR(l.DAY) ||
               ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 2) || ' CS credits/day over the prior 28d (GROSS usage, before the ' ||
               'account-level ~10% rebate). Robust z ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. This per-warehouse baseline replaces the fixed 10/20% ratio: a chronically compile-heavy ' ||
               'warehouse stays in-baseline, so this is a real step-change. Investigate: Operations > ' ||
               'Queries > cloud-services chatter by application, or Cost > Spend cloud-services health.',
           l.ROBUST_Z,
           'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS >= :cs_floor)
          OR (l.SIGNED_Z < 0 AND l.MED >= :cs_floor)
      )
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    RETURN 'cloud-services anomaly scan complete';
END;
$$;

-- >>> derived:SP_ANOMALY_SWEEP  (from V150; DT / volume / DQ_BREACH COMPANY via COMPANY_FOR_DATABASE (R2-024), COST_ORG_ACCOUNT_CREEP pointer (R2-095), COST_ANOMALY_SWEEP enabled gate (R1-227 rider), normalized CORTEX_MODEL read (CORTEX-NULLIF), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    zthr FLOAT;
    credit_price FLOAT;
    ai_model VARCHAR;
    ev_id VARCHAR;
    ev_title VARCHAR;
    day_s VARCHAR;
    series_s VARCHAR;
    wh_s VARCHAR;
    evidence VARCHAR;
    ai_prompt VARCHAR;
    ai_resp VARCHAR;
    c_new CURSOR FOR
        SELECT EVENT_ID, TITLE, DEDUPE_KEY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        WHERE RULE_ID = 'COST_ANOMALY_SWEEP'
          AND RAISED_AT >= DATEADD('minute', -15, CURRENT_TIMESTAMP())
          AND DETAIL NOT LIKE '%| AI:%'
        LIMIT 5;
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;

    -- V076: materiality floor mirrors the app-side warehouse anomaly gate
    -- (app/logic/anomaly.py): flag on real money AND a real baseline, so an
    -- idle warehouse cannot post a z+20 event on a trivial active day.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'WAREHOUSE ' || WAREHOUSE_NAME AS SERIES, COMPANY, DAY,
               SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
        UNION ALL
        SELECT 'SERVICE ' || SERVICE_TYPE, 'ALL', DAY, SUM(CREDITS_BILLED)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    -- V097: mean-absolute-deviation fallback denominator (== abs_dev.mean() in the
    -- app twin app/logic/anomaly.py robust_zscores) for series whose MAD collapses to 0.
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        -- task-dag-ordering (round 9): score the last 3 COMPLETE days, not just MAX(DAY).
        -- The nightly reconcile deletes+reloads FACT_METERING_DAILY / FACT_WAREHOUSE_DAILY for
        -- D-1..D-3 non-atomically; if the standalone sweep fires mid-reload, MAX(DAY) collapses
        -- to D-4 and yesterday's spike is scored against a truncated series (or skipped) and,
        -- because the sweep only ever scored the single latest day, never re-examined -- a
        -- permanently missed COST_ANOMALY_SWEEP alert. Scoring D-1..D-3 with the existing
        -- per-(series,day) DEDUPE_KEY (each day alerts at most once) self-heals: a day deleted at
        -- sweep time is picked up on the next run once reconcile has reloaded it.
        WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())
    )
    SELECT 'COST_ANOMALY_SWEEP', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ') ||
               ROUND(l.CREDITS, 1) || ' credits on ' ||
               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 1) || ' credits/day over the prior 28d. ' ||
               'Robust z-score ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. Investigate: Cost > Spend / Attribution for that day.',
           l.ROBUST_Z,
           'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)
          OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)
      )
      -- V172 (R1-227 rider): a disabled or deleted COST_ANOMALY_SWEEP rule books nothing. The threshold read
      -- above COALESCEs to 3.5 and never gated, so turning the rule off changed nothing. Not an early
      -- RETURN: the DT, drift, creep, volume, DQ and cloud-services arms below still run.
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                  WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    -- Dynamic-table refresh failures (guarded: accounts without the view
    -- keep the sweep's cost half working).
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               IFF(d.FAILURES >= 5, 'CRITICAL', c.SEVERITY),
               d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   ': ' || d.FAILURES || ' dynamic-table refresh failure(s) (24h)',
               'Schema ' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                   ' | last state ' || d.LAST_STATE ||
                   '. Downstream tables are serving stale data until this refreshes.',
               d.FAILURES,
               c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DATABASE_NAME, SCHEMA_NAME, NAME,
                   COUNT_IF(STATE = 'FAILED') AS FAILURES,
                   MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
            WHERE REFRESH_END_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY 1, 2, 3
            HAVING COUNT_IF(STATE = 'FAILED') > 0
        ) d ON c.RULE_ID = 'PIPE_DT_FAILURES' AND c.ENABLED AND d.FAILURES > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dynamic_tables_unavailable', 'DT refresh view not readable',
                   'cost anomaly sweep unaffected', CURRENT_ROLE();
    END;


    -- PERF_FINGERPRINT_DRIFT (Mondays): p95 per query family, last 7d vs the
    -- prior 28d — catches regressions that arrive WITHOUT a DDL change
    -- (data growth, clustering decay, plan changes). Complements the
    -- change-anchored V010 tracker.
    IF (DAYOFWEEKISO(CURRENT_DATE()) = 1) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL',
               IFF(f.P95_RECENT_S >= f.P95_BASE_S * 3, 'HIGH', c.SEVERITY),
               'Query family p95 ' || f.P95_BASE_S || 's -> ' || f.P95_RECENT_S || 's: ' ||
                   LEFT(f.SAMPLE_TEXT, 60),
               'Hash ' || f.QUERY_PARAMETERIZED_HASH || ' | runs ' || f.RUNS_BASE || ' -> ' ||
                   f.RUNS_RECENT || ' | 7d vs prior 28d, no change event required. ' ||
                   'Drill: Operations > Queries (heaviest queries).',
               ROUND(100 * (f.P95_RECENT_S / NULLIF(f.P95_BASE_S, 0) - 1), 1),
               c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                   TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
                   COUNT_IF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_RECENT,
                   COUNT_IF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_BASE,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_RECENT_S,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_BASE_S
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_PARAMETERIZED_HASH IS NOT NULL
            GROUP BY 1
            HAVING RUNS_RECENT >= 20 AND RUNS_BASE >= 20
        ) f ON c.RULE_ID = 'PERF_FINGERPRINT_DRIFT' AND c.ENABLED
           AND f.P95_BASE_S > 0
           AND f.P95_RECENT_S > f.P95_BASE_S * (1 + c.THRESHOLD_NUM / 100)
           AND f.P95_RECENT_S >= 10
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    END IF;


    -- COST_ORG_ACCOUNT_CREEP (guarded): any org account's currency spend up
    -- threshold% week-over-week — a sibling account can't surprise you.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               o.ACCOUNT_NAME || ' org spend up ' || ROUND(o.PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(o.CUR, 0) || ' vs prior ' || ROUND(o.PRV, 0) || ' ' || o.CCY ||
                   '. Breakdown: Cost Intelligence > Contract & Forecast.',
               o.PCT,
               c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT ACCOUNT_NAME, CCY, CUR, PRV, (CUR / NULLIF(PRV, 0) - 1) * 100 AS PCT
            FROM (
                SELECT ACCOUNT_NAME, MAX(CURRENCY) AS CCY,
                       SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS CUR,
                       SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS PRV
                FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
                WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
                GROUP BY 1
            )
        ) o ON c.RULE_ID = 'COST_ORG_ACCOUNT_CREEP' AND c.ENABLED
           AND o.PCT > c.THRESHOLD_NUM AND o.CUR >= 100
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'org_usage_unavailable', 'ORGANIZATION_USAGE not readable',
                   'org creep check skipped', CURRENT_ROLE();
    END;

    -- PIPE_VOLUME_DROP (guarded): yesterday's rows-added collapsed vs the
    -- prior-7-day average on tables that normally move real volume.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(v.DB),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               c.SEVERITY,
               v.DB || '.' || v.SCH || '.' || v.TBL || ' volume down ' || ROUND(v.DROP_PCT, 0) ||
                   '% (' || v.Y_ROWS || ' rows vs ~' || ROUND(v.AVG_ROWS, 0) || '/day)',
               'Yesterday vs prior-7d average. Upstream feed, failed COPY, or intentional? ' ||
                   'Check Operations > Pipeline SLA.',
               v.DROP_PCT,
               c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL || '|' ||
                   TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DB, SCH, TBL, Y_ROWS, AVG_ROWS,
                   (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 AS DROP_PCT
            FROM (
                SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
                       SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) AS Y_ROWS,
                       SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS
                FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
                WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
                  AND d.START_TIME < CURRENT_DATE()
                  -- PROD only, BOTH companies (owner decision 2026-07-08
                  -- after the DEV/SIT storm): ALFA_EDW_PRD + ALFA_EDW_MGM by
                  -- name, and every *_PRD database by suffix — which is what
                  -- covers Trexis PROD (TRXS_EDW_PRD, TRXS_GW_DATA_PRD,
                  -- TRXS_ABC_METADATA_PRD). DEV/SIT/SAN stay silent. Same
                  -- semantics as app environment_clause('PROD').
                  AND (UPPER(d.DATABASE_NAME) IN ('ALFA_EDW_PRD', 'ALFA_EDW_MGM')
                       OR UPPER(d.DATABASE_NAME) LIKE '%!_PRD' ESCAPE '!')
                GROUP BY 1, 2, 3
                HAVING AVG_ROWS >= 1000
            )
        ) v ON c.RULE_ID = 'PIPE_VOLUME_DROP' AND c.ENABLED
           AND v.DROP_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'volume-drop check skipped', CURRENT_ROLE();
    END;

    -- DQ_BREACH (R24): registered-product tables whose most recent rows-added load is a robust-z
    -- outlier (spike OR drop) vs its own prior loads -- the DB-side twin of logic/dq.row_volume_anomalies
    -- (the Operations data-quality panel), now booked as alerts. Guarded so an ACCOUNT_USAGE gap can't
    -- break the sweep's cost/volume halves.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        -- 28-day load window: MUST match the panel's product_row_volume(28) so the alert and the
        -- Operations data-quality panel score the SAME series (same baseline, same >=11-load
        -- eligibility) and can never disagree on a table -- a wider window would fire on tables the
        -- panel omits or shift the baseline median/MAD. (The panel's QUALIFY DENSE_RANK<=600 render
        -- cap is deliberately NOT mirrored: the alert has no render limit, so a real anomaly on the
        -- 601st+ registered table still pages -- more complete, never wrong.)
        WITH vol AS (
            SELECT d.DATABASE_NAME AS DB,
                   d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.TABLE_NAME AS FQN,
                   DATE(d.START_TIME) AS DAY,
                   SUM(d.ROWS_ADDED) AS ROWS_ADDED
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
            WHERE d.START_TIME >= DATEADD('day', -28, CURRENT_DATE())
              AND d.START_TIME < CURRENT_DATE()
              AND UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'
              AND NOT REGEXP_LIKE(d.TABLE_NAME, '.*_[0-9]{8}(_[0-9]+)+', 'i')
            GROUP BY 1, 2, 3
            HAVING SUM(d.ROWS_ADDED) > 0
        ),
        cat AS (
            SELECT ENTITY_TYPE, UPPER(ENTITY_KEY) AS K, DATA_PRODUCT, OWNER_NAME, CRITICALITY
            FROM DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG
            WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
        ),
        reg AS (
            SELECT v.FQN, v.DB, v.DAY, v.ROWS_ADDED,
                   COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
                   COALESCE(om.OWNER_NAME, dm.OWNER_NAME) AS OWNER_NAME
            FROM vol v
            LEFT JOIN cat om ON om.ENTITY_TYPE = 'OBJECT' AND om.K = UPPER(v.FQN)
            LEFT JOIN cat dm ON om.K IS NULL AND dm.ENTITY_TYPE = 'DATABASE' AND dm.K = UPPER(v.DB)
            WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
        ),
        ranked AS (
            SELECT FQN, DB, DAY, ROWS_ADDED, DATA_PRODUCT, OWNER_NAME,
                   ROW_NUMBER() OVER (PARTITION BY FQN ORDER BY DAY DESC) AS RN,
                   COUNT(*) OVER (PARTITION BY FQN) AS N_LOADS
            FROM reg
        ),
        base_med AS (
            SELECT FQN, MEDIAN(ROWS_ADDED) AS MED
            FROM ranked WHERE RN > 1 GROUP BY 1
        ),
        base_mad AS (
            SELECT r.FQN, m.MED, MEDIAN(ABS(r.ROWS_ADDED - m.MED)) AS MAD_RAW
            FROM ranked r JOIN base_med m ON m.FQN = r.FQN
            WHERE r.RN > 1 GROUP BY 1, 2
        ),
        scored AS (
            SELECT l.FQN, l.DB, l.DAY, l.ROWS_ADDED AS LATEST_ROWS, l.DATA_PRODUCT, l.OWNER_NAME,
                   b.MED,
                   (l.ROWS_ADDED - b.MED)
                       / NULLIF(GREATEST(b.MAD_RAW * 1.4826, 0.15 * b.MED), 0) AS RAW_Z
            FROM ranked l
            JOIN base_mad b ON b.FQN = l.FQN
            WHERE l.RN = 1
              AND l.N_LOADS >= 11
              AND b.MED >= 100
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(s.DB),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               c.SEVERITY,
               s.FQN || IFF(s.RAW_Z < 0, ' rows-added dropped to ', ' rows-added spiked to ') ||
                   s.LATEST_ROWS || ' on ' || TO_VARCHAR(s.DAY) || ' (z=' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ')',
               'Registered product ' || COALESCE(s.DATA_PRODUCT, '(unknown)') || ', owner ' ||
                   COALESCE(s.OWNER_NAME, '(unassigned)') || '. Baseline median ' || ROUND(s.MED, 0) ||
                   ' rows/load over its prior loads. Robust z ' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ' vs threshold ' || c.THRESHOLD_NUM ||
                   '. Investigate: Operations > Pipeline data-quality panel.',
               LEAST(99.9, GREATEST(-99.9, s.RAW_Z)),
               c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN scored s ON c.RULE_ID = 'DQ_BREACH' AND c.ENABLED
           AND ABS(s.RAW_Z) >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'DQ_BREACH check skipped', CURRENT_ROLE();
    END;

    -- DQ_SCHEMA_DRIFT (R23): schema drift on registered-product tables (columns added / removed /
    -- retyped vs the prior snapshot). The stateful snapshot + diff lives in SP_SCAN_SCHEMA_DRIFT; this
    -- arm just CALLs it inside the standard guard, so a COLUMNS/catalog issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'schema_drift_scan_failed', SQLERRM,
                   'DQ_SCHEMA_DRIFT check skipped', CURRENT_ROLE();
    END;

    -- COST_CLOUD_SVC_ANOMALY (Phase 2): per-warehouse cloud-services robust-z step-change vs a 28d
    -- baseline, replacing the fixed 10/20% CS-ratio threshold so a chronically compile-heavy warehouse
    -- stays in-baseline while a real spike fires. The scan lives in SP_SCAN_CLOUD_SVC_ANOMALY; this arm
    -- just CALLs it inside the standard guard, so a MART_CLOUD_SVC_DAILY issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cloud_svc_anomaly_scan_failed', SQLERRM,
                   'COST_CLOUD_SVC_ANOMALY check skipped', CURRENT_ROLE();
    END;

    -- Pre-explain fresh anomalies (guarded): grounded Cortex hypothesis is
    -- appended to the event DETAIL so the webhook message arrives explained.
    -- Capped at 5 events/run to bound AI spend.
    BEGIN
        -- V172 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a
        -- valid name else the default): a blank, padded, mixed-case or invalid stored value no longer
        -- reaches COMPLETE (the V171 digest read, same literal)
        SELECT IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')
          INTO :ai_model
        FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
              FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);
        FOR e IN c_new DO
            ev_id := e.EVENT_ID;
            ev_title := e.TITLE;
            series_s := SPLIT_PART(e.DEDUPE_KEY, '|', 2);
            day_s := SPLIT_PART(e.DEDUPE_KEY, '|', 3);
            wh_s := IFF(series_s LIKE 'WAREHOUSE %', LTRIM(SUBSTR(series_s, 10)), '');
            SELECT LISTAGG(SAMPLE_TEXT || ' day=' || H_DAY || 'h prior_avg=' || H_PRI || 'h', '; ')
              INTO :evidence
            FROM (
                SELECT ANY_VALUE(LEFT(QUERY_TEXT, 60)) AS SAMPLE_TEXT,
                       ROUND(SUM(IFF(DATE(START_TIME) = TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS H_DAY,
                       ROUND(SUM(IFF(DATE(START_TIME) < TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS H_PRI
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -7, TO_DATE(:day_s))
                  AND START_TIME < DATEADD('day', 1, TO_DATE(:day_s))
                  AND (:wh_s = '' OR WAREHOUSE_NAME = :wh_s)
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY QUERY_PARAMETERIZED_HASH
                ORDER BY H_DAY DESC
                LIMIT 10
            );
            ai_prompt := 'You are a Snowflake cost analyst. ALERT: ' || :ev_title ||
                         '. EVIDENCE (top query families, elapsed hours on the day vs prior-7d avg): ' ||
                         COALESCE(:evidence, 'none') ||
                         '. Using ONLY this evidence, name the 1-2 most likely drivers with their ' ||
                         'numbers, or say evidence is inconclusive. Max 80 words. Never invent data.';
            ai_resp := SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt);
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
               SET DETAIL = LEFT(COALESCE(DETAIL, '') || ' | AI: ' || :ai_resp, 2000)
             WHERE EVENT_ID = :ev_id;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cortex_pre_explain_unavailable',
                   'CORTEX.COMPLETE failed - events remain unexplained (drawer AI still works)',
                   'model or grant issue', CURRENT_ROLE();
    END;

    RETURN 'anomaly sweep v3 complete';
END;
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- One-time repairs (after the CREATEs, before the version row). Idempotent and bounded; OVERWATCH tables only.
-- ---------------------------------------------------------------------------------------------------------------

-- R1 (R2-023) OBJECT_CHANGE_REGISTRY.COMPANY by the V044 classification. The registry is append-only (MERGE WHEN
-- NOT MATCHED), so every row the old scan stamped with the raw guess kept it: an unmapped database read ALFA, a
-- COMPANY_SCOPE override was ignored. Only rows whose stamp changes are written.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET COMPANY = m.CO
  FROM (
      SELECT d.DATABASE_NAME, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME) AS CO
      FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d
  ) m
 WHERE t.DATABASE_NAME = m.DATABASE_NAME
   AND t.COMPANY IS DISTINCT FROM m.CO;

-- R1b live PERF_CHANGE_REGRESSION events (OPEN / ACK / SNOOZED, not linked to an incident -- an incident keeps
-- the company it was declared under) follow: from the re-stamped registry row of the same key, else the database
-- split out of the object FQN in DEDUPE_KEY part 2 ('PERF_CHANGE_REGRESSION|DB.SCHEMA.NAME|date'; part 2 is never
-- passed to the UDF whole). Step 7 dedupes on the key only, so nothing re-raises.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT k.EVENT_ID, COALESCE(g.COMPANY, k.FQN_COMPANY) AS NEW_COMPANY
      FROM (
          SELECT x.EVENT_ID, x.DEDUPE_KEY, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS FQN_COMPANY
          FROM (
              SELECT e.EVENT_ID, e.DEDUPE_KEY, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION'
                AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
          ) x
      ) k
      LEFT JOIN (
          SELECT 'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) AS DEDUPE_KEY,
                 MAX(r.COMPANY) AS COMPANY
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          GROUP BY 1
      ) g ON g.DEDUPE_KEY = k.DEDUPE_KEY
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;

-- R2 (R2-024) live DQ_SCHEMA_DRIFT / PIPE_DT_FAILURES / PIPE_VOLUME_DROP / DQ_BREACH events re-stamped the same way:
-- all four keys are 'RULE|DB.SCHEMA.OBJECT|day', so the database is part 2 up to its first dot.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT x.EVENT_ID, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS NEW_COMPANY
      FROM (
              SELECT e.EVENT_ID, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID IN ('DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')
                AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      ) x
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;

-- R4 (R2-025) still-tracking TASK baselines were frozen on raw attempts. Re-freeze them on the terminal attempt per
-- scheduled run over their own [CHANGE_SEEN_AT - 14d, CHANGE_SEEN_AT) window (30 days of TASK_HISTORY covers every
-- tracking row; no now-20d clip). The frozen credits numerator summed every attempt and still does, so credits/call
-- is rescaled by OLD_CALLS / NEW_CALLS (a factor of 1 on a re-run); a NULL credits/call stays NULL.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
       BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS,
       BASELINE_CREDITS_PER_CALL = s.OLD_CPC * s.OLD_CALLS / NULLIF(s.CALLS, 0)
  FROM (
      SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
             COUNT_IF(h.STATE = 'FAILED') AS FAILS,
             MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
             APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS,
             MAX(r.BASELINE_CALLS) AS OLD_CALLS, MAX(r.BASELINE_CREDITS_PER_CALL) AS OLD_CPC
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
      GROUP BY r.CHANGE_ID
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;

-- R3 (R2-021) the suffix match froze baselines that blended a RUN_<name> / X_<name> wrapper's calls into <name>.
-- Frozen baselines never recompute, so null them -- only for still-tracking PROCEDURE rows whose short name is a
-- strict suffix of another procedure's name (deleted procedures included: their old calls are still in the
-- window), and only once: a re-run must not undo a scan's re-freeze (each re-null re-freezes over a shorter
-- window). R3 runs LAST, directly before the version row, so a missing 172 row means R3 has not committed: every
-- statement that can stop the file (the five CREATEs, R1-R2, R4's 30-day TASK_HISTORY read) runs before it, and a
-- retry after any of them nulls these rows for the first time, even if a scan ran in between. The next
-- TASK_CHANGE_IMPACT_SCAN re-freezes them with the anchored match (over the scan's own 20-day reach: a change
-- older than 6 days gets a shorter baseline, at least 6 days).
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
       BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
  FROM (
      SELECT DISTINCT r.CHANGE_ID
      FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
      JOIN SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES p
        ON ENDSWITH(UPPER(p.PROCEDURE_NAME), UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3)))
       AND UPPER(p.PROCEDURE_NAME) <> UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3))
      WHERE r.OBJECT_TYPE = 'PROCEDURE'
        AND CURRENT_DATE() <= r.TRACKING_UNTIL
        AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172)
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 172 AS VERSION,
       'Detection scans (V166-V172 wave, detection cluster). SP_CHANGE_IMPACT_SCAN re-derived from V140: COMPANY via COMPANY_FOR_DATABASE in both registration arms (R2-023; arm 1a through a grouped derived table, the V030 shape); procedure calls matched by CALL<name>( or .<name>( over a whitespace-class strip, the drill rule, so RUN_<name> no longer blends into <name> (R2-021); TASK runs, fails and p95 count the terminal attempt per scheduled run (R2-025); the AFTER credits/call counts settled runs (started more than 8h ago) only, LEFT JOIN plus HAVING, divided by distinct scheduled runs (R2-022); VERDICT_DETAIL p95 in Hr/Min/Sec (R1-124). SP_WAREHOUSE_CHANGE_SCAN re-derived from V109: VERDICT_DETAIL p95 and queue in Hr/Min/Sec (R1-124). SP_SCAN_SCHEMA_DRIFT re-derived from V133 and SP_ANOMALY_SWEEP from V150: PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT COMPANY via COMPANY_FOR_DATABASE in a b wrapper (R2-024); the sweep also points COST_ORG_ACCOUNT_CREEP at Cost Intelligence > Contract & Forecast (R2-095), books COST_ANOMALY_SWEEP only while the rule is enabled (R1-227 rider, no early return) and reads CORTEX_MODEL normalized like the app (CORTEX-NULLIF); RETURN stays v3. SP_SCAN_CLOUD_SVC_ANOMALY re-derived from V150: the disabled-rule guard counts ENABLED rows (R1-227). One-time repairs: OBJECT_CHANGE_REGISTRY.COMPANY re-stamped, and live (OPEN, ACK, SNOOZED, not incident-linked) PERF_CHANGE_REGRESSION, DQ_SCHEMA_DRIFT, PIPE_DT_FAILURES, PIPE_VOLUME_DROP and DQ_BREACH events re-stamped from the database in their object FQN; tracking TASK baselines re-frozen on the terminal-attempt basis with credits/call rescaled; last, right before this row, a first-apply null of suffix-collided tracking PROCEDURE baselines (re-frozen by the next scan). No new object, no task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172);
