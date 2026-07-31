-- V069__exec_board_serverless_ai_drivers.sql
--
-- Audit finding C5: the Overview "Top cost drivers" panel reads the COST_DRIVER rows of
-- MART_EXEC_BOARD, and SP_REFRESH_EXEC_BOARD built those from FACT_WAREHOUSE_DAILY and
-- nothing else -- so serverless (auto-clustering, MV refresh, search optimization,
-- snowpipe, serverless tasks, SPCS...) and AI/Cortex spend could NEVER appear as a cost
-- driver, while the same page's KPI caption promises "compute, serverless, AI". A Cortex
-- or auto-clustering line could be the account's fastest-growing cost and be structurally
-- invisible on the driver panel.
--
-- SP_REFRESH_EXEC_BOARD is re-derived from its LATEST def (V054) via outputs/gen_v069.py +
-- count-asserted needle edits, gaining a second COST_DRIVER arm over FACT_METERING_DAILY --
-- the app's own daily fact, not a live ACCOUNT_USAGE scan:
--
--   * warehouse metering excluded (the existing arm covers it) with the canonical
--     COST_SERVERLESS_CREEP list, minus AI_SERVICES -- AI is the point of the arm;
--   * HOUSE RATE LAW -- AI/Cortex credits x :ai_credit_price, everything else x
--     :credit_price, the two-partition dollarization of V064/V065's alert blocks. The proc
--     had only :credit_price in scope, so its single-key SETTINGS read becomes the
--     canonical two-key IFF read (V061..V067 form); no rate is hardcoded in the SQL;
--   * CREDITS_BILLED (adjustment applied) is the base -- the same one this page's MTD /
--     Projected KPIs dollarize, so panel and KPI row agree;
--   * same window semantics as the existing arm (-365d read horizon, shared windows join);
--   * unchanged column contract, so app/ui/pages/overview.py needs NO change. The board has
--     no KIND column and no room for one, so the kind rides in the DIMENSION label:
--     'Serverless: <SERVICE_TYPE>' / 'AI/Cortex: <SERVICE_TYPE>';
--   * FACT_METERING_DAILY has no company dimension (account-level metering), so the new
--     rows are emitted for the 'ALL' pill ONLY -- fanning them across ALFA/Trexis would
--     invent an attribution the source does not carry.
--
-- The tail reloads the board so the new drivers appear immediately (the hourly task keeps
-- them current thereafter). Idempotent; apply AFTER V068. No new objects -- one proc
-- re-definition plus one board reload. Byte-verified by
-- tests/test_v069_exec_board_drivers.py; no owner smoke test required.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20069, 'V069 requires V068 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 68) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD  (serverless + AI/Cortex COST_DRIVER arm, house rate law)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
    ai_credit_price FLOAT;   -- V069: AI/Cortex credits bill at their OWN rate (house rate law)
BEGIN
    -- V069: both rates in ONE read, the canonical house form (V061..V067 alert scans).
    -- The COALESCE fallbacks mirror the V001 SETTINGS seeds; no rate is ever written
    -- into the SQL below.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :credit_price, :ai_credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

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
    -- V069 (audit C5): serverless + AI/Cortex spend, so the driver panel can show what
    -- the KPI row already counts. Source is FACT_METERING_DAILY -- the app's own daily
    -- fact (SP_LOAD_DAILY_FACTS), never a live ACCOUNT_USAGE scan -- on the SAME -365d
    -- horizon as the three arms above. Warehouse metering is excluded because wh_daily
    -- already carries it: the canonical exclusion list COST_SERVERLESS_CREEP spells in
    -- V066, minus its AI_SERVICES entry (AI is what this arm exists to surface).
    -- CREDITS_BILLED (adjustment applied) is the same base the page's MTD/Projected KPIs
    -- dollarize, so the driver panel and the KPI row agree. IS_AI evaluates the canonical
    -- AI predicate ONCE, here, so the label and the rate can never disagree.
    sv_daily AS (
        SELECT DAY, SERVICE_TYPE,
               (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') AS IS_AI,
               IFF((SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%'), 'AI/Cortex: ', 'Serverless: ') || SERVICE_TYPE AS DRIVER_LABEL,
               SUM(CREDITS_BILLED) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
          AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')
        GROUP BY 1, 2, 3, 4
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
    -- V069: the SAME windows expansion the three arms above use. There is deliberately NO
    -- scopes join -- FACT_METERING_DAILY carries no company dimension (account-level
    -- metering), so these rows are emitted for the 'ALL' pill ONLY. Fanning them across
    -- ALFA/Trexis would invent an attribution the source does not carry, and parking them
    -- in the V044 UNKNOWN pill would poison that pill's "go map this" signal with spend
    -- that can never be mapped.
    sv AS (
        SELECT 'ALL' AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DRIVER_LABEL, f.IS_AI, f.CREDITS
        FROM sv_daily f
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
    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME
    -- V069 (audit C5): serverless + AI/Cortex cost drivers. The arm above reads
    -- FACT_WAREHOUSE_DAILY ONLY, so a Cortex or auto-clustering line could be the
    -- account's fastest-growing cost and never reach the "Top cost drivers" panel, while
    -- this page's KPI caption promises compute + serverless + AI. Same column contract as
    -- the warehouse arm (the app groups COST_DRIVER by DIMENSION and sums VALUE_USD); the
    -- kind rides in the DIMENSION label because the board has no KIND column.
    -- HOUSE RATE LAW: AI/Cortex credits x :ai_credit_price, everything else x
    -- :credit_price -- the two-partition dollarization of V064/V065's alert blocks, over
    -- the canonical AI predicate resolved once as sv_daily.IS_AI.
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER', 'CREDITS', DRIVER_LABEL, NULL,
           SUM(CREDITS),
           ROUND(SUM(CASE WHEN IS_AI THEN 0 ELSE CREDITS END) * :credit_price
                 + SUM(CASE WHEN IS_AI THEN CREDITS ELSE 0 END) * :ai_credit_price, 2),
           'credits', 20
    FROM sv GROUP BY 1, 2, DRIVER_LABEL;

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

-- Rebuild the board so the serverless/AI drivers are on the panel now; the hourly
-- TASK_REFRESH_EXEC_BOARD chain keeps them fresh thereafter.
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 69 AS VERSION,
       'Exec-board serverless + AI cost drivers (audit C5): SP_REFRESH_EXEC_BOARD built COST_DRIVER rows from FACT_WAREHOUSE_DAILY alone, so serverless (auto-clustering, MV refresh, search optimization, snowpipe, serverless tasks) and AI/Cortex spend could never appear on the Overview driver panel even as the fastest-growing line - while the same page KPIs cover compute + serverless + AI. A second COST_DRIVER arm over FACT_METERING_DAILY (the app fact, not ACCOUNT_USAGE) excludes warehouse metering, prices AI/Cortex credits at AI_CREDIT_PRICE_USD and the rest at CREDIT_PRICE_USD (both read from SETTINGS - the proc gains the two-key read), keeps the same -365d/windows semantics and column contract, and labels drivers Serverless:/AI/Cortex: so the panel needs no app change. Account-level metering has no company dimension, so the rows land on the ALL scope only. Re-derived from V054; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 69);
