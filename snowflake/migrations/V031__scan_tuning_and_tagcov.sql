-- V031__scan_tuning_and_tagcov.sql — the tuning trio's migration half.
--
-- [1] SP_CHANGE_IMPACT_SCAN v2 (first replacement since V010). The scan was
--     the biggest single statement family on the shared warehouse (median
--     278s/call): its after-window joins used a blanket -18d bound and paid
--     the double-REPLACE POSITION match on every CALL's full text. v2 bounds
--     the joins to the OLDEST STILL-TRACKING change (:trk_lo — near-zero
--     scan when nothing is tracking) and adds a cheap ILIKE pre-filter so
--     the expensive normalization only runs on plausible rows. Verdict
--     semantics unchanged; derived VERBATIM from V010 with the enumerated
--     edits (tests/test_live_round7.py).
--
-- [2] MART_TAG_COVERAGE_DAILY — the user-grain tagged-exec mart the family
--     mart could not carry (no user grain), closing the last honest
--     non-adoption from wave 2. Loader arm follows the V030 shape law: the
--     UDF touches only a plain column of an already-aggregated derived
--     table. Freshness view re-emitted with the 17th arm.
--
-- Idempotent. Apply IN ORDER after V030. No new grants needed.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20031, 'BLOCKED: SCHEMA_VERSION < 30 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 30) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY (
    DAY DATE NOT NULL,
    USER_NAME VARCHAR(200) NOT NULL,
    COMPANY VARCHAR(40),
    QUERIES NUMBER(12,0),
    EXEC_SEC NUMBER(18,1),
    UNTAGGED_EXEC_SEC NUMBER(18,1),
    LOAD_TS TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

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
        SELECT 'PROCEDURE' AS OBJECT_TYPE,
               PROCEDURE_CATALOG AS DATABASE_NAME,
               PROCEDURE_SCHEMA AS SCHEMA_NAME,
               PROCEDURE_CATALOG || '.' || PROCEDURE_SCHEMA || '.' || PROCEDURE_NAME AS OBJECT_NAME,
               IFF(PROCEDURE_CATALOG LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,
               MAX(LAST_ALTERED) AS CHANGE_SEEN_AT
        FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
        WHERE DELETED IS NULL
          AND PROCEDURE_CATALOG IS NOT NULL
          AND LAST_ALTERED >= DATEADD('day', -3, CURRENT_TIMESTAMP())
        GROUP BY 1, 2, 3, 4, 5
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
                   IFF(DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,
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
    --    Procedure calls are matched by 'NAME(' in normalized CALL text; a
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
           AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                        REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
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
          JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
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
           AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                        REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
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
          JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
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
                   AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
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
                         SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
          ) s
         WHERE t.CHANGE_ID = s.CHANGE_ID;

        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
           SET AFTER_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.AFTER_CALLS, 0)
          FROM (
              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR
              FROM (
                  SELECT r.CHANGE_ID, q.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                    ON q.START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND q.START_TIME > r.CHANGE_SEEN_AT
                   AND q.QUERY_TYPE = 'CALL'
                   AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
                   AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
                  WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
              ) x
              JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
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
               || ' | p95 ' || COALESCE(ROUND(BASELINE_P95_MS / 1000, 1)::VARCHAR, '?') || 's->'
               || COALESCE(ROUND(AFTER_P95_MS / 1000, 1)::VARCHAR, '?') || 's'
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

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
    loaded VARCHAR DEFAULT '';
    d INT;
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;

    IF (UPPER(:SCOPE) = 'HOURLY') THEN

        -- [1] warehouse efficiency ------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY t
            USING (
                WITH m AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           SUM(CREDITS_USED) AS CREDITS_TOTAL,
                           SUM(CREDITS_USED_COMPUTE) AS CREDITS_COMPUTE,
                           COUNT_IF(CREDITS_USED > 0) AS BILLED_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2
                ),
                q AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,
                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,
                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,
                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS,
                           COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) AS ACTIVE_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_NAME IS NOT NULL
                    GROUP BY 1, 2
                )
                SELECT COALESCE(m.DAY, q.DAY) AS DAY,
                       COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)) AS COMPANY,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0), 4) AS CREDITS_TOTAL,
                       ROUND(COALESCE(m.CREDITS_COMPUTE, 0), 4) AS CREDITS_COMPUTE,
                       COALESCE(q.QUERIES, 0) AS QUERIES,
                       COALESCE(q.FAILS, 0) AS FAILS,
                       ROUND(COALESCE(q.QUEUED_MIN, 0), 2) AS QUEUED_MIN,
                       ROUND(COALESCE(q.SPILL_GB, 0), 3) AS SPILL_GB,
                       ROUND(COALESCE(q.P95_S, 0), 1) AS P95_S,
                       ROUND(COALESCE(q.EXEC_HOURS, 0), 3) AS EXEC_HOURS,
                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,
                       COALESCE(q.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,
                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(q.ACTIVE_HOURS, 0), 0)
                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY
                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET
                COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL,
                CREDITS_COMPUTE = s.CREDITS_COMPUTE, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_MIN = s.QUEUED_MIN, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S,
                EXEC_HOURS = s.EXEC_HOURS, BILLED_HOURS = s.BILLED_HOURS,
                ACTIVE_HOURS = s.ACTIVE_HOURS, IDLE_PCT = s.IDLE_PCT,
                CREDITS_PER_QUERY = s.CREDITS_PER_QUERY, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
                 QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS, IDLE_PCT, CREDITS_PER_QUERY)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_TOTAL, s.CREDITS_COMPUTE, s.QUERIES, s.FAILS,
                    s.QUEUED_MIN, s.SPILL_GB, s.P95_S, s.EXEC_HOURS, s.BILLED_HOURS, s.ACTIVE_HOURS, s.IDLE_PCT, s.CREDITS_PER_QUERY);
            loaded := loaded || 'wh_eff ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [2] query families (top 2000/day by exec time) --------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DATE(START_TIME) AS DAY,
                       QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);
            loaded := loaded || 'qfam ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_QUERY_FAMILY_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [3] role-hour fact -------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.ROLE_NAME = s.ROLE_NAME AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC)
            VALUES (s.HOUR_TS, s.ROLE_NAME, s.WAREHOUSE_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.EXEC_SEC);
            loaded := loaded || 'role_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_ROLE_HOURLY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [4] schema-hour fact -----------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.DATABASE_NAME = s.DATABASE_NAME AND t.SCHEMA_NAME = s.SCHEMA_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS, QUEUED_SEC, SPILL_GB, P95_S)
            VALUES (s.HOUR_TS, s.DATABASE_NAME, s.SCHEMA_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.QUEUED_SEC, s.SPILL_GB, s.P95_S);
            loaded := loaded || 'schema_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [4b] tag coverage by user, day grain (v4.14 tuning trio) --------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY t
            USING (
                SELECT g.DAY, g.USER_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(g.USER_NAME) AS COMPANY,
                       g.QUERIES, g.EXEC_SEC, g.UNTAGGED_EXEC_SEC
                FROM (
                    SELECT DATE(START_TIME) AS DAY,
                           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                           COUNT(*) AS QUERIES,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC,
                           ROUND(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL,
                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2
                ) g
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES,
                EXEC_SEC = s.EXEC_SEC, UNTAGGED_EXEC_SEC = s.UNTAGGED_EXEC_SEC,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, COMPANY, QUERIES, EXEC_SEC, UNTAGGED_EXEC_SEC)
            VALUES (s.DAY, s.USER_NAME, s.COMPANY, s.QUERIES, s.EXEC_SEC, s.UNTAGGED_EXEC_SEC);
            loaded := loaded || 'tagcov ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TAG_COVERAGE_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [5] cost allocation (exec-time share of each warehouse-hour) -------
        BEGIN
            CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE AS
            WITH wh AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       SUM(CREDITS_USED) AS HOUR_CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2
            ),
            q AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       USER_NAME, COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND WAREHOUSE_NAME IS NOT NULL AND COALESCE(EXECUTION_TIME, 0) > 0
                GROUP BY 1, 2, 3, 4, 5, 6
            ),
            tot AS (
                SELECT HOUR_TS, WAREHOUSE_NAME, SUM(EXEC_MS) AS TOTAL_MS FROM q GROUP BY 1, 2
            )
            SELECT DATE(q.HOUR_TS) AS DAY, q.WAREHOUSE_NAME, q.USER_NAME, q.ROLE_NAME,
                   q.DATABASE_NAME, q.SCHEMA_NAME, q.EXEC_MS,
                   wh.HOUR_CREDITS * q.EXEC_MS / NULLIF(tot.TOTAL_MS, 0) AS ALLOC_CREDITS
            FROM q
            JOIN tot ON tot.HOUR_TS = q.HOUR_TS AND tot.WAREHOUSE_NAME = q.WAREHOUSE_NAME
            JOIN wh ON wh.HOUR_TS = q.HOUR_TS AND wh.WAREHOUSE_NAME = q.WAREHOUSE_NAME;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t
            USING (
                SELECT DAY, 'USER' AS DIMENSION, USER_NAME AS KEY_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'DATABASE', DATABASE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'SCHEMA', DATABASE_NAME || '.' || SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3, DATABASE_NAME
                UNION ALL
                SELECT DAY, 'ROLE', ROLE_NAME,
                       CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END,
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
            ) s
            ON t.DAY = s.DAY AND t.DIMENSION = s.DIMENSION AND t.KEY_NAME = s.KEY_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, ALLOC_CREDITS = s.ALLOC_CREDITS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC)
            VALUES (s.DAY, s.DIMENSION, s.KEY_NAME, s.COMPANY, s.ALLOC_CREDITS, s.EXEC_SEC);
            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [6] task graphs -----------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY t
            USING (
                WITH runs AS (
                    SELECT COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
                           MIN_BY(h.NAME, h.QUERY_START_TIME) AS PIPELINE,
                           MIN_BY(h.DATABASE_NAME, h.QUERY_START_TIME) AS DATABASE_NAME,
                           MIN_BY(h.SCHEMA_NAME, h.QUERY_START_TIME) AS SCHEMA_NAME,
                           DATE(MIN(h.QUERY_START_TIME)) AS DAY,
                           COUNT(*) AS TASK_RUNS,
                           COUNT_IF(h.STATE = 'FAILED') AS FAILED_TASKS,
                           DATEDIFF('second', MIN(h.QUERY_START_TIME), MAX(h.COMPLETED_TIME)) AS WALL_SEC,
                           SUM(COALESCE(a.CREDITS, 0)) AS CREDITS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    LEFT JOIN (
                        SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND QUERY_ID IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY QUERY_ID
                    ) a ON a.QUERY_ID = h.QUERY_ID
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND h.STATE IN ('SUCCEEDED', 'FAILED')
                    GROUP BY RUN_KEY
                )
                SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
                       COUNT(*) AS GRAPH_RUNS,
                       COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
                       SUM(TASK_RUNS) AS TASK_RUNS,
                       ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
                       ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
                       ROUND(SUM(CREDITS), 4) AS WH_CREDITS
                FROM runs GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.PIPELINE = s.PIPELINE
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET GRAPH_RUNS = s.GRAPH_RUNS,
                RUNS_WITH_FAILURES = s.RUNS_WITH_FAILURES, TASK_RUNS = s.TASK_RUNS,
                AVG_WALL_SEC = s.AVG_WALL_SEC, P95_WALL_SEC = s.P95_WALL_SEC,
                WH_CREDITS = s.WH_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
                 TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
            VALUES (s.DAY, s.PIPELINE, s.DATABASE_NAME, s.SCHEMA_NAME, s.GRAPH_RUNS,
                    s.RUNS_WITH_FAILURES, s.TASK_RUNS, s.AVG_WALL_SEC, s.P95_WALL_SEC, s.WH_CREDITS);
            loaded := loaded || 'graphs ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

            INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
                (EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID)
            SELECT RAISED_AT, 'ALERT', COMPANY, SEVERITY, LEFT(TITLE, 300), EVENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
            UNION ALL
            SELECT QUERY_START_TIME, 'TASK_FAIL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE QUERY_START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'
            UNION ALL
            SELECT START_TIME, 'DDL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'INFO', LEFT(QUERY_TYPE || ' by ' || USER_NAME || ' (' || COALESCE(ROLE_NAME, '?') || ')', 300), QUERY_ID
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                                 'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
            UNION ALL
            SELECT CHANGE_SEEN_AT, 'WH_CHANGE', COMPANY, 'INFO',
                   LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' || COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 300),
                   CHANGE_ID
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            loaded := loaded || 'timeline ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();
        END;

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN

        -- [7] security posture ------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
            USING (
                SELECT CURRENT_DATE() AS DAY, 'EXPIRING_CRED_10D' AS METRIC, 'ALL' AS COMPANY,
                       COUNT(*)::NUMBER(18,2) AS VALUE
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL
                  AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'EXPIRED_CRED', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()
                UNION ALL
                SELECT CURRENT_DATE(), 'ADMIN_STMTS_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                UNION ALL
                SELECT CURRENT_DATE(), 'GRANT_CHANGES_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                      WHERE q.START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND U.DISABLED = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'BREAKGLASS_GRANTS_30D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                  AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
            ) s
            ON t.DAY = s.DAY AND t.METRIC = s.METRIC AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, METRIC, COMPANY, VALUE)
            VALUES (s.DAY, s.METRIC, s.COMPANY, s.VALUE);
            loaded := loaded || 'posture ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_SECURITY_POSTURE_DAILY - other marts unaffected', CURRENT_ROLE();
        END;

        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                SELECT c.USAGE_TIME::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM (
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                    UNION ALL
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI'
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                ) c
                LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u ON u.USER_ID = c.USER_ID
                GROUP BY 1, 2, 3
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, SOURCE, MODEL_NAME, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_code ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (code views) - other marts unaffected', CURRENT_ROLE();
        END;

        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                SELECT START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_TIMESTAMP())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, SOURCE, MODEL_NAME, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_functions ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', CURRENT_ROLE();
        END;

    END IF;

    RETURN 'V27 marts loaded (' || :SCOPE || ', ' || :d || 'd): ' || :loaded;
END;
$$;

CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS
SELECT 'FACT_QUERY_HOURLY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT,
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0 AS HOURS_SINCE_LOAD
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
UNION ALL
SELECT 'FACT_WAREHOUSE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
UNION ALL
SELECT 'FACT_METERING_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
UNION ALL
SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
UNION ALL
SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
UNION ALL
SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
UNION ALL
SELECT 'MART_EXEC_BOARD', MAX(REFRESHED_AT), COUNT(*),
       DATEDIFF('minute', MAX(REFRESHED_AT), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
UNION ALL
SELECT 'MART_WAREHOUSE_EFFICIENCY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
UNION ALL
SELECT 'MART_QUERY_FAMILY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
UNION ALL
SELECT 'FACT_QUERY_ROLE_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
UNION ALL
SELECT 'FACT_QUERY_SCHEMA_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
UNION ALL
SELECT 'MART_COST_ALLOCATION_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
UNION ALL
SELECT 'MART_TASK_GRAPH_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
UNION ALL
SELECT 'MART_SECURITY_POSTURE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
UNION ALL
SELECT 'MART_INCIDENT_TIMELINE', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
UNION ALL
SELECT 'FACT_AI_USAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
UNION ALL
SELECT 'MART_TAG_COVERAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY;

-- First fill so the tag panel is not empty until the next task tick.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 31 AS VERSION,
       'change-impact scan v2 (tracking-bounded + ILIKE prefilter) + tag-coverage mart' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
