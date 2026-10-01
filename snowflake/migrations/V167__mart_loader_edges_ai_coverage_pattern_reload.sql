-- V167__mart_loader_edges_ai_coverage_pattern_reload.sql
--
-- Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (v4.609 round-2 fixes).
--
-- WHY:
--   * R2-015: SP_LOAD_MARTS_V27 arm [1] expanded a query's spanned hours only for queries that STARTED inside
--     the window, so the hours a midnight-crossing query ran on day D-d read idle. The nightly reconcile last
--     writes a day as D-3, its left edge, so every finished day kept that error: IDLE_PCT / IDLE_CREDITS (the
--     Optimize idle and sizing panels, COST_IDLE_OPPORTUNITY) overstated idle on ELT warehouses.
--   * R2-014: arm [6] built task-graph runs only from attempts inside the window, so a run whose root started
--     before the window's first midnight was re-keyed to its first in-window child: a phantom pipeline row on
--     D-d, finalised by the reconcile, double-counting the children's credits.
--   * R2-052 (also satisfies R2-017): the Cortex Code views stamp USAGE_TIME as TIMESTAMP_TZ, and ::DATE /
--     ::TIMESTAMP_NTZ read its OWN stored offset, so arm [9] keyed days off the Central account day (and the
--     Central-midnight scan bound cut the oldest kept key short every run: a permanent under-count).
--   * R1-016: FACT_AI_USAGE_DAILY has no zero-row spine, so MIN(DAY) is the first AI use, not how far back the
--     loader reached; the app gate read it as the latter and blanked 180d / 365d / Current-year AI panels.
--   * R2-018: SP_NIGHTLY_RECONCILE DELETEd four tables up front whose reload edge (D-3 / ~now-3d) is wider
--     than the hourly task's d=2; one failed loader arm (each swallows its error) left that edge empty for good.
--     The V064 'transient gap' comment (and CHANGELOG:12389) was wrong for these four.
--   * R2-010: SP_LOAD_PATTERN_COST MERGEd on (DAY, QUERY_HASH, COMPANY, DATABASE_NAME) with COMPANY computed at
--     load time, so an Apply-mapping remap left the old-company row beside the new one (ALL scope summed both).
--   * PATTERN-RESTAMP: rows older than V120's 90-day re-stamp still carry inflated RUNS; a full-year reload is
--     only safe on the atomic loader, and the app may only read past 90 days where a reload provably reached.
--
-- WHAT (each re-derived proc is byte-identical to its base except the enumerated deltas):
--   + SOURCE_FRESHNESS_STATE.COVERAGE_FROM DATE (nullable; every writer names its columns).
--   ~ SP_LOAD_MARTS_V27 from V159: arm [1] span source START_TIME >= D-d-1 AND COALESCE(END_TIME, START_TIME)
--     >= D-d (m / q / m_idle keep D-d); arm [6] attempts from D-d-1 and WHERE DAY >= D-d before the final
--     GROUP BY (the QAH and IN-list bounds unchanged); arm [9] CONVERT_TIMEZONE('America/Chicago', ...) on DAY,
--     FIRST_TS and LAST_TS (the ai_functions arm keys an LTZ START_TIME, unchanged); the DAILY freshness MERGE
--     writes COVERAGE_FROM for FACT_AI_USAGE_DAILY = LEAST(old, today - d + 1) -- its HAVING already requires
--     BOTH AI arms. No UPDATE of SOURCE_FRESHNESS_STATE anywhere.
--   ~ SP_NIGHTLY_RECONCILE from V064: the four up-front DELETEs go; after the ('HOURLY', 3) verdict line each
--     table is swept of rows the reload did not re-stamp (LOAD_TS < recon_start) inside its reload window,
--     ONLY when that arm's :loaded token is in the marts verdict (rv). The hour bound is V159's ext_lo_hour
--     expression without its COALESCE fallback (an empty extract sweeps nothing). V159's 'SP_NIGHTLY_RECONCILE
--     DELETEs D-3..today' comment is superseded; its d > 2 gate escape is still required.
--   ~ SP_LOAD_PATTERN_COST from V120: BEGIN TRANSACTION; DELETE WHERE DAY >= :lo; INSERT (V120's aggregate,
--     unchanged); COMMIT; the V068 freshness MERGE (after the COMMIT) stamps COVERAGE_FROM = LEAST(old, :lo);
--     EXCEPTION WHEN OTHER THEN ROLLBACK; RAISE (the task still fails loudly).
--   - One-time repair: DELETE MART_PATTERN_COST_DAILY rows older than their (DAY, QUERY_HASH, DATABASE_NAME)
--     group's newest LOAD_TS by more than 10 minutes (groups of 2+ rows only). Every legitimate company row of
--     a day's last covering run shares that run's statement timestamp (V120's MERGE re-stamped LOAD_TS on
--     MATCHED), so an older row is a stale-company twin. Scan-free; recoverable by Time Travel or a reload.
--     Preview with PREFLIGHT P167.1 (rows, credits, day range) and P167.2 (same-stamp groups that are kept).
--
-- COST: arm [1]'s span leg scans one more day of QUERY_HISTORY (3 days instead of 2 on the six gated hourly
-- runs, 4 instead of 3 on the reconcile); arm [6] one more day of TASK_HISTORY; the same statement shapes, so
-- V159's compile diet stands. The reconcile adds four small DELETEs (two with a one-row extract subquery). The
-- pattern loader's statement count goes 2 -> 4 (BEGIN / DELETE / INSERT / COMMIT) on its daily 3-day window.
-- The twin DELETE reads only the mart. No new task, schedule or warehouse resume.
-- FIRST RUN: the next hourly TASK_LOAD_HOURLY graph, the 06:40-07:30 Central nightly chain and the 06:45
-- TASK_PATTERN_COST_DAILY pick up the new bodies. Nothing runs at apply time. COVERAGE_FROM starts as
-- today - 2 (AI) / today - 3 (pattern) and reaches back only after the owner-run reloads in the handoff
-- (OWNER_REPAIRS, a Central session): AI DAILY 365 reload-then-prune, then CALL SP_LOAD_PATTERN_COST(364),
-- then HOURLY N inside a TASK_LOAD_HOURLY suspend window, then the atomic arm [6] 364-day rebuild.
-- Known limits (disclosed): a query of 24h+ starting in hour 23 two days before a left edge on the
-- spring-forward night can still miss one hour; a task-graph run longer than ~1 day past the left edge can
-- still leave a phantom row; Cortex Code rows older than the views' retention stay offset-keyed.
-- ROLLBACK: re-run the base CREATEs (V159 SP_LOAD_MARTS_V27, V064 SP_NIGHTLY_RECONCILE, V120
-- SP_LOAD_PATTERN_COST); the column is inert to every older body (leave it). The twin DELETE is recoverable
-- by Time Travel (AT before the apply) or CALL SP_LOAD_PATTERN_COST(N).
-- Apply AFTER V166. Idempotent; safe to re-run. Owner applies in Snowsight; this file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20167, 'V167 requires V166 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 166) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- R1-016 / PATTERN-RESTAMP: the loaded-from watermark both re-derived loaders write inside their own freshness
-- MERGE (nullable; read only by the app, behind has_migration(167)).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS COVERAGE_FROM DATE;

-- >>> derived:SP_LOAD_MARTS_V27  (from V159; + R2-015 arm [1] span source padded a day, R2-014 arm [6] lead-in day + DAY filter, R2-052 arm [9] Central day key, R1-016 AI COVERAGE_FROM in the DAILY freshness MERGE, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
    loaded VARCHAR DEFAULT '';
    d INT;
    ct_hour INT;              -- V159 (D5): Central hour of this run, read once (the 4-hour gate)
    ext_lo DATE;
    ext_lo_hour TIMESTAMP_LTZ;
    req_fail INT DEFAULT 0;   -- V066 #10: REQUIRED-arm (core fact/mart) failures this run
    opt_fail INT DEFAULT 0;   -- V066 #10: OPTIONAL-arm (tag-cov, task-node, AI/Cortex) failures
    bad_scope EXCEPTION (-20661,
        'SP_LOAD_MARTS_V27: SCOPE must be HOURLY or DAILY - refusing to run as a silent no-op load.');   -- V066 #37 VALIDATE SCOPE
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;

    -- V066 #37 VALIDATE SCOPE: an unrecognized SCOPE matched no arm and the terminal RETURN
    -- still claimed the marts loaded, so a typo'd scope silently loaded nothing. Fail loudly
    -- at the top instead (the outer BEGIN has no handler, so this RAISE aborts the proc).
    IF (UPPER(:SCOPE) NOT IN ('HOURLY', 'DAILY')) THEN
        RAISE bad_scope;
    END IF;

    IF (UPPER(:SCOPE) = 'HOURLY') THEN

        -- V159 compile diet (D5): the three DAY-grain arms whose ACCOUNT_USAGE MERGEs dominate this
        -- loader's compile -- [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b]
        -- MART_TASK_NODE_DAILY -- run every 4th Central hour (00, 04, 08, 12, 16, 20) instead of every
        -- hour, and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE DELETEs D-3..today of the first two and
        -- re-loads them with ('HOURLY', 3), and backfills pass 90/365. The hourly task passes 2. A gated-off
        -- arm is not a failure (req_fail / opt_fail untouched) and appends no :loaded token, so its
        -- SOURCE_FRESHNESS_STATE row keeps its last stamp -- every name here contains DAILY, so the shared
        -- 30h cadence rule never reads it stale. Every other arm below still runs every hour.
        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;

        -- V062 B5/B10: clamp backfill lower bounds to the extract's first
        -- WHOLE day/hour so a wide :d actually loads :d days (not a silent 2),
        -- while normal ops (small :d) stay at the extract-bounded window.
        ext_lo := (SELECT COALESCE(
                       DATEADD('day', IFF(MIN(START_TIME) = DATE_TRUNC('day', MIN(START_TIME)), 0, 1), DATE(MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        ext_lo_hour := (SELECT COALESCE(
                       DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);

        -- [1] warehouse efficiency ------------------------------------------
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [1]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY t
            USING (
                WITH m AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           SUM(CREDITS_USED) AS CREDITS_TOTAL,
                           SUM(CREDITS_USED_COMPUTE) AS CREDITS_COMPUTE,
                           COUNT_IF(CREDITS_USED > 0) AS BILLED_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                ),
                q AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,
                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,
                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,
                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_NAME IS NOT NULL
                    GROUP BY 1, 2
                ),
                -- V103: ACTIVE_HOURS must count every clock hour a query was RUNNING, not just
                -- its START hour. The old COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) marked
                -- hours 11 and 12 of a 10:59->13:00 query IDLE, so IDLE_PCT (and every $ derived
                -- from it: the SUSPEND/DOWN sizing verdict, IDLE_MONTHLY_USD, the idle-$ KPI)
                -- overstated idle for any multi-hour query. Expand each query across the hours it
                -- SPANS (bounded to 25, matching insights_sql._active_hours_cte), attribute each
                -- spanned hour to its own DAY, and count distinct warehouse-day-hours.
                qh AS (
                    SELECT s.WAREHOUSE_NAME,
                           DATE(DATEADD('hour', g.SEQ, s.H0)) AS DAY,
                           DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS
                    FROM (
                        SELECT WAREHOUSE_NAME,
                               DATE_TRUNC('hour', START_TIME) AS H0,
                               DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                        -- V167 (R2-015): pad the span source one day back, so a query that started before the
                        -- window's first Central midnight still marks the hours it spans on day D-d active
                        -- (the 25-row GENERATOR caps a span at H0 + 24h, so one day reaches every D-d hour but
                        -- one hour on the spring-forward night); the END_TIME floor drops prior-day queries that
                        -- never reach D-d. m / q / m_idle keep D-d, so no output row moves to an earlier day.
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND COALESCE(END_TIME, START_TIME) >= DATEADD('day', -:d, CURRENT_DATE())
                          AND WAREHOUSE_NAME IS NOT NULL
                    ) s
                    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 25))) g
                      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1
                ),
                q_active AS (
                    SELECT WAREHOUSE_NAME, DAY, COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS
                    FROM qh
                    GROUP BY 1, 2
                ),
                m_idle AS (
                    -- V127: ACTUAL credits burned in warehouse-hours with NO active (span-
                    -- expanded) query -- mirrors the live twin insights_sql.idle_warehouse_analysis
                    -- (SUM(IFF(no active query hour, CREDITS_USED, 0))). Stored so the reader
                    -- eff_idle_analysis reads accurate idle spend instead of pro-rating the day's
                    -- total credits by the hour-count IDLE_PCT (which over-states idle for scale-out
                    -- warehouses, whose idle hours cost less than their active multi-cluster hours).
                    -- Join to DISTINCT active hours (like the live query_hours CTE) so a metering
                    -- slice is never fanned out by multiple queries sharing an hour.
                    SELECT DATE(mh.START_TIME) AS DAY, mh.WAREHOUSE_NAME,
                           SUM(IFF(a.HOUR_TS IS NULL, COALESCE(mh.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY mh
                    LEFT JOIN (SELECT DISTINCT WAREHOUSE_NAME, HOUR_TS FROM qh) a
                           ON a.WAREHOUSE_NAME = mh.WAREHOUSE_NAME
                          AND a.HOUR_TS = DATE_TRUNC('hour', mh.START_TIME)
                    WHERE mh.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND mh.WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                )
                SELECT COALESCE(m.DAY, q.DAY) AS DAY,
                       COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)) AS COMPANY,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0), 4) AS CREDITS_TOTAL,
                       ROUND(COALESCE(m.CREDITS_COMPUTE, 0), 4) AS CREDITS_COMPUTE,
                       COALESCE(q.QUERIES, 0) AS QUERIES,
                       COALESCE(q.FAILS, 0) AS FAILS,
                       ROUND(COALESCE(q.QUEUED_MIN, 0), 2) AS QUEUED_MIN,
                       ROUND(COALESCE(q.SPILL_GB, 0), 3) AS SPILL_GB,
                       ROUND(COALESCE(q.P95_S, 0), 1) AS P95_S,
                       ROUND(COALESCE(q.EXEC_HOURS, 0), 3) AS EXEC_HOURS,
                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,
                       COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,
                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(qa.ACTIVE_HOURS, 0), 0)
                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY,
                       ROUND(COALESCE(mi.IDLE_CREDITS, 0), 4) AS IDLE_CREDITS
                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME
                LEFT JOIN q_active qa ON qa.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                     AND qa.DAY = COALESCE(m.DAY, q.DAY)
                LEFT JOIN m_idle mi ON mi.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                   AND mi.DAY = COALESCE(m.DAY, q.DAY)
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET
                COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL,
                CREDITS_COMPUTE = s.CREDITS_COMPUTE, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_MIN = s.QUEUED_MIN, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S,
                EXEC_HOURS = s.EXEC_HOURS, BILLED_HOURS = s.BILLED_HOURS,
                ACTIVE_HOURS = s.ACTIVE_HOURS, IDLE_PCT = s.IDLE_PCT,
                CREDITS_PER_QUERY = s.CREDITS_PER_QUERY, IDLE_CREDITS = s.IDLE_CREDITS,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
                 QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS, IDLE_PCT, CREDITS_PER_QUERY, IDLE_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_TOTAL, s.CREDITS_COMPUTE, s.QUERIES, s.FAILS,
                    s.QUEUED_MIN, s.SPILL_GB, s.P95_S, s.EXEC_HOURS, s.BILLED_HOURS, s.ACTIVE_HOURS, s.IDLE_PCT, s.CREDITS_PER_QUERY, s.IDLE_CREDITS);
            loaded := loaded || 'wh_eff ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [1]

        -- [2] query families (top 2000/day by exec time) --------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DAY,
                       QUERY_HASH,
                       COMPANY,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM (
                    -- V082: derive COMPANY per row FIRST (UDF outside the aggregation, the
                    -- V029 shape law), so the outer GROUP BY keys on a plain column and never
                    -- on the correlated-subquery UDF directly -- grouping BY that UDF is the
                    -- exact shape that logged mart_load_failed every hour after V027 (V029).
                    SELECT DATE(START_TIME) AS DAY,
                           QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY,
                           QUERY_TEXT, EXECUTION_STATUS, USER_NAME, WAREHOUSE_NAME,
                           DATABASE_NAME, SCHEMA_NAME, EXECUTION_TIME, TOTAL_ELAPSED_TIME,
                           COMPILATION_TIME, BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                      AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                )
                GROUP BY DAY, QUERY_HASH, COMPANY
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, COMPANY, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);
            loaded := loaded || 'qfam ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_QUERY_FAMILY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [3] role-hour fact -------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.ROLE_NAME = s.ROLE_NAME AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC)
            VALUES (s.HOUR_TS, s.ROLE_NAME, s.WAREHOUSE_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.EXEC_SEC);
            loaded := loaded || 'role_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_ROLE_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4] schema-hour fact -----------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.DATABASE_NAME = s.DATABASE_NAME AND t.SCHEMA_NAME = s.SCHEMA_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS, QUEUED_SEC, SPILL_GB, P95_S)
            VALUES (s.HOUR_TS, s.DATABASE_NAME, s.SCHEMA_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.QUEUED_SEC, s.SPILL_GB, s.P95_S);
            loaded := loaded || 'schema_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4b] tag coverage by user, day grain (v4.14 tuning trio) --------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY t
            USING (
                SELECT g.DAY, g.USER_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(g.USER_NAME) AS COMPANY,
                       g.QUERIES, g.EXEC_SEC, g.UNTAGGED_EXEC_SEC
                FROM (
                    SELECT DATE(START_TIME) AS DAY,
                           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                           COUNT(*) AS QUERIES,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC,
                           ROUND(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL,
                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                    GROUP BY 1, 2
                ) g
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES,
                EXEC_SEC = s.EXEC_SEC, UNTAGGED_EXEC_SEC = s.UNTAGGED_EXEC_SEC,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, COMPANY, QUERIES, EXEC_SEC, UNTAGGED_EXEC_SEC)
            VALUES (s.DAY, s.USER_NAME, s.COMPANY, s.QUERIES, s.EXEC_SEC, s.UNTAGGED_EXEC_SEC);
            loaded := loaded || 'tagcov ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TAG_COVERAGE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5] cost allocation (exec-time share of each warehouse-hour) -------
        BEGIN
            CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE AS
            WITH wh AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       SUM(CREDITS_USED) AS HOUR_CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_ID > 0
                GROUP BY 1, 2
            ),
            q AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       USER_NAME, COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_NAME IS NOT NULL AND COALESCE(EXECUTION_TIME, 0) > 0
                GROUP BY 1, 2, 3, 4, 5, 6
            ),
            tot AS (
                SELECT HOUR_TS, WAREHOUSE_NAME, SUM(EXEC_MS) AS TOTAL_MS FROM q GROUP BY 1, 2
            )
            SELECT DATE(q.HOUR_TS) AS DAY, q.WAREHOUSE_NAME, q.USER_NAME, q.ROLE_NAME,
                   q.DATABASE_NAME, q.SCHEMA_NAME, q.EXEC_MS,
                   wh.HOUR_CREDITS * q.EXEC_MS / NULLIF(tot.TOTAL_MS, 0) AS ALLOC_CREDITS
            FROM q
            JOIN tot ON tot.HOUR_TS = q.HOUR_TS AND tot.WAREHOUSE_NAME = q.WAREHOUSE_NAME
            JOIN wh ON wh.HOUR_TS = q.HOUR_TS AND wh.WAREHOUSE_NAME = q.WAREHOUSE_NAME;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t
            USING (
                SELECT DAY, 'USER' AS DIMENSION, USER_NAME AS KEY_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'DATABASE', DATABASE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'SCHEMA', DATABASE_NAME || '.' || SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3, DATABASE_NAME
                UNION ALL
                SELECT DAY, 'ROLE', ROLE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
            ) s
            ON t.DAY = s.DAY AND t.DIMENSION = s.DIMENSION AND t.KEY_NAME = s.KEY_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, ALLOC_CREDITS = s.ALLOC_CREDITS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC)
            VALUES (s.DAY, s.DIMENSION, s.KEY_NAME, s.COMPANY, s.ALLOC_CREDITS, s.EXEC_SEC);
            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5b] cross-dim allocation fact (V041 R2): persist _OW_ALLOC_BASE at
        -- DAY x WAREHOUSE x DATABASE x USER before it collapses to single-dim.
        -- NO schema grain (cardinality; schema stays live-filtered). Same
        -- expressions as [5], so the day-sums reconcile by construction.
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY t
            USING (
                SELECT DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
                FROM _OW_ALLOC_BASE
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
               AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET EXEC_SEC = s.EXEC_SEC,
                ALLOC_CREDITS = s.ALLOC_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.EXEC_SEC, s.ALLOC_CREDITS);
            loaded := loaded || 'alloc_xdim ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [6] task graphs -----------------------------------------------------
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [6]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY t
            USING (
                WITH attempts AS (
                    -- V126: keep EVERY attempt (do NOT collapse to the terminal attempt before
                    -- the credit join) and tag the terminal one. TASK_RUNS / FAILED_TASKS still
                    -- count scheduled tasks via TERMINAL_RN = 1 (a task auto-retried to success
                    -- is not a graph-run failure), but WH_CREDITS now SUMs the compute of EVERY
                    -- attempt -- each retry really billed compute. This mirrors the live twin
                    -- graph_sql.graph_daily_costs exactly, so the same task-graph panel's cost no
                    -- longer flips with mart warmth. V102's terminal-only credit join dropped a
                    -- failed-retry attempt's compute (documented as accepted, but it disagreed
                    -- with the live path and the "every task run" panel caption).
                    SELECT COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
                           h.NAME, h.DATABASE_NAME, h.SCHEMA_NAME,
                           h.QUERY_START_TIME, h.COMPLETED_TIME, h.STATE,
                           COALESCE(a.CREDITS, 0) AS CREDITS,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID), h.NAME, h.SCHEDULED_TIME
                               ORDER BY h.COMPLETED_TIME DESC NULLS LAST) AS TERMINAL_RN
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    LEFT JOIN (
                        SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)
                    ) a ON a.ROOT_ID = h.QUERY_ID
                    -- V167 (R2-014): one lead-in day, so a run that began before the window is seen whole
                    -- here and dropped by the DAY filter below, instead of being re-keyed to its first
                    -- in-window child as a phantom pipeline row on the window's first day
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                      AND h.STATE IN ('SUCCEEDED', 'FAILED')
                ),
                runs AS (
                    SELECT RUN_KEY,
                           MIN_BY(NAME, QUERY_START_TIME) AS PIPELINE,
                           MIN_BY(DATABASE_NAME, QUERY_START_TIME) AS DATABASE_NAME,
                           MIN_BY(SCHEMA_NAME, QUERY_START_TIME) AS SCHEMA_NAME,
                           DATE(MIN(QUERY_START_TIME)) AS DAY,
                           COUNT_IF(TERMINAL_RN = 1) AS TASK_RUNS,
                           COUNT_IF(TERMINAL_RN = 1 AND STATE = 'FAILED') AS FAILED_TASKS,
                           DATEDIFF('second', MIN(QUERY_START_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC,
                           SUM(CREDITS) AS CREDITS
                    FROM attempts
                    GROUP BY RUN_KEY
                )
                SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
                       COUNT(*) AS GRAPH_RUNS,
                       COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
                       SUM(TASK_RUNS) AS TASK_RUNS,
                       ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
                       ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
                       ROUND(SUM(CREDITS), 4) AS WH_CREDITS
                FROM runs
                -- V167 (R2-014): only runs whose first attempt is inside the window; a run that started
                -- earlier was written whole on its own root day by the load that covered that day
                WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.PIPELINE = s.PIPELINE
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET GRAPH_RUNS = s.GRAPH_RUNS,
                RUNS_WITH_FAILURES = s.RUNS_WITH_FAILURES, TASK_RUNS = s.TASK_RUNS,
                AVG_WALL_SEC = s.AVG_WALL_SEC, P95_WALL_SEC = s.P95_WALL_SEC,
                WH_CREDITS = s.WH_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
                 TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
            VALUES (s.DAY, s.PIPELINE, s.DATABASE_NAME, s.SCHEMA_NAME, s.GRAPH_RUNS,
                    s.RUNS_WITH_FAILURES, s.TASK_RUNS, s.AVG_WALL_SEC, s.P95_WALL_SEC, s.WH_CREDITS);
            loaded := loaded || 'graphs ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [6]

        -- [6b] per-node task timing (queue + exec delay) -> MART_TASK_NODE_DAILY
        -- Observability for the deferred reconcile-scheduling work: the
        -- SCHEDULED_TIME->QUERY_START_TIME dispatch delay (which the pipeline-grain
        -- arm [6] discards) quantifies the 06:40/06:45 XSMALL contention. Own
        -- guarded arm; touches no existing statement; one TASK_HISTORY scan at the
        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [6b]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t
            USING (
                SELECT DATE(QUERY_START_TIME) AS DAY,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       NAME AS TASK_NAME,
                       COUNT(*) AS RUNS,
                       COUNT_IF(STATE = 'FAILED') AS FAILED,
                       ROUND(AVG(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS AVG_QUEUE_SEC,
                       ROUND(APPROX_PERCENTILE(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0), 0.95) / 1000, 2) AS P95_QUEUE_SEC,
                       ROUND(MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS MAX_QUEUE_SEC,
                       ROUND(AVG(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS AVG_EXEC_SEC,
                       ROUND(APPROX_PERCENTILE(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME), 0.95) / 1000, 2) AS P95_EXEC_SEC,
                       ROUND(MAX(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS MAX_EXEC_SEC,
                       MIN(QUERY_START_TIME) AS FIRST_START,
                       MAX(COMPLETED_TIME) AS LAST_COMPLETED
                FROM (
                    -- V102: collapse task auto-retries to the terminal attempt so RUNS /
                    -- FAILED and the queue/exec percentiles count scheduled runs, not
                    -- attempts, mirroring the live ops_sql.task_runs / task_recent_states.
                    SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                           QUERY_START_TIME, COMPLETED_TIME, STATE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND STATE IN ('SUCCEEDED', 'FAILED')
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                               ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
                ) th
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.TASK_NAME = s.TASK_NAME
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET
                RUNS = s.RUNS, FAILED = s.FAILED,
                AVG_QUEUE_SEC = s.AVG_QUEUE_SEC, P95_QUEUE_SEC = s.P95_QUEUE_SEC, MAX_QUEUE_SEC = s.MAX_QUEUE_SEC,
                AVG_EXEC_SEC = s.AVG_EXEC_SEC, P95_EXEC_SEC = s.P95_EXEC_SEC, MAX_EXEC_SEC = s.MAX_EXEC_SEC,
                FIRST_START = s.FIRST_START, LAST_COMPLETED = s.LAST_COMPLETED, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, RUNS, FAILED,
                 AVG_QUEUE_SEC, P95_QUEUE_SEC, MAX_QUEUE_SEC,
                 AVG_EXEC_SEC, P95_EXEC_SEC, MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED)
            VALUES (s.DAY, s.DATABASE_NAME, s.SCHEMA_NAME, s.TASK_NAME, s.RUNS, s.FAILED,
                    s.AVG_QUEUE_SEC, s.P95_QUEUE_SEC, s.MAX_QUEUE_SEC,
                    s.AVG_EXEC_SEC, s.P95_EXEC_SEC, s.MAX_EXEC_SEC, s.FIRST_START, s.LAST_COMPLETED);
            loaded := loaded || 'task_node ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [6b]

        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            -- V066 #3: wrap the DELETE+INSERT in ONE transaction. Under AUTOCOMMIT the DELETE
            -- committed immediately, so a later failure in the 4-way UNION INSERT (a transient
            -- ACCOUNT_USAGE read / COMPANY_FOR_DATABASE UDF error) left the trailing 48h BLANK
            -- until the next hourly rebuild -- an incident timeline empty mid-incident. ROLLBACK
            -- on error restores the prior rows (the B34 FACT_TASK_DAILY wrap pattern).
            BEGIN TRANSACTION;
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

            INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
                (EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID)
            SELECT RAISED_AT, 'ALERT', COMPANY, SEVERITY, LEFT(TITLE, 300), EVENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
            UNION ALL
            SELECT COMPLETED_TIME, 'TASK_FAIL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE COMPLETED_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'
            UNION ALL
            SELECT START_TIME, 'DDL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'INFO', LEFT(QUERY_TYPE || ' by ' || USER_NAME || ' (' || COALESCE(ROLE_NAME, '?') || ')', 300), QUERY_ID
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                                 'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
            UNION ALL
            SELECT CHANGE_SEEN_AT, 'WH_CHANGE', COMPANY, 'INFO',
                   LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' || COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 300),
                   CHANGE_ID
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            COMMIT;
            loaded := loaded || 'timeline ';
        EXCEPTION
            WHEN OTHER THEN
                ROLLBACK;   -- V066 #3: undo the 48h DELETE if the rebuild INSERT failed
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE: stamp ONLY the sources whose arm actually
        -- loaded this run. This MERGE used to advance GENERATION and write the successful-arm
        -- list as STATUS across the whole STATIC group, so a source whose arm just failed
        -- still looked freshly loaded. Each arm appends its token to :loaded only on its
        -- success path, so gate the source set on token membership (ARRAY_CONTAINS over
        -- SPLIT(:loaded)); a failed source is left untouched -- its prior generation/snapshot
        -- stand, correctly reading as not-loaded-this-run -- and STATUS now carries that
        -- source's own outcome.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'wh_eff'),
                    ('MART_QUERY_FAMILY_DAILY', 'qfam'),
                    ('FACT_QUERY_ROLE_HOURLY', 'role_hr'),
                    ('FACT_QUERY_SCHEMA_HOURLY', 'schema_hr'),
                    ('MART_TAG_COVERAGE_DAILY', 'tagcov'),
                    ('MART_COST_ALLOCATION_DAILY', 'alloc'),
                    ('FACT_COST_ALLOC_XDIM_DAILY', 'alloc_xdim'),
                    ('MART_TASK_GRAPH_DAILY', 'graphs'),
                    ('MART_TASK_NODE_DAILY', 'task_node'),
                    ('MART_INCIDENT_TIMELINE', 'timeline')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))
            GROUP BY f.SOURCE_NAME
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN

        -- [7] security posture ------------------------------------------------
        BEGIN
            -- V041 R11 (guarded, v4.36.1): SHOW -> RESULT_SCAN once daily
            -- (V024 precedent), so Security stops paying a SHOW + parse per
            -- render. The nested handler means a SHOW failure can never take
            -- the CORE posture metrics down with it — the monitor arms below
            -- emit no rows that day instead (HAVING; never a lying zero).
            BEGIN
                SHOW WAREHOUSES LIMIT 500;
                CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR AS
                SELECT "name"::VARCHAR AS WAREHOUSE_NAME,
                       COALESCE("resource_monitor"::VARCHAR, 'null') AS RESOURCE_MONITOR,
                       TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND
                FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR (
                        WAREHOUSE_NAME VARCHAR, RESOURCE_MONITOR VARCHAR, AUTO_SUSPEND NUMBER);
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'MartLoader', 'monitor_counts_skipped', :emsg, 'SHOW WAREHOUSES unavailable - core posture unaffected', CURRENT_ROLE();
            END;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
            USING (
                -- A4: CREDENTIALS scanned ONCE; both metrics via COUNT_IF + UNPIVOT (was two scans).
                SELECT CURRENT_DATE() AS DAY, cu.METRIC AS METRIC, 'ALL' AS COMPANY, cu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(EXPIRATION_DATE IS NOT NULL
                                    AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS "EXPIRING_CRED_10D",
                           COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS "EXPIRED_CRED"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                ) c
                UNPIVOT (VALUE FOR METRIC IN ("EXPIRING_CRED_10D", "EXPIRED_CRED")) cu
                UNION ALL
                SELECT CURRENT_DATE(), 'ADMIN_STMTS_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                UNION ALL
                -- A4: GRANTS_TO_USERS scanned ONCE; both grant metrics via COUNT_IF + UNPIVOT (was two
                -- scans). The outer WHERE is a superset of the rows either metric needs (created >= -30d
                -- covers the -24h change window; deleted >= -24h keeps revoked-in-24h rows), and each
                -- COUNT_IF re-applies its exact original predicate, so both counts are unchanged.
                SELECT CURRENT_DATE() AS DAY, gu.METRIC AS METRIC, 'ALL' AS COMPANY, gu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                                    OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS "GRANT_CHANGES_24H",
                           COUNT_IF(DELETED_ON IS NULL
                                    AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                                    AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS "BREAKGLASS_GRANTS_30D"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                    WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                ) g
                UNPIVOT (VALUE FOR METRIC IN ("GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D")) gu
                UNION ALL
                -- V041 R9: unused-role posture from the role-hour fact, not a
                -- 90d QUERY_HISTORY anti-join. Coverage-gated: HAVING emits NO
                -- row (never a lying zero) until the fact spans the window.
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY q
                      WHERE q.HOUR_TS >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)
                       <= DATEADD('day', -89, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_MONITOR', 'ALL',
                       COUNT_IF(LOWER(TRIM(RESOURCE_MONITOR)) IN ('null', '', 'none'))
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_AUTOSUSPEND', 'ALL',
                       COUNT_IF(COALESCE(AUTO_SUSPEND, 0) <= 0)
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
            ) s
            ON t.DAY = s.DAY AND t.METRIC = s.METRIC AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, METRIC, COMPANY, VALUE)
            VALUES (s.DAY, s.METRIC, s.COMPANY, s.VALUE);
            loaded := loaded || 'posture ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_SECURITY_POSTURE_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                -- V167 (R2-052): USAGE_TIME is TIMESTAMP_TZ, and ::DATE / ::TIMESTAMP_NTZ on a TIMESTAMP_TZ
                -- read the value's OWN stored offset, not the session zone. Convert to Central first, so DAY
                -- is the account day the Central-midnight window bound below (and every sibling fact) uses,
                -- and FIRST_TS / LAST_TS hold Central wall clock (a no-op if the views stamp Central).
                SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       ANY_VALUE(u.EMAIL) AS EMAIL,
                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact
                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ
                       -- (live 2026-08-13: "expecting TIMESTAMP_NTZ(9) but got
                       -- TIMESTAMP_TZ(9) for column FIRST_TS" killed this arm on
                       -- every run, starving the AI coverage gate).
                       CONVERT_TIMEZONE('America/Chicago', MIN(c.USAGE_TIME))::TIMESTAMP_NTZ AS FIRST_TS,
                       CONVERT_TIMEZONE('America/Chicago', MAX(c.USAGE_TIME))::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM (
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    UNION ALL
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI'
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                ) c
                LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u ON u.USER_ID = c.USER_ID
                GROUP BY 1, 2, 3
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_code ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (code views) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                -- V146: repointed off the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY onto the canonical
                -- CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Not a drop-in: TOKEN_CREDITS -> CREDITS; START_TIME
                -- is TIMESTAMP_LTZ (was NTZ) so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (same TZ->NTZ MERGE
                -- guard as the ai_code arm, V078); and there is NO scalar TOKENS column -- token counts
                -- live in the METRICS ARRAY as {"key":{"metric":"input"|"output","unit":"tokens"},"value":N},
                -- so LATERAL FLATTEN sums value where unit='tokens'. CREDITS + REQUESTS are deduped to
                -- once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
                -- empty METRICS, still counted once) so the FLATTEN fan-out cannot multiply them.
                SELECT f.START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(NULLIF(f.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(f.START_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(f.START_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN 1 END) AS REQUESTS,
                       SUM(CASE WHEN m.VALUE:key:unit::STRING = 'tokens'
                                THEN m.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
                       ROUND(SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0
                                      THEN COALESCE(f.CREDITS, 0) ELSE 0 END), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f,
                     LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m
                WHERE f.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_functions ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE (DAILY scope): same token-gated stamp.
        -- Only posture / AI sources whose arm loaded advance; FACT_AI_USAGE_DAILY collapses
        -- its two arms (ai_code, ai_functions) to one row via GROUP BY so the MERGE matches
        -- its target exactly once.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_SECURITY_POSTURE_DAILY', 'posture'),
                    ('FACT_AI_USAGE_DAILY', 'ai_code'),
                    ('FACT_AI_USAGE_DAILY', 'ai_functions')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            -- V066 #23 AI FRESHNESS PARTIAL: FACT_AI_USAGE_DAILY has TWO independent arms
            -- (ai_code + ai_functions) mapped to the ONE physical source. The #11 per-token
            -- WHERE ARRAY_CONTAINS stamped the whole source fresh as soon as a SINGLE arm's
            -- token reached :loaded, so a half-loaded AI source read green. Gate the whole
            -- group: stamp a source only when EVERY one of its tokens loaded (both AI arms,
            -- or the lone posture arm). A partial AI load leaves the prior stamp standing, so
            -- the source reads as not-loaded-this-run (same treatment #11 gives a failed arm).
            GROUP BY f.SOURCE_NAME
            HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS,
            -- V167 (R1-016): the AI fact's loaded-from watermark. FACT_AI_USAGE_DAILY holds only days that
            -- had usage, so MIN(DAY) is the first AI use, not how far back this loader reached, and the
            -- app gate read it as the latter (blank 180d / 365d / Current-year AI panels). The HAVING above
            -- stamps the row only when BOTH AI arms loaded. Record the earliest whole day such a run covered
            -- (today - d + 1), kept as the deepest reach ever (LEAST): the daily d=3 run never narrows it.
            COVERAGE_FROM = IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY',
                                LEAST(COALESCE(t.COVERAGE_FROM, DATEADD('day', -:d + 1, CURRENT_DATE())),
                                      DATEADD('day', -:d + 1, CURRENT_DATE())),
                                t.COVERAGE_FROM)
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS,
                IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY', DATEADD('day', -:d + 1, CURRENT_DATE()), NULL));

    END IF;

    -- V066 #10 FALSE SUCCESS: the terminal RETURN used to always claim the marts loaded,
    -- even when an arm's EXCEPTION handler swallowed a failure and continued. Return a
    -- machine-readable verdict from the REQUIRED / OPTIONAL failure counters instead.
    IF (req_fail = 0) THEN
        RETURN 'MARTS OK (' || :SCOPE || ', ' || :d || 'd): ' || :loaded
               || IFF(:opt_fail > 0, '[' || :opt_fail || ' optional failed]', '');
    END IF;
    RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail || ' optional ('
           || :SCOPE || ', ' || :d || 'd): ' || :loaded;
END;
$$;

-- >>> derived:SP_NIGHTLY_RECONCILE  (from V064; R2-018 token-gated mark-and-sweep of the four wide-edge tables after the marts reload instead of DELETE-first, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rv VARCHAR;             -- V064 #9: each child loader's RETURN verdict, captured per CALL
    fails INT DEFAULT 0;    -- children whose RETURN carried a WITH ERRORS/FAIL token
    recon_start TIMESTAMP_NTZ;  -- V064 #9: run start, to scope the failure-log count
    logged_fails INT DEFAULT 0; -- APP_ERROR_LOG failure rows written during this run
BEGIN
    -- V064 #9: most child loaders SWALLOW arm failures (log a '%_failed%' row to
    -- APP_ERROR_LOG, then return a benign 'loaded' string), so a per-CALL return-string
    -- check catches only the two machine-verdict children. The AUTHORITATIVE signal is
    -- the count of failure rows the children logged during this run -- captured below.
    recon_start := CURRENT_TIMESTAMP();
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    -- V167 (R2-018): MART_WAREHOUSE_EFFICIENCY_DAILY, FACT_QUERY_ROLE_HOURLY, FACT_QUERY_SCHEMA_HOURLY and
    -- MART_TASK_GRAPH_DAILY are no longer DELETEd here: they are mark-and-swept after the marts CALL below.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());

    -- Pull the watermarks back so the loaders re-cover the window.
    -- V064 rec7: the daily loader now keeps FOUR per-source marks, not one
    -- shared DAILY_FACTS mark -- rewind all four here or SP_LOAD_DAILY_FACTS
    -- reads a current mark and the nightly daily re-coverage silently no-ops.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'HOURLY_FACTS',
                      'FACT_METERING_DAILY', 'FACT_TASK_DAILY',
                      'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY');

    -- V064 #4-drop ANALYSIS (KEEP -- verified NOT redundant): TASK_LOAD_DAILY runs
    -- SP_LOAD_DAILY_FACTS first (root, ~1 day back from its live watermark), then
    -- TASK_NIGHTLY_RECONCILE runs AFTER it, rewinds the four daily marks to -3d, and
    -- DELETEs FACT_METERING_DAILY D-3 (above). The re-CALL of SP_LOAD_DAILY_FACTS below
    -- is the RE-COVER step, NOT a duplicate of the root load: it repopulates the metering
    -- rows this proc just DELETEd AND re-reads the D-2/D-3 window (rewound watermark) for
    -- late-arriving ACCOUNT_USAGE data the 1-day-back root run cannot see. Dropping it
    -- would leave a permanent metering gap. Redundant? NO -> kept.
    --
    -- TODO(#7 staging-swap): the DELETE(D-2/D-3)-then-reload here is NOT atomic -- a child
    -- failure between the DELETEs (above) and these CALLs leaves a gap that the next hourly run
    -- (extract-fed D-2 marts) or the held per-source watermark (daily facts) re-covers. V167
    -- (R2-018): that did NOT hold for the four tables whose reload edge is wider than the hourly
    -- task's ('HOURLY', 2) -- MART_WAREHOUSE_EFFICIENCY_DAILY + MART_TASK_GRAPH_DAILY (D-3) and
    -- FACT_QUERY_ROLE_HOURLY + FACT_QUERY_SCHEMA_HOURLY (~now-3d): only this proc re-loads that
    -- edge, so one failed arm lost it for good. Those four are no longer DELETEd up front; they
    -- are MARK-AND-SWEPT after the SP_LOAD_MARTS_V27 CALL below.
    -- The robust fix (build-into-staging + atomic SWAP, or one
    -- transaction per fact family wrapping delete+reload) is DEFERRED: the reloaders are
    -- separate procs that contain their OWN BEGIN TRANSACTION/COMMIT (SP_LOAD_DAILY_FACTS,
    -- SP_LOAD_QH_EXTRACT) and DDL that autocommits (SP_LOAD_MARTS_V27 does CREATE OR
    -- REPLACE TEMPORARY TABLE), so they cannot run inside a single reconcile-owned
    -- transaction without committing it early or breaking the per-source watermark rewind.
    -- A correct deferral beats a broken reconcile; the #9 verdict below makes any
    -- mid-reconcile child failure LOUD so a gap never passes silently.
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(0);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    -- V167 (R2-018) MARK-AND-SWEEP. Every SP_LOAD_MARTS_V27 MERGE arm stamps LOAD_TS = CURRENT_TIMESTAMP()
    -- on UPDATE and the column DEFAULTs to it on INSERT, so a row this reload wrote has LOAD_TS >=
    -- recon_start; an older row inside the reload window is a key the reload no longer produces (a
    -- vanished warehouse / pipeline / role / schema) and is swept. Each sweep runs ONLY when that arm's
    -- own :loaded token is in THIS call's verdict (rv, still the marts verdict here), so a failed arm keeps
    -- its previous rows (stale but present) instead of leaving a D-3 hole no later run refills. The hour
    -- bound is the [3] / [4] arms' real lower edge GREATEST(D-3, ext_lo_hour); an empty extract gives NULL
    -- and sweeps nothing.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE())
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('wh_eff'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE())
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('graphs'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS >= (SELECT GREATEST(DATEADD('day', -3, CURRENT_DATE()),
                              DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))))
                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('role_hr'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS >= (SELECT GREATEST(DATEADD('day', -3, CURRENT_DATE()),
                              DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))))
                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('schema_hr'::VARIANT, SPLIT(:rv, ' '));
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;

    -- V064 #9: the AUTHORITATIVE failure signal. Every child loader logs a '%_failed%'
    -- row to APP_ERROR_LOG when an arm fails (fact_load_failed, mart_load_failed,
    -- extract_load_failed, cloud_svc_mart_failed, object_cost_load_failed, ...), INCLUDING
    -- the ones that swallow the error and return a benign 'loaded' string -- so a
    -- return-string check alone would miss 4 of the 5 children. Count what was logged
    -- during THIS run; the per-CALL return check above is a secondary catch for the two
    -- machine-verdict children (SP_LOAD_DAILY_FACTS, SP_LOAD_MARTS_V27).
    -- V064 #6 RUN SCOPING: count ONLY this reconcile's OWN child-loader failures. The
    -- prior filter counted every '%_failed%' row account-wide in the run window, so a
    -- CONCURRENT AlertScan 'rule_block_failed', a NotifyWebhook 'route_send_failed', an
    -- ObjectCost load, or any app-page error logged in the same minutes was mis-attributed
    -- to the reconcile and made an OK run report WITH ERRORS. APP_ERROR_LOG has NO
    -- session/query-tag column to fence on (its columns are LOGGED_AT, PAGE, ERROR_TYPE,
    -- ERROR_MESSAGE, CONTEXT, ROLE_NAME -- see V001), and the children log without a run
    -- id, so the tightest CORRECT filter is PAGE. The five children that swallow+log write
    -- exactly three pages: ExtractLoader (SP_LOAD_QH_EXTRACT + its inner
    -- SP_LOAD_CLOUD_SVC_MART), DailyFacts (SP_LOAD_DAILY_FACTS), MartLoader
    -- (SP_LOAD_MARTS_V27). SP_LOAD_HOURLY_FACTS + SP_LOAD_OPS_DIAG do NOT swallow -- a
    -- failure there aborts THIS proc and FAILs the task run directly. (ExtractLoader can
    -- also be written by the concurrent hourly extract task; that is still a real loader
    -- failure in the window, not a cross-subsystem mis-attribution, so counting it is
    -- acceptable -- the secondary per-CALL verdict check above catches the reconcile's own
    -- extract call regardless.)
    SELECT COUNT(*) INTO :logged_fails
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE LOGGED_AT >= :recon_start
      AND PAGE IN ('ExtractLoader', 'DailyFacts', 'MartLoader')
      AND (ERROR_TYPE ILIKE '%_failed%' OR ERROR_TYPE ILIKE '%WITH ERRORS%');
    IF (fails > 0 OR logged_fails > 0) THEN
        RETURN 'RECONCILE WITH ERRORS: ' || :logged_fails || ' loader failure(s) logged'
            || IFF(:fails > 0, ' + ' || :fails || ' child verdict(s) non-success', '')
            || ' (metering + full-retention marts 3 days, extract-fed marts 2); inspect APP_ERROR_LOG';
    END IF;
    RETURN 'RECONCILE OK - nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2); no child loader failures logged';
END;
$$;

-- >>> derived:SP_LOAD_PATTERN_COST  (from V120; R2-010 atomic DELETE + INSERT of the window instead of the COMPANY-keyed MERGE, + the PATTERN-RESTAMP COVERAGE_FROM stamp, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;   -- V167 (R2-010 D1): ONE lower bound for the DELETE and both source filters
BEGIN
    lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE());
    -- V167 (R2-010 D2): COMPANY is COMPANY_FOR_WAREHOUSE() at load time and part of the grain, so the
    -- V037-V120 MERGE could not match a row whose warehouse was re-mapped: it INSERTed a second row
    -- under the new company and kept the old one, and the ALL scope summed both. Replace the window
    -- atomically instead (the SP_LOAD_OBJECT_COST idiom, V139): a failed INSERT rolls the DELETE back
    -- and readers keep the previous fill. COMPANY stays in the grain (the mart has no WAREHOUSE_NAME,
    -- so one DAY / HASH / DB can legitimately span companies). No DDL inside the transaction.
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
     WHERE DAY >= :lo;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
        (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)
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
                    WHERE START_TIME >= :lo
                    GROUP BY QUERY_ID
                ) a
                JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                  ON q.QUERY_ID = a.QUERY_ID
                 AND q.START_TIME >= :lo
                WHERE q.QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2, 3, 4
            ) g
        ) m
        GROUP BY 1, 2, 3, 4;
    COMMIT;

    -- V068: loader-owned freshness stamp (V041-R6 pattern; this standalone-task loader
    -- was missed in the V041 handoff, freezing its SOURCE_FRESHNESS_STATE row at apply
    -- time). LAST_LOAD_TS is a RUN stamp (CURRENT_TIMESTAMP()), not MAX(LOAD_TS) of the
    -- mart, so a window with ZERO source events still reads fresh - no news is not
    -- no load.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_PATTERN_COST_DAILY' AS SOURCE_NAME, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS,
               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT,
               :lo AS COVERAGE_FROM   -- V167 (PATTERN-RESTAMP D6): the first day this run replaced
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader',
        -- V167 (PATTERN-RESTAMP D6): the deepest day an atomic reload ever replaced, NULL-safe both ways
        -- (a NULL DAYS_BACK keeps the old stamp). The app lifts its 90-day pattern cap only for a read
        -- window whose first day is on or after it. Reached only after the COMMIT (a failure re-raises).
        COVERAGE_FROM = LEAST(COALESCE(t.COVERAGE_FROM, s.COVERAGE_FROM), COALESCE(s.COVERAGE_FROM, t.COVERAGE_FROM))
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);
    RETURN 'OK';
EXCEPTION   -- V167 (R2-010 D5): never leave the window deleted; re-raise so the task still fails visibly
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- R2-010 one-time repair (after the atomic loader is in place): drop the stale-company twins the COMPANY-keyed
-- MERGE left behind (the V037 -> V044 -> V047 era, plus any Apply-mapping remap since). Scan-free: reads only the
-- mart. A group's legitimate company rows share their last covering run's LOAD_TS; the 10-minute slack absorbs
-- one load statement. Groups of one row are never touched.
DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
USING (
    SELECT DAY, QUERY_HASH, DATABASE_NAME, MAX(LOAD_TS) AS NEWEST_TS
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1
) g
WHERE t.DAY = g.DAY AND t.QUERY_HASH = g.QUERY_HASH AND t.DATABASE_NAME = g.DATABASE_NAME
  AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 167 AS VERSION,
       'Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (R2-015, R2-014, R2-052 (also R2-017), R1-016, R2-018, R2-010, PATTERN-RESTAMP). SOURCE_FRESHNESS_STATE gains a nullable COVERAGE_FROM DATE. SP_LOAD_MARTS_V27 (re-derived from V159): arm [1] pads its query-span source one day back with an END_TIME floor, so a query crossing the window edge marks its hours active (idle no longer overstated); arm [6] reads one lead-in day and keeps only runs whose first attempt is in the window (no phantom child-named pipeline rows); arm [9] keys Cortex Code DAY and FIRST_TS/LAST_TS in Central (USAGE_TIME is TIMESTAMP_TZ); the DAILY freshness MERGE stamps COVERAGE_FROM for FACT_AI_USAGE_DAILY when both AI arms loaded (deepest reach, -d+1). SP_NIGHTLY_RECONCILE (re-derived from V064) no longer DELETEs MART_WAREHOUSE_EFFICIENCY_DAILY, MART_TASK_GRAPH_DAILY, FACT_QUERY_ROLE_HOURLY and FACT_QUERY_SCHEMA_HOURLY up front; it sweeps rows the reload did not re-stamp (LOAD_TS before the run) only when that arm loaded, so a failed arm no longer leaves a permanent D-3 hole. SP_LOAD_PATTERN_COST (re-derived from V120) replaces its window atomically (DELETE + INSERT, ROLLBACK + RAISE) instead of a COMPANY-keyed MERGE that left a stale-company twin after a COMPANY_SCOPE remap, and stamps COVERAGE_FROM. One-time in-migration repair: a scan-free DELETE of MART_PATTERN_COST_DAILY rows older than their group newest LOAD_TS by more than 10 minutes. No task, rule, grant or view change; nothing is CALLed at apply time. The heavy reloads (AI DAILY 365 reload-then-prune, pattern 364, HOURLY N, the arm [6] 364-day rebuild) are owner-run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 167);
