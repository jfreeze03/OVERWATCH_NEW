-- V109__warehouse_change_scan_fail_token.sql
--
-- SP_WAREHOUSE_CHANGE_SCAN failure axis. The proc (defined only in V024, never re-derived) counted
-- query failures with COUNT_IF(q.EXECUTION_STATUS = 'FAILED'), but QUERY_HISTORY.EXECUTION_STATUS is
-- 'SUCCESS' / 'FAIL' / 'INCIDENT' -- never 'FAILED' -- so BASELINE_FAIL_PCT and AFTER_FAIL_PCT were a
-- constant 0, the post-change regression fail axis (AFTER >= BASELINE + 5) was permanently 0 >= 5 =
-- false, and a setting change that broke a warehouse's queries read a false all-clear ('fail 0->0%').
--
-- Re-derives SP_WAREHOUSE_CHANGE_SCAN from V024 with COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') on both
-- the baseline and after arms (matching V010 and the codebase convention; the same dead token V057
-- fixed in SP_LOAD_MARTS_V27). Everything else byte-identical. No schema change, no task re-creation;
-- owner applies in Snowsight after V108 and the next daily TASK_WAREHOUSE_CHANGE_SCAN re-derives
-- FAIL_PCT on open tracking rows. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20109, 'V109 requires V108 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 108) THEN
        RAISE not_ready;
    END IF;
END;
$$;

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
               || ' | p95 ' || COALESCE(BASELINE_P95_S::VARCHAR, '?') || 's->'
               || COALESCE(AFTER_P95_S::VARCHAR, '?') || 's'
               || ' | queue ' || COALESCE(BASELINE_QUEUED_MIN_PER_DAY::VARCHAR, '0') || '->'
               || COALESCE(AFTER_QUEUED_MIN_PER_DAY::VARCHAR, '0') || ' min/d'
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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 109 AS VERSION,
       'SP_WAREHOUSE_CHANGE_SCAN failure axis: proc re-derived from V024 so the baseline and after query-failure counters use COUNT_IF(EXECUTION_STATUS <> ''SUCCESS'') instead of the dead = ''FAILED'' token (QUERY_HISTORY domain is SUCCESS/FAIL/INCIDENT). BASELINE_FAIL_PCT and AFTER_FAIL_PCT are no longer a constant 0, so the post-change regression fail axis can fire and a warehouse setting change that breaks queries no longer reads a false all-clear. Everything else byte-identical; no schema change, no task re-creation. Proc only; forward-healing on the next daily TASK_WAREHOUSE_CHANGE_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 109);
