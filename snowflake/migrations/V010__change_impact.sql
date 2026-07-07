-- V010__change_impact.sql — object-change performance regression tracking.
--
-- Answers: "we updated a stored procedure — did it get slower or more
-- expensive?" When a PROCEDURE or TASK changes, the daily scan freezes a
-- 14-day pre-change baseline (runs, runtime, failure rate, credits/call)
-- and compares the 14 days after the change against it. Confirmed
-- regressions raise PERF_CHANGE_REGRESSION alert events, which flow through
-- the normal alert/webhook pipeline.
--
-- Credits/call come from SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY:
-- every child statement a procedure or task runs rolls up to its CALL via
-- ROOT_QUERY_ID, so cost is measured, not estimated. If the view is not
-- available on this account the scan logs one APP_ERROR_LOG row and verdicts
-- fall back to runtime + failure rate. Idempotent.

-- One row per detected object change. DATABASE_NAME / SCHEMA_NAME are stored
-- as their own columns so every change is attributable to its schema and the
-- app's database/schema filters apply. Baselines are FROZEN at detection
-- (source history ages out of ACCOUNT_USAGE); AFTER_* refresh daily until
-- TRACKING_UNTIL.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY (
    CHANGE_ID         VARCHAR(80)   NOT NULL DEFAULT UUID_STRING() PRIMARY KEY,
    OBJECT_TYPE       VARCHAR(20)   NOT NULL,          -- PROCEDURE | TASK
    DATABASE_NAME     VARCHAR(200)  NOT NULL,
    SCHEMA_NAME       VARCHAR(200)  NOT NULL,
    OBJECT_NAME       VARCHAR(600)  NOT NULL,          -- DB.SCHEMA.NAME
    COMPANY           VARCHAR(40)   NOT NULL DEFAULT 'ALFA',
    CHANGE_SEEN_AT    TIMESTAMP_LTZ NOT NULL,
    CHANGED_BY        VARCHAR(200),
    CHANGE_DDL        VARCHAR(1000),
    BASELINE_FROM     TIMESTAMP_LTZ,                   -- freeze marker (set once)
    BASELINE_CALLS    NUMBER(12,0),
    BASELINE_FAILS    NUMBER(12,0),
    BASELINE_MEDIAN_MS NUMBER(18,2),
    BASELINE_P95_MS   NUMBER(18,2),
    BASELINE_CREDITS_PER_CALL NUMBER(18,6),
    AFTER_CALLS       NUMBER(12,0),
    AFTER_FAILS       NUMBER(12,0),
    AFTER_MEDIAN_MS   NUMBER(18,2),
    AFTER_P95_MS      NUMBER(18,2),
    AFTER_CREDITS_PER_CALL NUMBER(18,6),
    VERDICT           VARCHAR(30)   NOT NULL DEFAULT 'PENDING',
    -- PENDING (after-window still thin) | REGRESSED | IMPROVED | NEUTRAL
    -- NO_BASELINE (new or previously idle object)
    -- INSUFFICIENT_AFTER (tracking ended with too few runs to judge)
    VERDICT_DETAIL    VARCHAR(500),
    TRACKING_UNTIL    DATE,
    ALERTED           BOOLEAN       NOT NULL DEFAULT FALSE,
    LAST_EVALUATED_AT TIMESTAMP_LTZ
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'PERF_CHANGE_REGRESSION' AS RULE_ID, 'PERFORMANCE' AS FAMILY,
           'Procedure/task runs worse after a change (threshold = % increase)' AS NAME,
           TRUE AS ENABLED, 'HIGH' AS SEVERITY, 50 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    pct FLOAT;                 -- regression threshold, % increase (ALERT_CONFIG)
    min_calls FLOAT DEFAULT 5; -- both windows need this many runs for a verdict
    emsg VARCHAR;
BEGIN
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
            ON q.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
           AND q.START_TIME > r.CHANGE_SEEN_AT
           AND q.QUERY_TYPE = 'CALL'
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
            ON h.SCHEDULED_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
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
                    ON q.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
                   AND q.START_TIME > r.CHANGE_SEEN_AT
                   AND q.QUERY_TYPE = 'CALL'
                   AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0
                  WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
              ) x
              JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
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

-- Daily, after the overnight loads; dedicated app warehouse.
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CHANGE_IMPACT_SCAN
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 50 6 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CHANGE_IMPACT_SCAN RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 10 AS VERSION,
       'change impact: object-change registry, regression scan, PERF_CHANGE_REGRESSION rule' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
