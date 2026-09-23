-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  APPLY V148, V149, V150 (in order) + STEP-2 verify. THREE pending migrations.
--
--  Apply top-to-bottom as SNOW_ACCOUNTADMINS -- V148, then V149, then V150 --
--  then paste back the PART B grids. Each guards on the prior (V149 needs V148,
--  V150 needs V149), and all are idempotent (CREATE OR REPLACE / ADD COLUMN IF
--  NOT EXISTS / MERGE WHEN NOT MATCHED / SCHEMA_VERSION insert WHERE NOT EXISTS),
--  so an already-applied one no-ops and you flow into the next.
--
--  V148: re-derive SP_REFRESH_EXEC_BOARD (account clock kept) restoring the
--        CoCo/CoWork AI predicate so SNOWFLAKE_COCO_SNOWSIGHT prices at $2.20.
--  V149: ADD SESSION_ID + IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT and
--        re-derive SP_LOAD_QH_EXTRACT (from V094); tail CALL(3) reloads the extract.
--  V150: COST_CLOUD_SVC_ANOMALY per-warehouse cloud-services robust-z baseline
--        (SP_SCAN_CLOUD_SVC_ANOMALY + a CALL arm in SP_ANOMALY_SWEEP re-derived
--        from V133), replacing the fixed 10/20% ratio. Tail CALLs the scan once.
--
--  Requires V147 already applied (V148's guard). V147 confirmed applied 2026-09-21.
-- =====================================================================

-- =====================================================================
--  MIGRATION 1 of 3 -- APPLY V148 (idempotent). Source: snowflake/migrations/V148__exec_board_ai_predicate_restore_coco.sql
-- =====================================================================
-- V148__exec_board_ai_predicate_restore_coco.sql
--
-- Restore the CoCo/CoWork AI-rate broadening on the exec board's cost-driver panel.
--
-- V079 broadened SP_REFRESH_EXEC_BOARD's sv_daily AI predicate (both the IS_AI flag and the
-- DRIVER_LABEL prefix) to include '%COCO%' / '%COWORK%', because Cortex Code / CoWork bills to
-- this account under SERVICE_TYPE 'SNOWFLAKE_COCO_SNOWSIGHT', which matches NONE of the narrow
-- terms ('%CORTEX%','AI%','%INTELLIGENCE%'). That broadening is the canonical AI predicate the
-- app applies everywhere else (app/data/common.ai_service_predicate + AI_SERVICE_TOKENS, and the
-- still-broad SP_LOAD_PLATFORM_SCORE / SP_ALERT_SCAN* procs).
--
-- V123 later re-derived SP_REFRESH_EXEC_BOARD from the PRE-V079 ancestor V073 to move the calendar
-- windows onto the account clock, and SILENTLY dropped the V079 broadening (its header only claimed
-- "V073 with CURRENT_DATE -> account clock", "otherwise byte-identical"). Since V123 is the live
-- definition of the proc, MART_EXEC_BOARD has since priced CoCo/CoWork credits at the COMPUTE rate
-- ($3.68) instead of the AI rate ($2.20) and labeled them 'Serverless:' instead of 'AI/Cortex:' on
-- the Overview cost-driver panel (app/ui/pages/overview.py COST_DRIVER_SVC) -- a ~1.67x overstatement
-- of that line and a live cross-page mismatch with the Cost > Spend & Attribution page, which prices
-- the same credits at the AI rate.
--
-- This re-derives SP_REFRESH_EXEC_BOARD from V123 (KEEPING every CONVERT_TIMEZONE account-clock
-- change) and RESTORES `OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%'` in both
-- sv_daily predicates, so IS_AI/DRIVER_LABEL match ai_service_predicate() again. Output contract,
-- windows, scopes, atomic stage swap and freshness stamp are byte-identical to V123. The tail
-- re-runs the refresh so the board re-stamps with the corrected rate immediately (it would otherwise
-- self-heal on the next hourly task). Proc only, no schema change.
--
-- Owner applies in Snowsight after V147. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20148, 'V148 requires V147 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 147) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD  (V123 with the V079 CoCo/CoWork AI predicate restored)
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
    -- V148: restore V079's CoCo/CoWork broadening (dropped by V123's V073 re-derivation) so
    -- SNOWFLAKE_COCO_SNOWSIGHT (Cortex Code / CoWork) prices at the AI rate and labels 'AI/Cortex:',
    -- matching app/data/common.ai_service_predicate() and every other AI-rate surface.
    sv_daily AS (
        SELECT DAY, SERVICE_TYPE,
               (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') AS IS_AI,
               IFF((SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%'), 'AI/Cortex: ', 'Serverless: ') || SERVICE_TYPE AS DRIVER_LABEL,
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

-- Re-stamp the board with the corrected AI predicate immediately; the hourly task keeps it fresh.
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 148 AS VERSION,
       'Exec board AI-predicate restore: SP_REFRESH_EXEC_BOARD re-derived from V123 (account clock kept) with V079''s CoCo/CoWork broadening restored in both sv_daily predicates (IS_AI + DRIVER_LABEL), so SNOWFLAKE_COCO_SNOWSIGHT prices at the AI rate and labels AI/Cortex on the COST_DRIVER_SVC panel, matching ai_service_predicate() and every other AI-rate surface. V123 had silently dropped it when re-derived from V073. Proc only, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 148);

-- =====================================================================
--  MIGRATION 2 of 3 -- APPLY V149 (idempotent; GUARDS on V148). Source: snowflake/migrations/V149__qh_extract_session_client_columns.sql
-- =====================================================================
-- V149__qh_extract_session_client_columns.sql
--
-- Foundation for cloud-services driver / application attribution (Phase 1). Adds SESSION_ID
-- and IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT so a later phase can (a) join
-- ACCOUNT_USAGE.SESSIONS on SESSION_ID to attribute metadata chatter to a client
-- application / driver, and (b) separate platform/driver-issued statements from
-- user-authored ones. Both columns already exist on ACCOUNT_USAGE.QUERY_HISTORY; nothing
-- reads them yet (the reader panel is the next app-side step), so this is purely additive.
--
-- Re-derives SP_LOAD_QH_EXTRACT from its CURRENT definition -- V094, NOT V055: the loader
-- was re-derived across V041/V042/V055/V056/V062/V094, so an older base would silently
-- drop the intervening changes -- byte-identically plus two edits appending the two columns
-- to the extract INSERT column list and SELECT. Every other arm is untouched. The tail
-- CALL(3) reloads the 72h extract so the columns carry values immediately.
--
-- ALTER ... ADD COLUMN IF NOT EXISTS is idempotent; the proc re-derivation and CALL are
-- safe to re-run. Owner applies in Snowsight after V148. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20149, 'V149 requires V148 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 148) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Additive columns on the single-scan staging copy (names mirror ACCOUNT_USAGE.QUERY_HISTORY
-- so the SELECT below fills them directly). Idempotent.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS SESSION_ID NUMBER;
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS IS_CLIENT_GENERATED_STATEMENT BOOLEAN;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo TIMESTAMP_NTZ;  -- reload lower bound
    d INT;
    emsg VARCHAR;
    ok BOOLEAN DEFAULT FALSE;  -- r22 #7: extract arm committed this cycle
BEGIN
    -- DAYS_BACK > 0 = explicit backfill window; 0 or NULL = watermark mode.
    -- The tasks pass 0 (never a bare NULL — no signature-resolution
    -- questions on any runtime).
    IF (COALESCE(DAYS_BACK, 0) > 0) THEN
        d := GREATEST(1, LEAST(DAYS_BACK, 400))::INT;
        lo := DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_NTZ;
    ELSE
        -- watermark - 45 min (ACCOUNT_USAGE lag overlap), first run 48h,
        -- catch-up clamped at the 3-day retention (wider gaps: backfill).
        SELECT GREATEST(
                   COALESCE(DATEADD('minute', -45, MAX(WM_TS)),
                            DATEADD('hour', -48, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ),
                   DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
          INTO :lo
        FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
        WHERE SOURCE = 'QH_EXTRACT';
    END IF;

    -- The one QUERY_HISTORY scan of the hourly cycle. Retention trim rides
    -- the same DELETE; an explicit backfill keeps its wider window until the
    -- next watermark-mode run trims back to 3 days. Both arms carry V017
    -- isolation (v4.36.1): a failed extract fill must not fail the task —
    -- the facts keep their last load and the freshness labels say so.
    -- r22 #7: the arm is one TRANSACTION — a failed INSERT rolls the DELETE
    -- back (no hole; consumers really do read the previous fill) and the
    -- watermark below only advances on COMMIT.
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
     WHERE START_TIME >= :lo
        OR START_TIME < LEAST(:lo, DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ);

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        (QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
         USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE,
         TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME, QUEUED_OVERLOAD_TIME,
         QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE, BYTES_SCANNED,
         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT,
         CREDITS_USED_CLOUD_SERVICES, SESSION_ID, IS_CLIENT_GENERATED_STATEMENT)
    SELECT QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
           USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE::VARCHAR,
           LEFT(ERROR_MESSAGE, 200), TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME,
           QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE,
           BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH,
           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0),
           SESSION_ID, IS_CLIENT_GENERATED_STATEMENT
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= :lo;
    COMMIT;
    ok := TRUE;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'extract_load_failed', :emsg, 'OW_QH_EXTRACT - consumers read the previous fill', CURRENT_ROLE();
    END;

    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()));

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        (HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    SELECT
        DATE_TRUNC('hour', START_TIME),
        WAREHOUSE_NAME,
        DATABASE_NAME,
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME),
        COUNT(*),
        SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)),
        SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95),
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3)
    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    WHERE START_TIME >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))
    GROUP BY 1, 2, 3, 4, 5;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_HOURLY - extract unaffected', CURRENT_ROLE();
    END;

    -- r22 #1: the day-grain query fact — same dims as the hourly fact, 1/24th
    -- the rows, backfillable a full year (backfill_365.sql owns history; this
    -- arm keeps the trailing 3 days current). Company via the UDF on a plain
    -- column OUTSIDE the aggregation (V030 shape law). 'FAIL' matches the
    -- V002 hourly-fact convention.
    BEGIN
    BEGIN TRANSACTION;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY t
    USING (
        SELECT g.DAY, g.WAREHOUSE_NAME, g.DATABASE_NAME, g.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
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
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            -- Day-aligned (audit #6): only WHOLE days inside the 72h extract,
            -- so an aging day freezes COMPLETE, not at its last partial hour.
            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())
            GROUP BY 1, 2, 3, 4
        ) g
    ) s
    ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
       AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
    WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERY_COUNT = s.QUERY_COUNT,
        FAILED_COUNT = s.FAILED_COUNT, ELAPSED_SEC_SUM = s.ELAPSED_SEC_SUM,
        QUEUED_SEC_SUM = s.QUEUED_SEC_SUM, SPILL_REMOTE_GB = s.SPILL_REMOTE_GB,
        LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.COMPANY, s.QUERY_COUNT,
            s.FAILED_COUNT, s.ELAPSED_SEC_SUM, s.QUEUED_SEC_SUM, s.SPILL_REMOTE_GB);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- V055: cloud-services breakdown mart, from the extract just filled.
    -- Isolated (V017): its failure must not break the extract or the
    -- watermark — consumers keep the previous mart fill.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'cloud_svc_mart_failed', :emsg, 'MART_CLOUD_SVC_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- R5: advance the watermark; R6: loader-owned freshness — ONLY when the
    -- extract arm committed (r22 #7: a failed cycle must re-cover its window).
    IF (ok) THEN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'QH_EXTRACT' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OW_QH_EXTRACT' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        UNION ALL
        SELECT 'FACT_QUERY_HOURLY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        UNION ALL
        SELECT 'FACT_QUERY_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');
    END IF;

    RETURN 'qh extract + query facts loaded (extract committed: ' || :ok || ')';
END;
$$;

-- Reload the whole 72h extract so SESSION_ID / IS_CLIENT_GENERATED_STATEMENT carry values
-- immediately (a plain watermark-mode hourly run would only fill them from the watermark
-- forward, leaving the rest of the retention window NULL until it ages out).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 149 AS VERSION,
       'OW_QH_EXTRACT session/client columns: ADD COLUMN SESSION_ID + IS_CLIENT_GENERATED_STATEMENT (both on ACCOUNT_USAGE.QUERY_HISTORY) to the single-scan staging copy, and re-derive SP_LOAD_QH_EXTRACT from V094 (the current definition; the loader was re-derived across V041/V042/V055/V056/V062/V094) so the extract INSERT/SELECT fill them -- byte-identical otherwise. Foundation for cloud-services driver/application attribution (join ACCOUNT_USAGE.SESSIONS on SESSION_ID; split client-generated vs user statements); nothing reads them yet. Tail CALL(3) reloads the 72h extract so the columns populate immediately. Additive schema change, no backfill.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 149);

-- =====================================================================
--  MIGRATION 3 of 3 -- APPLY V150 (idempotent; GUARDS on V149). Source: snowflake/migrations/V150__cloud_svc_anomaly_baseline.sql
-- =====================================================================
-- V150__cloud_svc_anomaly_baseline.sql
--
-- COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline (Cloud Services Driver Intelligence,
-- Phase 2). Replaces the fixed 10/20% cloud-services RATIO threshold (COST_CLOUD_SVC_RATIO) as the
-- primary "is this warehouse's cloud services a problem" signal with a PER-WAREHOUSE robust-z
-- baseline: a chronically compile-heavy discovery warehouse sits in-baseline instead of re-alerting
-- every day, while a genuine step-change fires. Same median/MAD modified-z engine (0.6745 / 0.7979
-- fallback, 28-day window) as the COST_ANOMALY_SWEEP arm and app/logic/anomaly.robust_zscores,
-- sourced from MART_CLOUD_SVC_DAILY (gross CS credits, per family x warehouse x day). Materiality is
-- a CS-CREDIT VOLUME floor, NOT the $50 compute floor (cloud-services credits are tiny). CS is GROSS
-- usage, never billable.
--
-- Adds SP_SCAN_CLOUD_SVC_ANOMALY + its COST_CLOUD_SVC_ANOMALY ALERT_CONFIG rule (COST / MEDIUM), and
-- a CALL arm in SP_ANOMALY_SWEEP (re-derived from V133) inside the standard per-arm EXCEPTION guard --
-- so it rides the existing daily TASK_ANOMALY_SWEEP cadence with no new task, and a failure can't
-- break the cost/volume/DQ arms. Internal mart reads only, so the rule ships ENABLED. Owner applies in
-- Snowsight after V149; the trailing CALL books any current step-change immediately. This file never
-- runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20150, 'V150 requires V149 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 149) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_CLOUD_SVC_ANOMALY', 'COST', 'Cloud-services credits per warehouse: robust-z step-change vs the 28d baseline, replacing the fixed 10/20% ratio', TRUE, 'MEDIUM', 3.5, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

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
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;
    IF (:zthr IS NULL) THEN
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
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    -- Dynamic-table refresh failures (guarded: accounts without the view
    -- keep the sweep's cost half working).
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID,
               IFF(d.DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA'),
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
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                  '.' || d.NAME || '|' || TO_VARCHAR(CURRENT_DATE())
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
                   '. Breakdown: Admin > Org spend.',
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
        SELECT c.RULE_ID,
               IFF(v.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
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
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL ||
                  '|' || TO_VARCHAR(CURRENT_DATE())
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
        SELECT c.RULE_ID,
               IFF(s.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
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
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)
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
        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
          INTO :ai_model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
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

-- Run the new scan once so any current step-change books immediately (the daily TASK_ANOMALY_SWEEP
-- picks it up going forward). Only the CS arm -- not the full sweep -- to avoid the Cortex pre-explain
-- AI cost at apply time; the arm is exception-guarded there and here.
CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 150 AS VERSION,
       'COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline (CS driver intelligence, Phase 2): SP_SCAN_CLOUD_SVC_ANOMALY books one alert per (warehouse, day) whose gross CS credits from MART_CLOUD_SVC_DAILY are a robust-z (0.6745 / 0.7979 fallback, 28d) outlier vs the warehouse own prior baseline -- the per-entity replacement for the fixed 10/20% CS-ratio threshold so a chronically compile-heavy warehouse stays in-baseline while a step-change fires. CS-credit VOLUME materiality floor (not the $50 compute floor; CS credits are tiny). Adds the COST_CLOUD_SVC_ANOMALY ALERT_CONFIG rule (COST / MEDIUM, ENABLED) + a CALL arm in SP_ANOMALY_SWEEP (re-derived from V133) inside the standard EXCEPTION guard, riding the existing daily TASK_ANOMALY_SWEEP cadence (no new task). Proc otherwise byte-identical. CS credits are gross usage, never billable.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 150);

-- =====================================================================
--  PART B -- STEP-2 VERIFY (read-only). Paste all grids back.
-- =====================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- ---- V148 checks -----------------------------------------------------
-- (V148.1) the live proc carries the CoCo/CoWork broadening (both TRUE).
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()'), '%COCO%')   AS HAS_COCO,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()'), '%COWORK%') AS HAS_COWORK;

-- (V148.2) CoCo/CoWork lines on the board price at the AI rate (~2.20/credit). EMPTY = no CoCo spend in-window.
SELECT COMPANY, WINDOW_DAYS, DIMENSION, ROUND(VALUE, 2) AS CREDITS, VALUE_USD,
       ROUND(VALUE_USD / NULLIF(VALUE, 0), 3) AS IMPLIED_RATE
FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
WHERE PANEL = 'COST_DRIVER_SVC' AND (DIMENSION ILIKE '%COCO%' OR DIMENSION ILIKE '%COWORK%')
ORDER BY WINDOW_DAYS, VALUE DESC;

-- ---- V149 checks -----------------------------------------------------
-- (V149.1) the two columns now exist on OW_QH_EXTRACT (SESSION_ID = NUMBER, IS_CLIENT_GENERATED_STATEMENT = BOOLEAN).
DESCRIBE TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT;

-- (V149.2) the CALL(3) reload populated them (WITH_SESSION ~= TOTAL; CLIENT_GEN counts driver/UI rows).
SELECT COUNT(*) AS TOTAL_ROWS, COUNT(SESSION_ID) AS WITH_SESSION,
       COUNT_IF(IS_CLIENT_GENERATED_STATEMENT) AS CLIENT_GEN_ROWS,
       COUNT(DISTINCT SESSION_ID) AS SESSIONS
FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT;

-- ---- V150 checks -----------------------------------------------------
-- (V150.1) the new rule is seeded + enabled (THRESHOLD_NUM is the robust-z threshold).
SELECT RULE_ID, FAMILY, ENABLED, SEVERITY, THRESHOLD_NUM
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY';

-- (V150.2) the sweep now carries the CS-anomaly arm (both TRUE = arm wired, schema-drift arm intact).
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'SP_SCAN_CLOUD_SVC_ANOMALY') AS HAS_CS_ARM,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()'), 'SP_SCAN_SCHEMA_DRIFT')      AS HAS_DRIFT_ARM;

-- (V150.3) any CS anomalies booked by the tail scan (EMPTY = every warehouse is in-baseline, which is fine).
SELECT COMPANY, TITLE, ROUND(METRIC_VALUE, 1) AS ROBUST_Z, RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY'
ORDER BY RAISED_AT DESC LIMIT 10;

-- (all) V148, V149, V150 are registered.
SELECT VERSION, DESCRIPTION, APPLIED_AT
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION IN (148, 149, 150) ORDER BY VERSION;
