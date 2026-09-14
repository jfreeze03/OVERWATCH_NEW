-- V140__change_impact_exclude_self_procs.sql
--
-- Harden the change-impact regression detector so it never tracks OVERWATCH's OWN objects.
-- SP_CHANGE_IMPACT_SCAN registers every changed PROCEDURE/TASK into OBJECT_CHANGE_REGISTRY and
-- alerts when runtime/credits regress after the change. But it also registered OVERWATCH's own
-- maintenance procs -- which get CREATE OR REPLACE'd on every migration AND often run a one-time
-- apply-time backfill CALL. That one heavy call inflates the "after" p95/credits vs the daily
-- baseline, so a correctness fix (e.g. V120's SP_LOAD_PATTERN_COST fanout fix on 2026-09-02, which
-- re-stamped 90 days at apply) trips a false "PROCEDURE ... regressed after <date>" CRITICAL -- the
-- oldest-open critical dragging the responsiveness KPI. OVERWATCH's own plumbing is already
-- monitored the right way (SOURCE_FRESHNESS_STATE staleness + per-loader error logging + the OPS
-- scan-health tally), so change-impact self-tracking is pure noise. (V139's SP_LOAD_OBJECT_COST
-- used the same CREATE-OR-REPLACE + apply-time-backfill pattern, so it would have tripped the
-- identical false alert in a few days -- this prevents that too.)
--
-- Fix: re-derive SP_CHANGE_IMPACT_SCAN from V061 with a single DBA_MAINT_DB exclusion in EACH
-- registration arm (procedures 1a, tasks 1b). Everything else is byte-identical (test_v140 proves
-- proc == V061 modulo those two lines). Then one-time: resolve the open change-impact alerts for
-- DBA_MAINT_DB objects and drop their registry rows so the existing false critical clears. Proc +
-- data cleanup; no schema change. Apply AFTER V139. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20140, 'V140 requires V139 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 139) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_CHANGE_IMPACT_SCAN (from V061 + DBA_MAINT_DB self-exclusion, V140)
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
          AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own procs are self-monitored (freshness + per-loader error log), not change-impact-tracked
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
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
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
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
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

-- One-time: clear the alerts + registry rows the OLD scan raised for OVERWATCH's own objects.
-- These are false positives (see header); the hardened scan above never re-creates them.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'EXPECTED'
 WHERE STATUS IN ('OPEN', 'ACK')
   AND DEDUPE_KEY LIKE 'PERF_CHANGE_REGRESSION|DBA_MAINT_DB.%';

DELETE FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB';

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 140 AS VERSION,
       'Change-impact detector hardening: SP_CHANGE_IMPACT_SCAN no longer registers OVERWATCH''s own DBA_MAINT_DB procedures/tasks into OBJECT_CHANGE_REGISTRY (they are self-monitored via SOURCE_FRESHNESS_STATE + per-loader error logging), so a maintenance-proc redeploy plus a one-time apply-time backfill can no longer trip a false PERF_CHANGE_REGRESSION alert (e.g. SP_LOAD_PATTERN_COST after V120). Re-derived from V061 with a DBA_MAINT_DB exclusion in each registration arm; one-time resolves the open self-object change-impact alerts and drops their registry rows.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 140);
