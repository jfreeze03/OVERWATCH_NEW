-- V143__query_operator_stats_collector.sql — QOIE Slice 2: operator-level query profile mart.
--
--   QOIE Slice 1 (v4.548) ranks recurring queries by OOS from QUERY-level stats. The
--   operator-level pathologies it CANNOT see — exploding joins (a Join whose output rows
--   dwarf its input rows), which specific operator spilled, and where a query's time
--   actually goes — live only in the Query Profile, exposed programmatically by the
--   table function GET_QUERY_OPERATOR_STATS(<query_id>). That function is per-query-id
--   only, has NO bulk ACCOUNT_USAGE view, and reaches back only 14 days, so operator
--   stats have to be COLLECTED: a scheduled proc loops the recent expensive query_ids and
--   lands one row per operator into a fact the app can read set-based.
--
--   NEW FACT_QUERY_OPERATOR_STATS_DAILY (one row per operator of a collected query):
--   QUERY_PARAMETERIZED_HASH joins it back to the Slice-1 fingerprint; per-operator
--   INPUT/OUTPUT_ROWS + ROW_MULTIPLE (join explosion), REMOTE/LOCAL_SPILL_GB (spill
--   causation), OPERATOR_TYPE + OP_TIME_PCT (operator anatomy), SCAN_PCT (per-scan
--   pruning). The display-facing measures use the humanize suffixes (_SEC/_GB/_PCT); the
--   raw partition counts (PARTITIONS_*) are computation inputs — the Slice-2 reader formats
--   them explicitly. OP_TIME_PCT stores overall_percentage as-is (the docs call it a
--   percentage and the Query Profile UI renders 0-100); its scale is confirmed by the
--   STEP-2 probe on first apply before any reader trusts it.
--
--   NEW SP_LOAD_QUERY_OPERATOR_STATS + a daily 07:20 America/Chicago task. INCREMENTAL:
--   each run collects only recent (2-day, well inside the 14-day operator-stats window)
--   expensive queries NOT already collected, reusing query_optimization_triage's exact
--   self-noise + inefficiency filters so this surface never contradicts the triage table.
--   Per-id calls are wrapped in EXCEPTION WHEN OTHER so an aged/utility/unauthorized
--   query_id is skipped, never aborting the run (GET_QUERY_OPERATOR_STATS's behavior for
--   an out-of-window / non-existent id is undocumented — tolerate both empty and error).
--
--   PRIVILEGE: the function needs OPERATE or MONITOR on the warehouse the target query
--   ran on (NOT query-ownership, NOT ACCOUNTADMIN). The proc runs EXECUTE AS OWNER, so
--   the OWNING role (SNOW_ACCOUNTADMINS) must hold that on the fleet's warehouses; an
--   admin role does. If STEP-2 lands 0 rows, that grant is the first thing to check.
--
--   Additive + contained: NEW table + NEW standalone proc + NEW task, NO re-derivation of
--   the byte-locked SP_LOAD_MARTS_V27. Idempotent; safe to re-run. Apply AFTER V142.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20143, 'V143 requires V142 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 142) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- One row per OPERATOR of a collected query. QUERY_* columns are enriched from
-- QUERY_HISTORY after the load (COMPANY via COMPANY_FOR_WAREHOUSE, the same scope axis
-- the query builders use). Byte magnitudes are stored in GB (the reader humanizes),
-- durations carry _SEC, percentages _PCT (owner's duration/byte-humanize naming rule).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY (
    QUERY_DAY                DATE,
    QUERY_ID                 VARCHAR(64),
    QUERY_PARAMETERIZED_HASH VARCHAR(64),
    WAREHOUSE_NAME           VARCHAR(256),
    WAREHOUSE_SIZE           VARCHAR(40),
    COMPANY                  VARCHAR(20),
    QUERY_ELAPSED_SEC        NUMBER(18,3),
    STEP_ID                  NUMBER(9,0),
    OPERATOR_ID              NUMBER(9,0),
    PARENT_OPERATOR_ID       NUMBER(9,0),
    OPERATOR_TYPE            VARCHAR(64),
    INPUT_ROWS               NUMBER(38,0),
    OUTPUT_ROWS              NUMBER(38,0),
    ROW_MULTIPLE             NUMBER(18,3),
    REMOTE_SPILL_GB          NUMBER(18,3),
    LOCAL_SPILL_GB           NUMBER(18,3),
    GB_SCANNED               NUMBER(18,3),
    PARTITIONS_SCANNED       NUMBER(18,0),
    PARTITIONS_TOTAL         NUMBER(18,0),
    SCAN_PCT                 NUMBER(9,2),
    OP_TIME_PCT              NUMBER(9,2),
    LOAD_TS                  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

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
                || 'ROUND(EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT, 2) '
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
        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3)
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

-- First fill so the mart-first reader serves immediately (retain 30 days of history).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(30);

-- Daily refresh at 07:20 America/Chicago (after the 07:10 storage task).
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_QUERY_OPERATOR_STATS
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 20 7 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(30);

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_QUERY_OPERATOR_STATS RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 143 AS VERSION,
       'QOIE Slice 2: operator-level query profile mart. FACT_QUERY_OPERATOR_STATS_DAILY + SP_LOAD_QUERY_OPERATOR_STATS + daily 07:20 task. The collector loops the recent (2-day) expensive query_ids from QUERY_HISTORY (query_optimization_triage filter set; incremental, skips already-collected) and calls GET_QUERY_OPERATOR_STATS per id, landing one row per operator: INPUT/OUTPUT_ROWS + ROW_MULTIPLE (exploding joins), REMOTE/LOCAL_SPILL_GB (spill causation), OPERATOR_TYPE + OP_TIME_PCT (operator anatomy), SCAN_PCT (per-scan pruning), enriched with QUERY_PARAMETERIZED_HASH to join Slice-1 fingerprints + COMPANY/warehouse scope. Per-id EXCEPTION-isolated (aged/utility ids skipped). NEW proc/task, no re-derivation of the byte-locked loaders.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 143);
