-- V147__operator_stats_identity_grain.sql
--
-- Stamp USER_NAME / DATABASE_NAME / SCHEMA_NAME onto the QOIE Slice 2 operator-stats fact so
-- the Operator profile (Operations ▸ Queries) can honor the User/Database/Schema scope filters,
-- not just company/warehouse/window. V143/V144 stamped only COMPANY + WAREHOUSE_NAME, so the
-- panel silently showed data broader than the active scope when one of those filters was set.
--
--   * ALTER FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS the three identity columns
--     (idempotent; named to match ACCOUNT_USAGE.QUERY_HISTORY so the app reuses _query_scope's
--     USER_NAME / DATABASE_NAME / SCHEMA_NAME predicates).
--   * Re-derive SP_LOAD_QUERY_OPERATOR_STATS from V144 (the current live proc): the set-based
--     enrich UPDATE, which already joins the query's QUERY_HISTORY row, now also fills the three.
--     That is the ONLY change to the proc (byte-identical otherwise; test_v147 proves it).
--   * One-time backfill of existing rows: they carry QUERY_DAY, so the proc's QUERY_DAY-IS-NULL
--     enrich never revisits them and the NOT-EXISTS candidate gate blocks re-collection — the
--     USER_NAME-IS-NULL backfill is the only fill path. Bounded to -35d (30-day retention +
--     2-day collection lag), inside QUERY_HISTORY's 365-day retention; idempotent.
--
-- App-side (already shipped): operator_stats_summary / operator_problem_board reference the
-- three columns ONLY when the arg is non-empty, and operations.py passes them empty until 147
-- is in the applied SCHEMA_VERSION set — so nothing references the columns before this applies,
-- and the grain self-heals the moment it does (no redeploy). Apply AFTER V146. Safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20147, 'V147 requires V146 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 146) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Identity grain (idempotent). Names mirror ACCOUNT_USAGE.QUERY_HISTORY so the app reuses the
-- same USER_NAME / DATABASE_NAME / SCHEMA_NAME predicates the query-level sections apply.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS USER_NAME VARCHAR(256);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS DATABASE_NAME VARCHAR(256);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS SCHEMA_NAME VARCHAR(256);

-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V144; enrich UPDATE now also stamps USER_NAME/DATABASE_NAME/SCHEMA_NAME, V147)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    keep INT;
    landed INT DEFAULT 0;      -- operator ROWS actually inserted (SQLROWCOUNT sum)
    collected INT DEFAULT 0;   -- candidate queries that landed >= 1 operator row
    skipped INT DEFAULT 0;     -- candidates that errored OR returned empty (aged/utility)
    emsg VARCHAR;              -- last per-id SQLERRM sample (distinguishes a SQL bug from a skip)
    ins VARCHAR;
    -- Candidate query_ids: the recent (2-day, inside the 14-day operator-stats window)
    -- expensive queries, using query_optimization_triage's EXACT filter set (self-noise
    -- dropped, a genuine-inefficiency gate) so the two surfaces agree. INCREMENTAL: skip
    -- any query already collected. Capped at 250/run (one table-function call per row).
    c_qids CURSOR FOR
        SELECT qh.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
        WHERE qh.START_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP())
          AND qh.EXECUTION_STATUS = 'SUCCESS'
          AND qh.QUERY_TYPE <> 'CALL'
          AND UPPER(COALESCE(qh.QUERY_TEXT, '')) NOT LIKE 'EXECUTE STREAMLIT%'
          AND UPPER(COALESCE(qh.QUERY_TEXT, '')) NOT LIKE '%OVERWATCH_APP%'
          AND COALESCE(qh.QUERY_TAG, '') NOT LIKE 'OVERWATCH%'
          AND (COALESCE(qh.BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0
               OR COALESCE(qh.BYTES_SCANNED, 0) > 50 * POWER(1024, 3))
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
              WHERE f.QUERY_ID = qh.QUERY_ID)
        ORDER BY COALESCE(qh.BYTES_SPILLED_TO_REMOTE_STORAGE, 0) DESC,
                 qh.BYTES_SCANNED DESC
        LIMIT 250;
BEGIN
    -- DAYS_BACK bounds how many days of collected operator stats to RETAIN (default 30);
    -- the collection window itself is a fixed 2 days (inside the function's 14-day reach).
    keep := GREATEST(COALESCE(:DAYS_BACK, 30), 1)::INT;

    -- Land the raw operator rows per query_id. The query_id is a Snowflake UUID from
    -- ACCOUNT_USAGE (safe to embed); the table-function argument cannot be bound, so build
    -- the INSERT with EXECUTE IMMEDIATE. QUERY_* enrichment columns are filled set-based
    -- below. PARENT_OPERATORS is an ARRAY (BCR-1175) - store the first parent, NULL at root.
    FOR q IN c_qids DO
        BEGIN
            ins := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY '
                || '(QUERY_ID, STEP_ID, OPERATOR_ID, PARENT_OPERATOR_ID, OPERATOR_TYPE, '
                || 'INPUT_ROWS, OUTPUT_ROWS, ROW_MULTIPLE, REMOTE_SPILL_GB, LOCAL_SPILL_GB, '
                || 'GB_SCANNED, PARTITIONS_SCANNED, PARTITIONS_TOTAL, SCAN_PCT, OP_TIME_PCT) '
                || 'SELECT ' || '''' || q.QUERY_ID || '''' || ', STEP_ID, OPERATOR_ID, '
                || 'PARENT_OPERATORS[0]::NUMBER, OPERATOR_TYPE, '
                || 'OPERATOR_STATISTICS:input_rows::NUMBER, '
                || 'OPERATOR_STATISTICS:output_rows::NUMBER, '
                || 'ROUND(OPERATOR_STATISTICS:output_rows::FLOAT '
                || '/ NULLIF(OPERATOR_STATISTICS:input_rows::FLOAT, 0), 3), '
                || 'ROUND(OPERATOR_STATISTICS:spilling:bytes_spilled_remote_storage::FLOAT '
                || '/ POWER(1024, 3), 3), '
                || 'ROUND(OPERATOR_STATISTICS:spilling:bytes_spilled_local_storage::FLOAT '
                || '/ POWER(1024, 3), 3), '
                || 'ROUND(OPERATOR_STATISTICS:io:bytes_scanned::FLOAT / POWER(1024, 3), 3), '
                || 'OPERATOR_STATISTICS:pruning:partitions_scanned::NUMBER, '
                || 'OPERATOR_STATISTICS:pruning:partitions_total::NUMBER, '
                || 'ROUND(OPERATOR_STATISTICS:pruning:partitions_scanned::FLOAT '
                || '/ NULLIF(OPERATOR_STATISTICS:pruning:partitions_total::FLOAT, 0) * 100, 2), '
                -- V144: overall_percentage is a 0-1 fraction (owner probe) -> * 100 for a 0-100 _PCT.
                || 'ROUND(EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT * 100, 2) '
                || 'FROM TABLE(GET_QUERY_OPERATOR_STATS(' || '''' || q.QUERY_ID || '''' || '))';
            EXECUTE IMMEDIATE :ins;
            -- SQLROWCOUNT = operator rows the table function returned for this id. It can
            -- legitimately be 0 WITHOUT raising (an aged/utility id returns empty), so count
            -- ROWS LANDED, not INSERT successes — otherwise an all-empty run reports false
            -- success and the all-empty alert below never fires.
            landed := landed + SQLROWCOUNT;
            IF (SQLROWCOUNT > 0) THEN
                collected := collected + 1;
            ELSE
                skipped := skipped + 1;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                -- An aged (>14d), non-existent, utility (no profile), or unauthorized
                -- query_id: skip it (expected), do not abort the run. Keep the last SQLERRM
                -- so a SYSTEMATIC dynamic-SQL bug (every id raises) is distinguishable from
                -- an expected skip when the all-empty alert fires (no CI executes this SQL).
                emsg := SQLERRM;
                skipped := skipped + 1;
        END;
    END FOR;

    -- Enrich the just-landed rows (QUERY_DAY IS NULL) from QUERY_HISTORY: the fingerprint
    -- hash to join Slice-1, the warehouse/company scope axis, and the query's elapsed time
    -- (so the app can turn OP_TIME_PCT into per-operator seconds). Set-based, no injection.
    UPDATE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
    SET QUERY_DAY = TO_DATE(qh.START_TIME),
        QUERY_PARAMETERIZED_HASH = qh.QUERY_PARAMETERIZED_HASH,
        WAREHOUSE_NAME = qh.WAREHOUSE_NAME,
        WAREHOUSE_SIZE = qh.WAREHOUSE_SIZE,
        COMPANY = DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qh.WAREHOUSE_NAME),
        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3),
        -- V147: identity grain so the Operator profile honors the User/Database/Schema scope
        -- filters (same QUERY_HISTORY columns the query-level _query_scope filters on).
        USER_NAME = qh.USER_NAME,
        DATABASE_NAME = qh.DATABASE_NAME,
        SCHEMA_NAME = qh.SCHEMA_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
    WHERE f.QUERY_ID = qh.QUERY_ID
      AND f.QUERY_DAY IS NULL
      -- -5d (candidate window is only -2d): if a prior run aborted between INSERT and this
      -- enrich, the NOT-EXISTS gate blocks re-collection, so a following run's QUERY_DAY-IS-
      -- NULL retry is the only self-heal path; the wider window gives it real grace to catch up.
      AND qh.START_TIME >= DATEADD('day', -5, CURRENT_TIMESTAMP());

    -- Retain `keep` days of collected operator stats (LOAD_TS is always set).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY
    WHERE LOAD_TS < DATEADD('day', -:keep, CURRENT_TIMESTAMP());

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_QUERY_OPERATOR_STATS_DAILY' AS SOURCE_NAME,
               MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT,
        t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    -- 0 rows landed while candidates existed is a real signal: log ONE summary line, not
    -- one per skipped id (the false-error-noise lesson). emsg present => a per-id error
    -- (likely a systematic dynamic-SQL bug); emsg NULL => all-empty returns (privilege gap
    -- on the fleet warehouses for the owning role, or nothing inside the 14-day window).
    IF (landed = 0 AND skipped > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OperatorStatsCollector', 'operator_stats_all_empty',
               'GET_QUERY_OPERATOR_STATS landed 0 operator rows for ' || :skipped || ' candidate queries'
               || COALESCE(' | last per-id error: ' || :emsg, ' | no per-id error raised (all empty returns)'),
               'if an error is shown: likely a dynamic-SQL defect; if all empty: check OPERATE/MONITOR '
               || 'on the fleet warehouses for the owning role, or the 14-day operator-stats window',
               CURRENT_ROLE();
    END IF;

    RETURN 'OK landed=' || :landed || ' queries=' || :collected || ' skipped=' || :skipped;
END;
$$;

-- One-time backfill of the already-collected rows (QUERY_DAY set => the proc's enrich skips
-- them; NOT-EXISTS gate blocks re-collection). Fills only NULLs, so it is idempotent and a
-- re-run is a no-op. -35d covers the 30-day retention + 2-day collection lag, well inside
-- QUERY_HISTORY's 365-day retention.
UPDATE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
SET USER_NAME = qh.USER_NAME,
    DATABASE_NAME = qh.DATABASE_NAME,
    SCHEMA_NAME = qh.SCHEMA_NAME
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
WHERE f.QUERY_ID = qh.QUERY_ID
  AND f.USER_NAME IS NULL
  AND qh.START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP());

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 147 AS VERSION,
       'Operator-stats identity grain: ADD COLUMN USER_NAME/DATABASE_NAME/SCHEMA_NAME to FACT_QUERY_OPERATOR_STATS_DAILY (V143/V144 stamped only COMPANY + WAREHOUSE_NAME) so the QOIE Slice 2 Operator profile honors the User/Database/Schema scope filters, not just company/warehouse/window. Re-derive SP_LOAD_QUERY_OPERATOR_STATS from V144 to also fill the three in the set-based enrich UPDATE that already joins the query QUERY_HISTORY row (byte-identical to V144 otherwise; the V144 overall_percentage*100 scale fix preserved). One-time -35d idempotent backfill of existing rows (USER_NAME-IS-NULL guard; QUERY_DAY-set rows are skipped by the proc enrich). Column names mirror ACCOUNT_USAGE.QUERY_HISTORY so the app reuses _query_scope predicates; the reader references them only when the filter is set and operations.py gates on 147 in the applied set, so nothing references the columns until this applies and the grain self-heals with no redeploy.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 147);
