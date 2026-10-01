-- backfill_365.sql — OPT-IN, one-time: load a year of DAILY facts before
-- ACCOUNT_USAGE ages out (365-day retention). Unlocks YoY/QoQ comparisons
-- and materially improves the seasonal/ML forecast engines.
--
-- Idempotent by construction: each INSERT only takes days OLDER than what
-- the fact already holds, so re-running inserts nothing. Aggregations are
-- copies of the standing loaders' logic (SP_LOAD_DAILY_FACTS /
-- SP_LOAD_HOURLY_FACTS) — keep them in sync if a loader changes.
-- FACT_QUERY_HOURLY is deliberately NOT backfilled: a year at hourly x
-- warehouse x database x user grain is large and low-value vs the dailies.
-- Run as a role that can read SNOWFLAKE.ACCOUNT_USAGE and write the schema.
-- Expect a few minutes on WH_ALFA_ADMIN.

INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
    (DAY, SERVICE_TYPE, CREDITS_COMPUTE, CREDITS_CLOUD_SVCS, CREDITS_ADJUSTMENT, CREDITS_USED, CREDITS_BILLED)
SELECT
    USAGE_DATE,
    UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')),
    SUM(COALESCE(CREDITS_USED_COMPUTE, 0)),
    SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)),
    SUM(COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)),
    SUM(COALESCE(CREDITS_USED, 0)),
    SUM(COALESCE(CREDITS_BILLED,
        GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0))))
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
  AND USAGE_DATE < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY),
                            CURRENT_DATE())
GROUP BY 1, 2;

INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
    (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_COMPUTE, CREDITS_TOTAL)
SELECT
    DATE(START_TIME),
    WAREHOUSE_NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME),
    SUM(COALESCE(CREDITS_USED_COMPUTE, CREDITS_USED)),
    SUM(COALESCE(CREDITS_USED, 0))
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= DATEADD('day', -365, CURRENT_DATE())
  AND WAREHOUSE_ID > 0
  AND DATE(START_TIME) < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY),
                                  CURRENT_DATE())
GROUP BY 1, 2, 3;

INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
    (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
WITH task_attempts AS (
    -- Retry dedup (mirrors the standing loader SP_LOAD_DAILY_FACTS, fixed in V101): collapse task
    -- auto-retries to the TERMINAL attempt of each scheduled run so RUNS/FAILED count scheduled runs,
    -- not attempts. Without this, a task that FAILED on attempt 1 then SUCCEEDED on retry (two
    -- TASK_HISTORY rows sharing SCHEDULED_TIME) was backfilled as RUNS=2/FAILED=1, inflating the
    -- historical year with phantom failures vs the collapsed trailing days (data-loader-hunt 2026-08-30).
    SELECT DATABASE_NAME, SCHEMA_NAME, NAME, QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE QUERY_START_TIME >= DATEADD('day', -365, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
)
SELECT
    DATE(QUERY_START_TIME),
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
    COUNT(*),
    SUM(IFF(STATE = 'FAILED', 1, 0)),
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),
    MAX_BY(STATE, QUERY_START_TIME),
    MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)
FROM task_attempts
WHERE DATE(QUERY_START_TIME) < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY),
                                        CURRENT_DATE())
GROUP BY 1, 2, 3, 4, 5;

INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
    (DAY, USER_NAME, COMPANY, LOGINS, FAILED_LOGINS, PASSWORD_LOGINS)
SELECT
    DATE(EVENT_TIMESTAMP),
    USER_NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME),
    COUNT(*),
    SUM(IFF(IS_SUCCESS = 'NO', 1, 0)),
    SUM(IFF(FIRST_AUTHENTICATION_FACTOR = 'PASSWORD' AND IS_SUCCESS = 'YES', 1, 0))
FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
WHERE EVENT_TIMESTAMP >= DATEADD('day', -365, CURRENT_DATE())
  AND DATE(EVENT_TIMESTAMP) < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY),
                                       CURRENT_DATE())
GROUP BY 1, 2, 3;

INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
    (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
SELECT
    USAGE_DATE,
    DATABASE_NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
    AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
    AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
  AND USAGE_DATE < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY),
                            CURRENT_DATE())
GROUP BY 1, 2, 3;

-- Verify: earliest day per fact should now be ~a year back.
SELECT 'FACT_METERING_DAILY' AS FACT, MIN(DAY) AS EARLIEST, COUNT(*) AS ROWS_ FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
UNION ALL SELECT 'FACT_WAREHOUSE_DAILY', MIN(DAY), COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
UNION ALL SELECT 'FACT_TASK_DAILY', MIN(DAY), COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
UNION ALL SELECT 'FACT_LOGIN_DAILY', MIN(DAY), COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
UNION ALL SELECT 'FACT_STORAGE_DAILY', MIN(DAY), COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY;

-- ---------------------------------------------------------------------------
-- V027 mart family backfill (same loader codepath as the tasks — one truth).
-- QUERY_HISTORY-derived marts see at most ~365d. The two high-cardinality
-- marts (query families, cost allocation) default to 90d here on purpose:
-- widen deliberately if you accept the scan cost.
-- V041: the hourly marts read OW_QH_EXTRACT, so a wide backfill fills the
-- extract FIRST with the same window. B12: TASK_LOAD_HOURLY is SUSPENDed
-- across this whole extract-fed section (see the ALTER TASK below) so its
-- minute-7 watermark trim cannot shrink that window before the HOURLY marts
-- read it; RESUME + re-enable dependents runs at the end, and the next
-- scheduled run then trims the extract back to its 3-day retention on its own.
-- B5/V062: the old LEAST(:d,2) cap on the extract-fed day-grain arms is lifted,
-- so 'HOURLY', 90 below now loads the full 90d (it silently loaded 2d before).
-- ---------------------------------------------------------------------------
-- V042 (r22 #1): a full year of the day-grain query fact. Same idempotent
-- only-older-days rule as the dailies above; aggregation mirrors
-- SP_LOAD_QH_EXTRACT's arm (company via the UDF OUTSIDE the aggregation).
INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
     FAILED_COUNT, ELAPSED_SEC_SUM, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
SELECT g.DAY, g.WAREHOUSE_NAME, g.DATABASE_NAME, g.USER_NAME,
       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME),
       g.QUERY_COUNT, g.FAILED_COUNT, g.ELAPSED_SEC_SUM, g.QUEUED_SEC_SUM, g.SPILL_REMOTE_GB
FROM (
    SELECT DATE(START_TIME) AS DAY,
           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
           COUNT(*) AS QUERY_COUNT,
           SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
           SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000 AS ELAPSED_SEC_SUM,
           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000 AS QUEUED_SEC_SUM,
           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -365, CURRENT_DATE())
      AND START_TIME < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY),
                                CURRENT_DATE())::TIMESTAMP_LTZ
    GROUP BY 1, 2, 3, 4
) g;

-- B12: suspend the hourly task graph before any extract-fed load. The root
-- suspend halts the whole chain (extract -> V27 marts -> ops-diag), so the
-- minute-7 TASK_LOAD_HOURLY watermark DELETE cannot trim the freshly-filled
-- 90d OW_QH_EXTRACT mid-run and destroy up to ~87d of ops-diag history.
-- !! IF THIS WORKSHEET STOPS BEFORE THE END (a timeout, Stop, any error) !!
-- run the ALTER TASK ... RESUME and SYSTEM$TASK_DEPENDENTS_ENABLE statements
-- just above this file's final verify SELECT, or snowflake/loader_chain_check.sql
-- step 0. Until you do, the WHOLE hourly graph stays suspended: no loads, no
-- alert scan, no Teams delivery (the V041 stranding class).
-- Each load below is its own guarded block (R1-231). A loader's own verdict is
-- kept: the block reads the CALL's return value (the V064 reconcile idiom,
-- RESULT_SCAN(LAST_QUERY_ID())) and its pane reads 'ok: <call> -> <verdict>'.
-- A failure verdict ('MARTS WITH ERRORS: ...' from SP_LOAD_MARTS_V27, or
-- '(extract committed: false)' from SP_LOAD_QH_EXTRACT) or an ERROR becomes a
-- 'FAILED: ...' pane plus an APP_ERROR_LOG row (PAGE 'Backfill365'), so Run
-- All still reaches the RESUME, and the LAST result pane counts the failures.
-- A statement timeout or a Stop cannot be caught that way -- hence the note
-- above. One block per CALL on purpose: a single block around all of them
-- would be ONE statement carrying the whole window against WH_ALFA_ADMIN's
-- 300s STATEMENT_TIMEOUT_IN_SECONDS.
SET backfill_started = CURRENT_TIMESTAMP();   -- scopes the failure count in the last pane to THIS run
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;

EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_QH_EXTRACT(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_QH_EXTRACT(90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_QH_EXTRACT(90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_QH_EXTRACT(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_QH_EXTRACT(90) - ' || emsg;
END;
$$;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_MARTS_V27(''HOURLY'', 90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(''HOURLY'', 90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_MARTS_V27(''HOURLY'', 90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_MARTS_V27(''HOURLY'', 90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(''HOURLY'', 90) - ' || emsg;
END;
$$;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_OPS_DIAG(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_OPS_DIAG(90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_OPS_DIAG(90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_OPS_DIAG(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_OPS_DIAG(90) - ' || emsg;
END;
$$;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_MARTS_V27(''DAILY'', 365)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(''DAILY'', 365) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_MARTS_V27(''DAILY'', 365) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_MARTS_V27(''DAILY'', 365)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(''DAILY'', 365) - ' || emsg;
END;
$$;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_PLATFORM_SCORE(120)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_PLATFORM_SCORE(120) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_PLATFORM_SCORE(120) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_PLATFORM_SCORE(120)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_PLATFORM_SCORE(120) - ' || emsg;
END;
$$;
-- V075: security detail is intentionally bounded to 90d even in the 365d pack.
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%' OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_SECURITY_FACTS(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_SECURITY_FACTS(90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_SECURITY_FACTS(90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'Backfill365', 'backfill_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_SECURITY_FACTS(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_SECURITY_FACTS(90) - ' || emsg;
END;
$$;
-- Optional full-year sweep for the cheap daily marts (wh efficiency, graphs)
-- (fill the extract to the same width first; wrap each like the blocks above):
-- CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(365);
-- CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 365);

-- B12: backfill complete — re-enable the hourly task graph. RESUME the root,
-- then SYSTEM$TASK_DEPENDENTS_ENABLE (a root RESUME does not resume children).
-- Placed after the optional 365 sweep above so that sweep, if uncommented,
-- stays inside the suspend window. The next scheduled run trims the extract.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- Verify (the LAST result pane). Both counts 0 = every load in THIS run succeeded.
--   BACKFILL_CALLS_FAILED: a guarded CALL above raised, or returned a failure
--     verdict (its own pane shows which).
--   LOADER_ARMS_FAILED: arm failures the loaders log and swallow without
--     failing the CALL -- the optional mart arms, the query-fact and
--     cloud-services arms of the extract load (PAGE ExtractLoader / MartLoader /
--     SecurityLoader, ERROR_TYPE *_failed: the V064 reconcile's filter; a
--     skipped/unavailable note is not a failure). The daily task graph is not
--     suspended, so a loader failure it logs in the same minutes counts too --
--     a real loader failure either way.
-- FAILURES names each one -- fix the cause, then re-run this file
-- (idempotent). Needs the SET above from the same worksheet session.
SELECT COUNT_IF(PAGE = 'Backfill365') AS BACKFILL_CALLS_FAILED,
       COUNT_IF(PAGE <> 'Backfill365') AS LOADER_ARMS_FAILED,
       LISTAGG(PAGE || ' ' || COALESCE(CONTEXT, '?') || ': ' || COALESCE(LEFT(ERROR_MESSAGE, 160), '?'), ' | ')
           WITHIN GROUP (ORDER BY LOGGED_AT) AS FAILURES
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE LOGGED_AT >= $backfill_started::TIMESTAMP_NTZ
  AND (PAGE = 'Backfill365'
       OR (PAGE IN ('ExtractLoader', 'MartLoader', 'SecurityLoader')
           AND ERROR_TYPE ILIKE '%_failed%'));
