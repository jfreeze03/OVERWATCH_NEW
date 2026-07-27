-- V054__exec_board_window_history.sql — the long windows actually read long
-- (Codex r29 #1, CONFIRMED regression in deployed V052).
--
--   V052 added 180/365 to the exec-board `windows` CTE, but the three source
--   CTEs (wh_daily/qh_daily/tk_daily) still capped at 90 days, so the 180/365
--   pills computed over only 90 days — identical to the 90-day row. This
--   widens the three source horizons to 365 (= max DAY_WINDOW_OPTIONS). The
--   sources are FACT_*_DAILY (mart, long retention), so this is history, not a
--   live rescan; the aggregate-once/join-small shape is unchanged.
--
--   Companion (Codex r29 #20): raise the SP_PURGE_FACTS daily-fact retention
--   floor 180 -> 365 so retained history always covers the max advertised
--   window (a no-op under the default 800-day setting; it only tightens the
--   guarantee). tests/test_v054_window_history.py locks BOTH: read horizon and
--   retention floor >= max window.
--
--   Two proc swaps + one board reload; no new objects. Apply AFTER V053.
--   Idempotent; safe to re-run. (Codex r29 #7 not included — already fixed in
--   V042/r22 #7; that reviewer cited the superseded V041 body.)
--
-- Derivation law: SP_REFRESH_EXEC_BOARD from V052 + three horizon edits;
-- SP_PURGE_FACTS from V045 + one floor edit; the test re-derives and
-- byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20054, 'V054 requires V053 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 53) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(VALUE)), 3.68) INTO :credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD';

    -- Build into the stage; readers keep the old board until the SWAP (the
    -- V003 DELETE+INSERT gap stranded Overview on the live fallback hourly).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE
        (COMPANY, WINDOW_DAYS, PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER)
    WITH scopes AS (
        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'
        UNION ALL SELECT 'UNKNOWN'  -- V044 (#18): the unmapped bucket is a first-class pill
    ),
    windows AS (
        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
        UNION ALL SELECT 60 UNION ALL SELECT 90
        UNION ALL SELECT 180 UNION ALL SELECT 365  -- V052: mart-history windows
    ),
    -- Aggregate each fact ONCE at (COMPANY, DAY[, dim]) grain; the
    -- scope-window expansion joins these small frames, never the raw facts.
    wh_daily AS (
        SELECT COMPANY, DAY, WAREHOUSE_NAME, SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
        GROUP BY 1, 2, 3
    ),
    qh_daily AS (
        -- r22 #1: the day fact is backfillable a year, so 14/60/90-day
        -- windows hold real totals right after a rebuild (the hourly fact
        -- only accrues from install day).
        SELECT COMPANY, DAY,
               SUM(QUERY_COUNT) AS QUERIES, SUM(FAILED_COUNT) AS FAILED,
               SUM(QUEUED_SEC_SUM) AS QUEUED_SEC, SUM(SPILL_REMOTE_GB) AS SPILL_GB
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    wh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DAY, f.WAREHOUSE_NAME, f.CREDITS
        FROM wh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    qh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS,
               f.QUERIES, f.FAILED, f.QUEUED_SEC, f.SPILL_GB
        FROM qh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.RUNS, f.FAILED
        FROM tk_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    -- One aggregation pass per source; the KPI arms below just unpivot these.
    wh_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(CREDITS) AS CREDITS
        FROM wh GROUP BY 1, 2
    ),
    qh_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(QUERIES) AS QUERIES, SUM(FAILED) AS FAILED,
               SUM(QUEUED_SEC) AS QUEUED_SEC, SUM(SPILL_GB) AS SPILL_GB
        FROM qh GROUP BY 1, 2
    ),
    tk_kpi AS (
        SELECT SCOPE_COMPANY, WINDOW_DAYS, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM tk GROUP BY 1, 2
    )
    -- KPI panel (unpivoted from the single-pass aggregates) ------------------
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'CREDITS', NULL, NULL,
           CREDITS, ROUND(CREDITS * :credit_price, 2), 'credits', 10
    FROM wh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUERIES', NULL, NULL,
           QUERIES, NULL, 'count', 20
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'FAILED_QUERIES', NULL, NULL,
           FAILED, NULL, 'count', 30
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'QUEUED_MINUTES', NULL, NULL,
           ROUND(QUEUED_SEC / 60, 1), NULL, 'minutes', 40
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'SPILL_GB', NULL, NULL,
           ROUND(SPILL_GB, 2), NULL, 'gb', 50
    FROM qh_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_RUNS', NULL, NULL,
           RUNS, NULL, 'count', 60
    FROM tk_kpi
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'KPI', 'TASK_FAILURES', NULL, NULL,
           FAILED, NULL, 'count', 70
    FROM tk_kpi
    -- Daily spend panel -------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'DAILY_SPEND', 'CREDITS', NULL, DAY,
           SUM(CREDITS), ROUND(SUM(CREDITS) * :credit_price, 2), 'credits/day', 10
    FROM wh GROUP BY 1, 2, DAY
    -- Cost drivers ------------------------------------------------------------
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER', 'CREDITS', WAREHOUSE_NAME, NULL,
           SUM(CREDITS), ROUND(SUM(CREDITS) * :credit_price, 2), 'credits', 10
    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME;

    ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
        SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_EXEC_BOARD' AS SOURCE_NAME, MAX(REFRESHED_AT) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'exec board refreshed (atomic swap)';
END;
$$;

-- >>> derived:SP_PURGE_FACTS
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    hourly_days FLOAT;
    daily_days FLOAT;
    err_days FLOAT;
    usage_days FLOAT;
    total INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_HOURLY', VALUE, NULL))), 400),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_DAILY', VALUE, NULL))), 800),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'ERROR_LOG_RETENTION_DAYS', VALUE, NULL))), 180),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'APP_USAGE_RETENTION_DAYS', VALUE, NULL))), 365)
      INTO :hourly_days, :daily_days, :err_days, :usage_days
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    hourly_days := GREATEST(hourly_days, 90);
    daily_days := GREATEST(daily_days, 365);
    err_days := GREATEST(err_days, 30);
    usage_days := GREATEST(usage_days, 90);

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
     WHERE LOGGED_AT < DATEADD('day', -1 * :err_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE
     WHERE AT < DATEADD('day', -1 * :usage_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;


    -- V042 (r22 #10): the V027 mart family and the V041 loader-pass tables
    -- join retention — same settings-driven windows, hour grain vs day grain.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
     WHERE EVENT_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;

    RETURN 'purged ' || :total || ' row(s)';
END;
$$;

-- Rebuild the board so the 180/365 pills carry real long-window data now (the
-- daily task keeps them fresh thereafter).
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 54 AS VERSION,
       'Exec-board 180/365 windows read full history (V052 capped their source CTEs at 90); daily-fact retention floor raised 180->365 so advertised windows always have source history' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 54);
