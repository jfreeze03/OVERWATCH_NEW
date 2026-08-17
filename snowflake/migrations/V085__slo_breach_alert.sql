-- V085__slo_breach_alert.sql
--
-- SLO-breach proactive alert (CoCo Control Room #16, the evaluate+notify half).
-- v4.223.0 shipped the read half: the watchlist shows a "crossed threshold" badge
-- by reading the already-evaluated SLO objectives. This turns that visibility into
-- alerting: a new raiser proc SP_SLO_BREACH_SCAN evaluates the configured
-- SLO_OBJECTIVES against the same warehouse/task/family marts the app's slo_cockpit
-- uses, and raises a HIGH alert for each objective whose measured value is in BREACH.
--
-- Additive, idempotent:
--   * Seed a PERFORMANCE rule PERF_SLO_BREACH (HIGH, 24h) via the house
--     WHEN-NOT-MATCHED MERGE (never clobbers an operator edit).
--   * Create SP_SLO_BREACH_SCAN (a new raiser) and a task TASK_SLO_BREACH_SCAN
--     serialized AFTER the hourly mart load (fresh marts), mirroring the V075
--     security-facts task. STALE / NO_DATA objectives are excluded, so a stalled
--     loader never manufactures a breach; a breach also needs >= 5 observations so
--     the PAGE never fires off a degenerate 1-4-sample 0% (the passive watchlist
--     badge still shows the raw verdict). Deduped per objective per day, since an
--     SLO breach is a persistent condition (a daily "still breaching" reminder),
--     not a discrete event. Severity escalates to CRITICAL at >= 2x error-budget
--     burn; the whole scan is wrapped in an exception handler that logs to
--     APP_ERROR_LOG and heals on the next run instead of failing the task silently.
--
-- The scanner is NOT called at apply time (it would raise alerts) and self-corrects
-- on its next scheduled run after the hourly mart load.
--
-- Owner applies in Snowsight after V084. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20085, 'V085 requires V084 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 84) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Seed the PERFORMANCE rule (idempotent WHEN NOT MATCHED; never clobbers operator edits).
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'PERF_SLO_BREACH' AS RULE_ID, 'PERFORMANCE' AS FAMILY,
           'A configured SLO objective is in breach' AS NAME,
           TRUE AS ENABLED, 'HIGH' AS SEVERITY, 1 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- New raiser: evaluate configured objectives (the same logic slo_cockpit renders in
-- Decision Studio) and raise PERF_SLO_BREACH for each BREACH. STALE / NO_DATA are
-- excluded so a stalled mart loader never manufactures a breach.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
BEGIN
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
        WHERE ENABLED AND RULE_ID = 'PERF_SLO_BREACH'
    ),
    objectives AS (
        SELECT SLO_ID, NAME, ENTITY_TYPE, ENTITY_KEY, METRIC_KEY, COMPARATOR,
               TARGET_VALUE, ERROR_BUDGET_PCT, WINDOW_DAYS, OWNER_NAME, NOTES
        FROM DBA_MAINT_DB.OVERWATCH.SLO_OBJECTIVES
        WHERE ACTIVE
    ), warehouse_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'WAREHOUSE_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.QUERIES), 0))
                 WHEN 'WAREHOUSE_P95_SEC' THEN MAX(m.P95_S)
               END AS CURRENT_VALUE,
               SUM(m.QUERIES) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'WAREHOUSE'
         AND UPPER(m.WAREHOUSE_NAME) = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('WAREHOUSE_SUCCESS_PCT', 'WAREHOUSE_P95_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), task_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'TASK_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILED, 0)) / NULLIF(SUM(m.RUNS), 0))
                 WHEN 'TASK_P95_EXEC_SEC' THEN MAX(m.P95_EXEC_SEC)
               END AS CURRENT_VALUE,
               SUM(m.RUNS) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'TASK'
         AND UPPER(m.DATABASE_NAME || '.' || m.SCHEMA_NAME || '.' || m.TASK_NAME)
             = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('TASK_SUCCESS_PCT', 'TASK_P95_EXEC_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), family_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'QUERY_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.RUNS), 0))
                 WHEN 'QUERY_P95_SEC' THEN MAX(m.P95_S)
               END AS CURRENT_VALUE,
               SUM(m.RUNS) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'QUERY_FINGERPRINT'
         AND UPPER(TO_VARCHAR(m.QUERY_HASH)) = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('QUERY_SUCCESS_PCT', 'QUERY_P95_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), measured AS (
        SELECT * FROM warehouse_values
        UNION ALL SELECT * FROM task_values
        UNION ALL SELECT * FROM family_values
    ), evaluated AS (
        SELECT o.SLO_ID, o.NAME, o.ENTITY_TYPE, o.ENTITY_KEY, o.METRIC_KEY,
               o.COMPARATOR, o.TARGET_VALUE, o.WINDOW_DAYS, m.CURRENT_VALUE,
               COALESCE(m.SAMPLE_N, 0) AS SAMPLE_N, m.AS_OF,
               CASE
                 WHEN m.CURRENT_VALUE IS NULL THEN 'NO_DATA'
                 -- Don't PAGE on a statistically meaningless sample. SLO_OBJECTIVES are
                 -- operator-configured, so the target is respected and genuine low-traffic
                 -- breaches are NOT suppressed -- only the degenerate 1-4-observation case
                 -- that would read 0% off a single failure. The passive watchlist badge
                 -- still shows the raw verdict; the PAGE requires >= 5 observations.
                 WHEN COALESCE(m.SAMPLE_N, 0) < 5 THEN 'NO_DATA'
                 WHEN m.AS_OF < DATEADD('day', -2, CURRENT_DATE()) THEN 'STALE'
                 WHEN o.COMPARATOR = '>=' AND m.CURRENT_VALUE >= o.TARGET_VALUE THEN 'MET'
                 WHEN o.COMPARATOR = '<=' AND m.CURRENT_VALUE <= o.TARGET_VALUE THEN 'MET'
                 ELSE 'BREACH'
               END AS STATUS,
               IFF(o.METRIC_KEY ILIKE '%SUCCESS_PCT' AND m.CURRENT_VALUE IS NOT NULL,
                   ROUND(GREATEST(100 - m.CURRENT_VALUE, 0)
                         / NULLIF(o.ERROR_BUDGET_PCT, 0), 2), NULL) AS BURN_MULTIPLE
        FROM objectives o
        LEFT JOIN measured m ON m.SLO_ID = o.SLO_ID
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT c.RULE_ID, 'ALL',
           -- Escalate to CRITICAL when the objective is burning >= 2x its error budget;
           -- success-rate objectives only (latency/P95 have no burn) stay at the seeded HIGH.
           IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRITICAL', c.SEVERITY),
           LEFT('SLO breach: ' || e.NAME, 300),
           LEFT(e.METRIC_KEY || ' measured ' || ROUND(e.CURRENT_VALUE, 2) || ' vs target '
               || e.COMPARATOR || ' ' || e.TARGET_VALUE || ' over ' || e.WINDOW_DAYS
               || 'd on ' || e.ENTITY_TYPE || ' ' || e.ENTITY_KEY
               || ' (' || e.SAMPLE_N || ' obs'
               || IFF(e.BURN_MULTIPLE IS NOT NULL, ', burn ' || e.BURN_MULTIPLE || 'x', '')
               || ', as of ' || e.AS_OF || '). Review in Decision Studio -> SLOs.', 2000),
           e.CURRENT_VALUE,
           c.RULE_ID || '|' || e.SLO_ID || '|' || TO_VARCHAR(CURRENT_DATE())
    FROM cfg c, evaluated e
    WHERE e.STATUS = 'BREACH'
    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );
    RETURN 'slo breach scan v1: ' || SQLROWCOUNT || ' new SLO-breach alert(s)';
EXCEPTION
    WHEN OTHER THEN
        -- One broken scan logs and returns instead of failing the task silently; the
        -- next scheduled run heals (house pattern: SP_ALERT_SCAN's per-arm handlers).
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'SloBreachScan', 'scan_failed', :emsg, 'SP_SLO_BREACH_SCAN', CURRENT_ROLE();
        RETURN 'slo breach scan failed: ' || :emsg;
END;
$$;

-- Serialize behind the hourly mart load so it evaluates fresh marts, instead of a
-- sibling that could race the loader. Same task-graph law as V075: suspend the
-- root, create the child AFTER the mart task, resume child + root, re-enable.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN
    WAREHOUSE = WH_ALFA_ADMIN
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN();
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SLO_BREACH_SCAN RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- The scanner is NOT fired at apply time -- it would raise alerts -- and
-- self-corrects on its next scheduled run after the hourly mart load.

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 85 AS VERSION,
       'SLO-breach proactive alert (CoCo Control Room #16, evaluate+notify half): seeds a PERFORMANCE rule PERF_SLO_BREACH and adds SP_SLO_BREACH_SCAN, a new raiser that evaluates the configured SLO_OBJECTIVES against the warehouse/task/family marts (the same logic the app slo_cockpit renders) and raises a HIGH alert per objective in BREACH (CRITICAL at >= 2x error-budget burn). STALE / NO_DATA and sub-5-observation samples excluded so a stalled or thin loader never manufactures a page; deduped per objective per day (a persistent-breach reminder); exception-isolated to APP_ERROR_LOG. Runs from a new task serialized after the hourly mart load; the scanner is not fired at apply time and self-corrects on its schedule.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 85);
