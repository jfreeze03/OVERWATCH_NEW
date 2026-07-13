-- V044__unknown_classification.sql — adjudication #18 (owner: "do 18").
--
--   Unknown entities stop defaulting to ALFA. Classification is now
--   evidence-based on BOTH sides:
--     warehouse: COMPANY_SCOPE row -> WH_TRXS_* list stays Trexis via rows;
--                WH_ALFA_* -> ALFA; residual -> UNKNOWN
--     database:  COMPANY_SCOPE 'DATABASE' row -> TRXS_* -> ALFA%/ADMIN ->
--                residual UNKNOWN (DBA_MAINT_DB seeded ALFA: app infra)
--     user:      USER_OVERRIDE row -> %TRXS% role -> %ALFA% or DBA role ->
--                residual UNKNOWN (SYSTEM lands here on purpose: it runs
--                both companies' work)
--   The exec board gains an UNKNOWN scope so the pill is mart-served.
--
--   HISTORY NOTE: mart rows keep the COMPANY stamped at load time. The
--   nightly reconcile re-stamps the trailing 3 days; older rows re-stamp
--   only if you re-run the backfill. Go-forward is honest immediately.
--
-- Derivation law: UDFs from V001/V019, board proc from V043, verbatim +
-- enumerated edits; tests/test_v044_unknown.py re-derives and compares.

-- >>> derived:COMPANY_FOR_WAREHOUSE
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WH VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'WAREHOUSE' AND PATTERN = UPPER(COALESCE(WH, ''))),
        -- V044 (#18): ALFA needs evidence too (WH_ALFA_* naming);
        -- anything else is UNKNOWN until a COMPANY_SCOPE row maps it.
        IFF(UPPER(COALESCE(WH, '')) LIKE 'WH!_ALFA!_%' ESCAPE '!', 'ALFA', 'UNKNOWN')
    )
$$;

-- >>> derived:COMPANY_FOR_DATABASE
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DB VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        -- V044 (#18): explicit mapping wins (SCOPE_TYPE='DATABASE' rows —
        -- DBA_MAINT_DB is seeded ALFA below: the app infra is ALFA-owned).
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'DATABASE' AND PATTERN = UPPER(COALESCE(DB, ''))),
        IFF(UPPER(COALESCE(DB, '')) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis',
        IFF(UPPER(COALESCE(DB, '')) LIKE 'ALFA%' OR UPPER(COALESCE(DB, '')) = 'ADMIN',
            'ALFA', 'UNKNOWN'))
    )
$$;

-- >>> derived:COMPANY_FOR_USER
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(U VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = UPPER(COALESCE(U, ''))),
        IFF(EXISTS (
                SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
                WHERE g.DELETED_ON IS NULL
                  AND UPPER(g.GRANTEE_NAME) = UPPER(COALESCE(U, ''))
                  AND g.ROLE ILIKE '%TRXS%'),
            'Trexis',
            -- V044 (#18): ALFA needs role evidence too (%ALFA% roles or the
            -- two DBA roles); no company-indicating role = UNKNOWN.
            IFF(EXISTS (
                    SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
                    WHERE g.DELETED_ON IS NULL
                      AND UPPER(g.GRANTEE_NAME) = UPPER(COALESCE(U, ''))
                      AND (g.ROLE ILIKE '%ALFA%'
                           OR g.ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'))),
                'ALFA', 'UNKNOWN'))
    )
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
    ),
    -- Aggregate each fact ONCE at (COMPANY, DAY[, dim]) grain; the
    -- scope-window expansion joins these small frames, never the raw facts.
    wh_daily AS (
        SELECT COMPANY, DAY, WAREHOUSE_NAME, SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())
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
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    tk_daily AS (
        -- V043: task monitoring retired — the KPI arms below emit no rows,
        -- and the board table keeps its shape.
        SELECT NULL::VARCHAR AS COMPANY, NULL::DATE AS DAY, 0 AS RUNS, 0 AS FAILED
        WHERE FALSE
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

-- >>> seeds
MERGE INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE t
USING (
    SELECT 'DATABASE' AS SCOPE_TYPE, 'DBA_MAINT_DB' AS PATTERN, 'ALFA' AS COMPANY
) s
ON t.SCOPE_TYPE = s.SCOPE_TYPE AND t.PATTERN = s.PATTERN
WHEN NOT MATCHED THEN INSERT (SCOPE_TYPE, PATTERN, COMPANY)
     VALUES (s.SCOPE_TYPE, s.PATTERN, s.COMPANY);

-- >>> first fills
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 44 AS VERSION, 'UNKNOWN classification (#18): evidence-based company on both sides, COMPANY_SCOPE mapping lever (DATABASE rows supported, DBA_MAINT_DB seeded ALFA), exec board UNKNOWN scope' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 44);
