-- V123__exec_board_account_clock.sql
--
-- exec_board's Current-month / Current-year calendar presets keyed WINDOW_DAYS (and
-- the board's window ranges) off session/UTC CURRENT_DATE(), while EVERY other
-- calendar-month surface in the app uses the ACCOUNT clock (America/Chicago) via
-- account_today_sql()/account_month_start_sql() -- health-strip MTD, storage calendar,
-- Decision Studio quarter, and the Overview MTD pace KPI. During the ~5-6h each evening
-- that UTC has rolled past Chicago midnight (worst on month-end), the board's
-- days-into-month drifted one day from those siblings, so "Spend, current month" and the
-- MTD pace KPI on the same screen could disagree by up to a full month of dollars; and
-- the reader (query-time) vs this loader (refresh-time) could pick different WINDOW_DAYS
-- and blank the board.
--
-- Re-derived from V073 with EVERY CURRENT_DATE() replaced by the account-clock date
-- (CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE), so the stored
-- WINDOW_DAYS and the window ranges match the account_today_sql() the reader
-- (app/data/mart_sql.py exec_board) now uses. Output contract, atomic stage swap, source
-- horizons, UNKNOWN scope and the serverless/AI driver arm are otherwise byte-identical.
-- The migration tail re-runs the refresh so the board re-stamps on the account clock
-- immediately (it would otherwise self-heal on the next hourly task run).
--
-- Owner applies in Snowsight after V122. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20123, 'V123 requires V122 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 122) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD  (V073 with CURRENT_DATE -> account clock)
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
        -- V073: fixed rolling windows plus Snowsight-style calendar presets.
        -- MTD/YTD are day OFFSETS because the joins are inclusive of CURRENT_DATE.
        -- DISTINCT prevents a duplicate board when today's offset equals a fixed pill.
        SELECT DISTINCT WINDOW_DAYS
        FROM (
            SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
            UNION ALL SELECT 60 UNION ALL SELECT 90
            UNION ALL SELECT 180 UNION ALL SELECT 365
            UNION ALL
            SELECT DATEDIFF('day', DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE), CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
            UNION ALL
            SELECT DATEDIFF('day', DATE_TRUNC('year', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE), CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
        ) calendar_windows
    ),
    -- Aggregate each fact ONCE at (COMPANY, DAY[, dim]) grain; the
    -- scope-window expansion joins these small frames, never the raw facts.
    wh_daily AS (
        SELECT COMPANY, DAY, WAREHOUSE_NAME, SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -365, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
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
        WHERE DAY >= DATEADD('day', -365, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
        GROUP BY 1, 2
    ),
    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -365, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
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
        WHERE DAY >= DATEADD('day', -365, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
          AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')
        GROUP BY 1, 2, 3, 4
    ),
    wh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DAY, f.WAREHOUSE_NAME, f.CREDITS
        FROM wh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
    ),
    qh AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS,
               f.QUERIES, f.FAILED, f.QUEUED_SEC, f.SPILL_GB
        FROM qh_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
    ),
    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.RUNS, f.FAILED
        FROM tk_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
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
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
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
    -- V069 (audit C5): serverless + AI/Cortex cost drivers on their OWN panel. The
    -- warehouse arm above reads FACT_WAREHOUSE_DAILY ONLY, so a Cortex or auto-clustering
    -- line could be the account's fastest-growing cost and never reach the driver panel,
    -- while this page's KPI caption promises compute + serverless + AI. These rows go under
    -- PANEL='COST_DRIVER_SVC' -- a DISTINCT panel from the warehouse 'COST_DRIVER' -- so
    -- the warehouse drivers keep summing to the warehouse-only headline KPIs and the page's
    -- "% of warehouse compute spend" caption stays true; the app renders this as a separate
    -- table beneath the warehouse drivers. Same column contract; the kind rides in the
    -- DIMENSION label because the board has no KIND column.
    -- BASIS: this panel is BILLED $ -- CREDITS_BILLED (adjustment applied), AI/Cortex
    -- credits x :ai_credit_price and everything else x :credit_price (the two-partition
    -- dollarization of V064/V065's alert blocks, over the canonical AI predicate resolved
    -- once as sv_daily.IS_AI). The warehouse panel is operational CREDITS_TOTAL at the
    -- compute rate -- the two panels never mix bases.
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER_SVC', 'CREDITS', DRIVER_LABEL, NULL,
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

-- Re-stamp the board on the account clock immediately; the hourly task keeps it fresh.
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 123 AS VERSION,
       'Exec board on the account clock: SP_REFRESH_EXEC_BOARD re-derived so the Current-month/Current-year calendar-preset WINDOW_DAYS and window ranges use the account-tz date (America/Chicago) instead of session/UTC CURRENT_DATE(), matching the reader (mart_sql.exec_board) and every other account_today-anchored calendar-month surface. Fixes the evening month-boundary drift between the board and the MTD pace KPI. Proc only, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 123);
