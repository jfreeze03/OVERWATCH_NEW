-- V120: SP_LOAD_PATTERN_COST run-count fan-out fix (loader-01, round-7 hunt). The
-- pattern-cost loader joined QUERY_ATTRIBUTION_HISTORY directly to QUERY_HISTORY and
-- counted COUNT(*) AS RUNS. QAH emits MULTIPLE rows per QUERY_ID for a query whose
-- execution spans hour boundaries, so a single N-hour query fanned into N rows and
-- RUNS counted attribution rows, not executions -- inflating MART_PATTERN_COST_DAILY.RUNS
-- and HALVING CREDITS_PER_RUN for exactly the long-running patterns the workbench cost
-- panel exists to surface (CREDITS_ATTRIBUTED and USERS_HLL were unaffected). This
-- re-derives the loader to pre-aggregate QAH to one row per QUERY_ID before the join
-- (matching every sibling attribution loader), then re-runs it over 90 days to re-stamp
-- the historically inflated RUNS. Proc-only; no schema change. Apply AFTER V119.
-- Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20120, 'V120 requires V119 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 119) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
    USING (
        SELECT m.DAY, m.QUERY_HASH, m.COMPANY, m.DATABASE_NAME,
               SUM(m.RUNS) AS RUNS,
               SUM(m.CREDITS_ATTRIBUTED) AS CREDITS_ATTRIBUTED,
               HLL_COMBINE(m.USERS_HLL) AS USERS_HLL
        FROM (
            SELECT g.DAY, g.QUERY_HASH, g.DATABASE_NAME,
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                   g.RUNS, g.CREDITS_ATTRIBUTED, g.USERS_HLL
            FROM (
                SELECT CAST(q.START_TIME AS DATE) AS DAY,
                       q.QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                       COALESCE(q.WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                       COALESCE(q.DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COUNT(*) AS RUNS,
                       SUM(a.CREDITS_ATTRIBUTED) AS CREDITS_ATTRIBUTED,
                       HLL_ACCUMULATE(q.USER_NAME) AS USERS_HLL
                -- loader-01 (round 7): pre-aggregate QUERY_ATTRIBUTION_HISTORY to ONE row per
                -- QUERY_ID before joining QUERY_HISTORY. QAH emits multiple rows for a query that
                -- spans hour boundaries, so the old direct a x q join fanned one query into N rows
                -- and COUNT(*) counted attribution rows, inflating RUNS (and halving CREDITS_PER_RUN)
                -- for exactly the long-running patterns this mart exists to surface. Matches the
                -- per-QUERY_ID pre-aggregation every sibling loader uses (V067/V077/V113).
                FROM (
                    SELECT QUERY_ID,
                           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0)
                               + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS_ATTRIBUTED
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                    WHERE START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
                    GROUP BY QUERY_ID
                ) a
                JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                  ON q.QUERY_ID = a.QUERY_ID
                 AND q.START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
                WHERE q.QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2, 3, 4
            ) g
        ) m
        GROUP BY 1, 2, 3, 4
    ) s
    ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY
       AND t.DATABASE_NAME = s.DATABASE_NAME
    WHEN MATCHED THEN UPDATE SET
        t.RUNS = s.RUNS, t.CREDITS_ATTRIBUTED = s.CREDITS_ATTRIBUTED,
        t.USERS_HLL = s.USERS_HLL, t.LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)
    VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.DATABASE_NAME, s.RUNS, s.CREDITS_ATTRIBUTED, s.USERS_HLL);

    -- V068: loader-owned freshness stamp (V041-R6 pattern; this standalone-task loader
    -- was missed in the V041 handoff, freezing its SOURCE_FRESHNESS_STATE row at apply
    -- time). LAST_LOAD_TS is a RUN stamp (CURRENT_TIMESTAMP()), not MAX(LOAD_TS) of the
    -- mart, so a window with ZERO source events still reads fresh - no news is not
    -- no load.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_PATTERN_COST_DAILY' AS SOURCE_NAME, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS,
               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');
    RETURN 'OK';
END;
$$;

-- Re-stamp the historically inflated RUNS over the window the pattern-cost panel reads.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(90);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 120 AS VERSION, 'SP_LOAD_PATTERN_COST fan-out fix (loader-01): pre-aggregate QUERY_ATTRIBUTION_HISTORY to one row per QUERY_ID before joining QUERY_HISTORY so RUNS counts query executions, not attribution rows (an hour-spanning query no longer inflates RUNS / halves CREDITS_PER_RUN). Re-derived from V068; migration tail re-runs the loader over 90d to re-stamp inflated rows. Proc only, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 120);
