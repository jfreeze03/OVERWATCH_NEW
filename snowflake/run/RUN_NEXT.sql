-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  APPLY V148 then V149 (in order) + STEP-2 verify. TWO pending migrations.
--
--  WHY TWO: V148 (exec-board CoCo/CoWork AI-rate restore) was staged but is not
--  yet confirmed applied; V149 (OW_QH_EXTRACT session/client columns) GUARDS on
--  V148 (raises -20149 if V148 is missing). Apply top-to-bottom as
--  SNOW_ACCOUNTADMINS -- V148 first, then V149 -- then paste back the PART B grids.
--  Both are idempotent (CREATE OR REPLACE / ADD COLUMN IF NOT EXISTS / SCHEMA_VERSION
--  insert WHERE NOT EXISTS), safe to re-run. If V148 is already applied its
--  guard/inserts no-op and you flow straight into V149.
--
--  V148: re-derive SP_REFRESH_EXEC_BOARD (account clock kept) restoring the
--        CoCo/CoWork AI predicate so SNOWFLAKE_COCO_SNOWSIGHT prices at $2.20 and
--        labels 'AI/Cortex:' on the Overview cost-driver panel.
--  V149: ADD SESSION_ID + IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT and
--        re-derive SP_LOAD_QH_EXTRACT (from V094, the current definition) to fill
--        them; tail CALL(3) reloads the 72h extract. Foundation for cloud-services
--        driver/application attribution; nothing reads the columns yet.
--
--  Requires V147 already applied (V148's guard). V147 confirmed applied 2026-09-21.
-- =====================================================================

-- =====================================================================
--  MIGRATION 1 of 2 -- APPLY V148 (idempotent). Source: snowflake/migrations/V148__exec_board_ai_predicate_restore_coco.sql
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
--  MIGRATION 2 of 2 -- APPLY V149 (idempotent; GUARDS on V148). Source: snowflake/migrations/V149__qh_extract_session_client_columns.sql
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
--  PART B -- STEP-2 VERIFY (read-only). Paste all grids back.
-- =====================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- ---- V148 checks -----------------------------------------------------
-- (V148.1) the live proc carries the CoCo/CoWork broadening (both TRUE).
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()'), '%COCO%')   AS HAS_COCO,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()'), '%COWORK%') AS HAS_COWORK;

-- (V148.2) CoCo/CoWork lines on the board now price at the AI rate (~2.20/credit),
--          labeled 'AI/Cortex:'. EMPTY just means no CoCo/CoWork spend in-window.
SELECT COMPANY, WINDOW_DAYS, DIMENSION,
       ROUND(VALUE, 2) AS CREDITS, VALUE_USD,
       ROUND(VALUE_USD / NULLIF(VALUE, 0), 3) AS IMPLIED_RATE
FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
WHERE PANEL = 'COST_DRIVER_SVC'
  AND (DIMENSION ILIKE '%COCO%' OR DIMENSION ILIKE '%COWORK%')
ORDER BY WINDOW_DAYS, VALUE DESC;

-- ---- V149 checks -----------------------------------------------------
-- (V149.1) the two columns now exist on OW_QH_EXTRACT (look for SESSION_ID = NUMBER
--          and IS_CLIENT_GENERATED_STATEMENT = BOOLEAN in the "type" column).
DESCRIBE TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT;

-- (V149.2) the CALL(3) reload populated them. WITH_SESSION should be ~= TOTAL (SESSION_ID
--          is set for nearly every statement); CLIENT_GEN counts platform/driver-issued rows.
SELECT COUNT(*)                                              AS TOTAL_ROWS,
       COUNT(SESSION_ID)                                     AS WITH_SESSION,
       COUNT_IF(IS_CLIENT_GENERATED_STATEMENT)               AS CLIENT_GEN_ROWS,
       COUNT_IF(NOT COALESCE(IS_CLIENT_GENERATED_STATEMENT, FALSE)) AS USER_STMT_ROWS,
       COUNT(DISTINCT SESSION_ID)                            AS SESSIONS
FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT;

-- (V149.3) a few client-generated rows (sanity: SESSION_ID + the flag look real).
SELECT SESSION_ID, IS_CLIENT_GENERATED_STATEMENT, USER_NAME, QUERY_TYPE,
       LEFT(QUERY_TEXT, 60) AS SAMPLE
FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
WHERE IS_CLIENT_GENERATED_STATEMENT = TRUE
LIMIT 5;

-- (both) V148 and V149 are registered.
SELECT VERSION, DESCRIPTION, APPLIED_AT
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION IN (148, 149)
ORDER BY VERSION;
