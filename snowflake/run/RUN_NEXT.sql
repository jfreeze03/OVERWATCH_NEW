-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (APPLY V142: posture arm single-scans CREDENTIALS + GRANTS)
--
--  A4 (approved). SP_LOAD_MARTS_V27's [7] security-posture arm scanned CREDENTIALS twice and
--  GRANTS_TO_USERS twice; V142 collapses each to ONE scan via COUNT_IF + UNPIVOT, output-equivalent.
--  Shipped v4.540.0 (repo: V142__posture_arm_single_scan.sql; CI green; adversarially verified).
--
--  Run All as SNOW_ACCOUNTADMINS. Apply after V141. Idempotent (proc CREATE OR REPLACE + guarded
--  SCHEMA_VERSION insert). STEP 2 [B] is the LIVE EQUIVALENCE PROOF -- it computes each metric the
--  OLD (two-scan) way and the NEW (one-scan) way on real data; every MATCH must be TRUE. Paste it back.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;

-- ============================ STEP 1: APPLY V142 =============================

-- V142__posture_arm_single_scan.sql
--
-- A4 (owner-approved, half 2 of the A1+A4 bundle): the SP_LOAD_MARTS_V27 [7] security-posture arm
-- scanned SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS twice (EXPIRING_CRED_10D + EXPIRED_CRED) and
-- GRANTS_TO_USERS twice (GRANT_CHANGES_24H + BREAKGLASS_GRANTS_30D) as separate UNION members.
-- This re-derives the proc so each source is scanned ONCE with COUNT_IF conditional aggregation and
-- UNPIVOTed back to the identical (DAY, METRIC, COMPANY, VALUE) rows the MERGE consumes.
--
-- Output-equivalent by construction: COUNT_IF(cond) == COUNT(*) WHERE cond, and the GRANTS one-scan
-- WHERE (created >= -30d OR deleted >= -24h) is a SUPERSET of the rows either metric counts, with each
-- COUNT_IF re-applying its exact original predicate. The MART_SECURITY_POSTURE_DAILY MERGE still lands
-- one row per (DAY, METRIC, COMPANY). Everything else in the proc is byte-identical to V127 (test_v142
-- proves it). Run the equivalence check staged with the apply (old vs new counts) before trusting it.
--
-- NOT included: the SP_ANOMALY_SWEEP TABLE_DML_HISTORY double-scan -- its two scans use different
-- windows/filters/grains and are separately exception-isolated on purpose, so collapsing them needs
-- shared temp-staging that couples their failure modes (the A3 pattern, deferred). Left as-is.
--
-- Proc-only; no schema/rule/task change. Apply AFTER V141. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20142, 'V142 requires V141 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 141) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27 (from V127; posture arm CREDENTIALS + GRANTS single-scanned, V142)
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
                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
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
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
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
                FROM runs GROUP BY 1, 2, 3, 4
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

        -- [6b] per-node task timing (queue + exec delay) -> MART_TASK_NODE_DAILY
        -- Observability for the deferred reconcile-scheduling work: the
        -- SCHEDULED_TIME->QUERY_START_TIME dispatch delay (which the pipeline-grain
        -- arm [6] discards) quantifies the 06:40/06:45 XSMALL contention. Own
        -- guarded arm; touches no existing statement; one TASK_HISTORY scan at the
        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).
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
                SELECT c.USAGE_TIME::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       ANY_VALUE(u.EMAIL) AS EMAIL,
                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact
                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ
                       -- (live 2026-08-13: "expecting TIMESTAMP_NTZ(9) but got
                       -- TIMESTAMP_TZ(9) for column FIRST_TS" killed this arm on
                       -- every run, starving the AI coverage gate).
                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,
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
                SELECT START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(START_TIME) AS FIRST_TS,
                       MAX(START_TIME) AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
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
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 142 AS VERSION,
       'A4 posture-arm single-scan: SP_LOAD_MARTS_V27 [7] security-posture arm now scans ACCOUNT_USAGE.CREDENTIALS once (EXPIRING_CRED_10D + EXPIRED_CRED via COUNT_IF + UNPIVOT) and GRANTS_TO_USERS once (GRANT_CHANGES_24H + BREAKGLASS_GRANTS_30D, one-scan superset WHERE + COUNT_IF), instead of two scans each. Output-equivalent (COUNT_IF(cond)==COUNT(*) WHERE cond; the grants WHERE is a superset so no counted row is dropped); the posture MERGE still lands one row per (DAY, METRIC, COMPANY). Re-derived from V127, byte-identical outside the posture arm. Did NOT touch the SP_ANOMALY_SWEEP TABLE_DML_HISTORY scans (different windows/grains + deliberate per-arm isolation; the A3 temp-staging pattern). Proc-only, no schema/rule/task change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 142);


-- ======================= STEP 2: VERIFY (paste results) =====================
-- [A] Version recorded (expect 142).
SELECT VERSION FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 142;

-- [B] LIVE EQUIVALENCE PROOF. Each metric computed the OLD two-scan way vs the NEW one-scan way on
--     your real data. EVERY row must show MATCH = TRUE (and OLD_VALUE = NEW_VALUE). Read-only.
WITH old_way AS (
    SELECT 'EXPIRING_CRED_10D' AS METRIC, COUNT(*) AS VAL
    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
    WHERE EXPIRATION_DATE IS NOT NULL
      AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'EXPIRED_CRED', COUNT(*)
    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
    WHERE EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()
    UNION ALL
    SELECT 'GRANT_CHANGES_24H', COUNT(*)
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'BREAKGLASS_GRANTS_30D', COUNT(*)
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
      AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
),
new_way AS (
    SELECT cu.METRIC, cu.VALUE AS VAL
    FROM (
        SELECT COUNT_IF(EXPIRATION_DATE IS NOT NULL
                        AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS "EXPIRING_CRED_10D",
               COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS "EXPIRED_CRED"
        FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
    ) UNPIVOT (VALUE FOR METRIC IN ("EXPIRING_CRED_10D", "EXPIRED_CRED")) cu
    UNION ALL
    SELECT gu.METRIC, gu.VALUE
    FROM (
        SELECT COUNT_IF(CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                        OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS "GRANT_CHANGES_24H",
               COUNT_IF(DELETED_ON IS NULL
                        AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                        AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS "BREAKGLASS_GRANTS_30D"
        FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
        WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
           OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
    ) UNPIVOT (VALUE FOR METRIC IN ("GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D")) gu
)
SELECT o.METRIC, o.VAL AS OLD_VALUE, n.VAL AS NEW_VALUE, (o.VAL = n.VAL) AS MATCH
FROM old_way o JOIN new_way n ON o.METRIC = n.METRIC
ORDER BY o.METRIC;


-- =====================================================================
--  PART B: NATIVE BUDGET READ GRANTS  (for the v4.544 "Native Snowflake budget" panel)
--
--  Lets the app's owner's-rights role read Snowflake's native account budget
--  (SNOWFLAKE.CORE.BUDGET) on Cost Intelligence > Contract & Forecast. Read-only:
--  BUDGET_VIEWER is the READ app role (BUDGET_ADMIN, the write one, is NOT granted).
--  IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE is very likely ALREADY held (the app reads
--  ACCOUNT_USAGE broadly) -> that grant is then a harmless no-op. Independent of the V142
--  apply above; safe to run any time. The panel is toggle-gated + probe=True, so until
--  these are granted (or a budget is configured) it simply shows a "needs setup" note.
-- =====================================================================

USE ROLE ACCOUNTADMIN;
GRANT APPLICATION ROLE SNOWFLAKE.BUDGET_VIEWER TO ROLE SNOW_ACCOUNTADMINS;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_ACCOUNTADMINS;

-- Verify the app role can now read the account budget's month-to-date spend (paste the result).
-- Runs AS the app's role to prove the panel will work. If the table function rejects the
-- expression args, replace both with a literal for the current month, e.g. '2026-09'.
USE ROLE SNOW_ACCOUNTADMINS;
SELECT SERVICE_TYPE, SUM(CREDITS_USED) AS CREDITS
FROM TABLE(SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!GET_SERVICE_TYPE_USAGE_V2(
        TO_CHAR(CURRENT_DATE(), 'YYYY-MM'), TO_CHAR(CURRENT_DATE(), 'YYYY-MM')))
GROUP BY SERVICE_TYPE
ORDER BY CREDITS DESC;


-- =====================================================================
--  PART C: APPLY V143  (QOIE Slice 2 -- operator-stats collector)
--
--  Shipped v4.549.0 (repo: V143__query_operator_stats_collector.sql; CI green;
--  adversarially verified 4-lens -> STAGE-WITH-FIXES applied). Creates
--  FACT_QUERY_OPERATOR_STATS_DAILY + SP_LOAD_QUERY_OPERATOR_STATS + a daily 07:20
--  task that loops the recent (2-day) expensive query_ids and calls
--  GET_QUERY_OPERATOR_STATS per id for operator-level join-explosion / spill /
--  anatomy / pruning stats -- the profile data QOIE Slice 1 (query-level) cannot see.
--
--  Run All as SNOW_ACCOUNTADMINS. Apply AFTER V142 above (the -20143 guard enforces
--  it and RAISEs if V142 is not yet applied). Idempotent: CREATE OR REPLACE + IF NOT
--  EXISTS + guarded SCHEMA_VERSION insert + incremental collector (NOT EXISTS skip).
--  The first-fill CALL runs inside the apply; landing 0 rows is a PRIVILEGE signal,
--  not an apply failure (see STEP 2). Paste STEP 2 [A]-[E] back.
--
--  PRIVILEGE: GET_QUERY_OPERATOR_STATS needs OPERATE or MONITOR on the warehouse each
--  target query ran on (NOT ACCOUNTADMIN, NOT query-ownership). SNOW_ACCOUNTADMINS
--  (the proc owner, EXECUTE AS OWNER) should already hold this fleet-wide; if STEP 2
--  [C] lands 0 rows with the collector reporting skipped>0, grant it per warehouse:
--      GRANT MONITOR ON WAREHOUSE <wh> TO ROLE SNOW_ACCOUNTADMINS;
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;

-- ------------------- STEP 1: APPLY V143 (verbatim migration body) -------------------
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

-- ======================= STEP 2: VERIFY (paste results) =====================
-- [A] Version recorded (expect 143).
SELECT VERSION FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 143;

-- [B] Re-run the collector once by hand (expect a verdict like
--     'OK landed=<n> queries=<n> skipped=<n>', no ERROR). It is incremental, so this
--     second run mostly skips what the first-fill already collected -- that is expected.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(30);

-- [C] Rows landed + scope sanity (expect ROWS_LANDED > 0, NEWEST recent). If ROWS_LANDED = 0
--     and the collector said skipped>0, it is almost always the warehouse MONITOR grant above.
SELECT COUNT(*)                 AS ROWS_LANDED,
       COUNT(DISTINCT QUERY_ID) AS QUERIES,
       COUNT(DISTINCT COMPANY)  AS COMPANIES,
       MAX(QUERY_DAY)           AS NEWEST_QUERY_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY;

-- [D] *** MANDATORY SCALE PROBE (adversarial-review action -- do NOT skip) ***
--     The loader stores EXECUTION_TIME_BREAKDOWN:overall_percentage AS-IS into OP_TIME_PCT
--     (SCAN_PCT is *100). Confirm OP_TIME_PCT is on a 0-100 scale like SCAN_PCT.
--     If MAX_OP_TIME_PCT comes back ~1.0 (a 0-1 fraction) instead of up to ~100, tell me --
--     the loader needs a '* 100' on that column before any reader trusts it. If it spans
--     0-100, we are correct and I will record the confirmed scale.
SELECT ROUND(MAX(OP_TIME_PCT), 3)  AS MAX_OP_TIME_PCT,
       ROUND(MAX(SCAN_PCT), 3)     AS MAX_SCAN_PCT,
       ROUND(MAX(ROW_MULTIPLE), 2) AS MAX_ROW_MULTIPLE
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY;

-- [E] Eyeball a few exploding-join / spill operators (paste a couple of rows).
SELECT QUERY_ID, OPERATOR_TYPE, INPUT_ROWS, OUTPUT_ROWS, ROW_MULTIPLE,
       REMOTE_SPILL_GB, OP_TIME_PCT
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY
WHERE OPERATOR_TYPE IN ('Join', 'CartesianJoin') OR COALESCE(REMOTE_SPILL_GB, 0) > 0
ORDER BY COALESCE(REMOTE_SPILL_GB, 0) DESC, COALESCE(ROW_MULTIPLE, 0) DESC
LIMIT 10;


-- =====================================================================
--  PART D: APPLY V144  (operator OP_TIME_PCT -> 0-100 scale)
--
--  Shipped v4.553.0 (repo: V144__operator_time_pct_scale.sql; CI green). The STEP-2 [D]
--  probe under PART C confirmed EXECUTION_TIME_BREAKDOWN:overall_percentage is a 0-1
--  FRACTION (MAX(OP_TIME_PCT)=1.0 vs MAX(SCAN_PCT)=100), so V143 stored a *_PCT column as
--  0-1. This re-derives SP_LOAD_QUERY_OPERATOR_STATS to store overall_percentage * 100, so
--  OP_TIME_PCT is 0-100 like SCAN_PCT.
--
--  Run All as SNOW_ACCOUNTADMINS. Apply AFTER V143 above (the -20144 guard enforces it).
--  PROC-ONLY: fact table + daily task unchanged; NO first-fill; NO backfill (existing 0-1
--  rows age out in the 30-day retention and are never displayed raw -- the app already shows
--  a scale-invariant TIME_SHARE_PCT). Idempotent. Paste STEP 2 back.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;

-- ------------------- STEP 1: APPLY V144 (verbatim migration body) -------------------
-- V144__operator_time_pct_scale.sql — store OP_TIME_PCT on a 0-100 scale (owner probe fix).
--
--   The QOIE Slice 2 collector (V143) stored OP_TIME_PCT =
--   EXECUTION_TIME_BREAKDOWN:overall_percentage AS-IS. The owner's STEP-2 probe confirmed
--   Snowflake returns overall_percentage as a 0-1 FRACTION (MAX(OP_TIME_PCT)=1.0 vs
--   MAX(SCAN_PCT)=100), so a column named *_PCT was holding 0-1 — a trap for any direct
--   reader/CSV. This re-derives SP_LOAD_QUERY_OPERATOR_STATS to multiply by 100 so the stored
--   column is 0-100 like SCAN_PCT and the _PCT humanize convention.
--
--   PROC-ONLY: the fact table + daily task from V143 are unchanged (the same NUMBER(9,2)
--   column holds 0-100). The proc body is byte-identical to V143 except the one
--   overall_percentage extraction (adds `* 100`). No first-fill CALL — the daily task
--   already runs it. Existing 0-1 rows age out within the 30-day retention and are never
--   displayed raw (the reader shows a scale-INVARIANT TIME_SHARE_PCT = OP_TIME_PCT /
--   SUM(OP_TIME_PCT) OVER () * 100, which is correct under either scale, and each query's
--   operators share one collection run so they share one scale — no mixed-scale-within-a-query).
--   So NO backfill is needed. Idempotent; safe to re-run. Apply AFTER V143.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20144, 'V144 requires V143 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 143) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V143; overall_percentage now * 100, V144)
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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 144 AS VERSION,
       'Operator-stats OP_TIME_PCT scale fix: re-derive SP_LOAD_QUERY_OPERATOR_STATS to store EXECUTION_TIME_BREAKDOWN:overall_percentage * 100 (owner STEP-2 probe confirmed it is a 0-1 fraction; MAX was 1.0 vs SCAN_PCT 100), so the *_PCT column is 0-100 like SCAN_PCT. Proc-only, byte-identical to V143 except the one overall_percentage extraction. Fact/task unchanged (same NUMBER(9,2) column); no first-fill (daily task runs it); no backfill (existing 0-1 rows age out in 30d and are never displayed raw - the reader shows scale-invariant TIME_SHARE_PCT).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 144);

-- ======================= STEP 2: VERIFY (paste results) =====================
-- [A] Version recorded (expect 144).
SELECT VERSION FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 144;

-- [B] The re-derived proc scales by 100 (expect SCALED = TRUE).
SELECT GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(FLOAT)')
       ILIKE '%overall_percentage::FLOAT * 100%' AS SCALED;

-- [C] Optional: run the collector once, then re-probe the scale. Newly-collected queries
--     land 0-100; already-collected rows stay 0-1 until they age out (retention). So right
--     after this, MAX(OP_TIME_PCT) trends toward ~100 as new queries are profiled -- it is
--     NOT expected to be exactly 100 immediately. (No action needed either way.)
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(30);
SELECT ROUND(MAX(OP_TIME_PCT), 2) AS MAX_OP_TIME_PCT,
       ROUND(MAX(SCAN_PCT), 2)    AS MAX_SCAN_PCT
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY;


-- =====================================================================
--  PART E: APPLY V145  (savings ledger books its lever — no more "unclassified")
--
--  Shipped v4.554.0 (repo: V145__ledger_autobook_stamp_finding_type.sql; CI green;
--  adversarially verified = SHIP, no fan-out, idempotent). SP_LEDGER_AUTOBOOK (the
--  dominant savings-ledger path) never set FINDING_TYPE, so the Decision Studio ROI
--  "by lever" chart pooled every autobooked saving into "unclassified". This re-derives
--  the proc to stamp FINDING_TYPE from the source WAREHOUSE_CHANGE_REGISTRY.SETTING
--  (SIZE -> RESIZE) + a one-time backfill of existing rows.
--
--  Run All as SNOW_ACCOUNTADMINS. Apply AFTER V144 above (the -20145 guard enforces it).
--  PROC-ONLY re-derive (byte-identical to V118 except the INSERT FINDING_TYPE column+value)
--  + an idempotent backfill (FINDING_TYPE-is-empty guard; NULL-source manual rows stay
--  "unclassified"). No new object. The app already recovers the lever at read time, so the
--  chart is right even before this applies; this makes the stored column honest. Paste STEP 2.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;

-- ------------------- STEP 1: APPLY V145 (verbatim migration body) -------------------
-- V145__ledger_autobook_stamp_finding_type.sql — book the savings lever (owner ask).
--
--   The Decision Studio ROI "Where the realized savings come from — by lever" chart showed
--   ALL verified savings as "unclassified". Root cause: SP_LEDGER_AUTOBOOK (the DOMINANT
--   booking path — every detected warehouse cost-lever change books through it) never set
--   SAVINGS_LEDGER.FINDING_TYPE (added later by V053), even though the lever is right there
--   in the INSERT's source row (WAREHOUSE_CHANGE_REGISTRY.SETTING). So autobook rows landed
--   with FINDING_TYPE = NULL and the by-lever rollup pooled them all into 'unclassified'.
--   (The $0 ESTIMATED_USD is by design — no invented numbers — so Realization % is N/A there.)
--
--   Fix: re-derive SP_LEDGER_AUTOBOOK (from V118, its current form) to STAMP FINDING_TYPE from
--   r.SETTING, mapping SIZE -> 'RESIZE' so autobook resize rows share the app RESIZE bucket
--   (the same SIZE->RESIZE alias proven_fix_transfer already uses). Proc body is byte-identical
--   to V118 except the INSERT column list + value. STEP 2 is a ONE-TIME idempotent backfill that
--   stamps existing autobook rows from the registry, guarded by FINDING_TYPE-is-empty so a re-run
--   is a no-op and NULL-source manual rows stay 'unclassified'. Apply AFTER V144. Idempotent.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20145, 'V145 requires V144 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 144) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LEDGER_AUTOBOOK (from V118; stamp FINDING_TYPE from registry SETTING, V145)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rate NUMBER;
BEGIN
    SELECT COALESCE(TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :rate
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- Book detected cost-lever changes as ESTIMATED $0. V145: also stamp FINDING_TYPE from the
    -- source SETTING (SIZE -> RESIZE) so the lever is on the row, not just in the free-text NOTE.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, SOURCE_CHANGE_ID, FINDING_TYPE)
    SELECT 'Detected ' || r.SETTING || ' change on ' || r.WAREHOUSE_NAME || ': '
               || COALESCE(r.OLD_VALUE, '?') || ' -> ' || COALESCE(r.NEW_VALUE, '?'),
           'ESTIMATED',
           0,
           'SELECT * FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY WHERE CHANGE_ID = ''' || r.CHANGE_ID || '''',
           'Auto-booked from the daily warehouse-change scan; the 14-day measured verdict settles it.',
           r.CHANGE_ID,
           CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
    WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
                      WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID)
      AND (
            (r.SETTING = 'AUTO_SUSPEND'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'MAX_CLUSTERS'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'SCALING_POLICY'
             AND UPPER(COALESCE(r.NEW_VALUE, '')) = 'ECONOMY'
             AND UPPER(COALESCE(r.OLD_VALUE, '')) = 'STANDARD')
         OR (r.SETTING = 'SIZE'
             AND CASE UPPER(REPLACE(COALESCE(r.NEW_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 99 END
               < CASE UPPER(REPLACE(COALESCE(r.OLD_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)
      );

    -- Settle forward-only. LBA-1: rank co-occurring levers within one measured
    -- window; the primary (RN=1) carries the full warehouse saving, the rest settle
    -- VERIFIED at $0 so the physical saving is booked exactly once.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),
           VERIFIED_USD = CASE
                              WHEN s.WH_SAVED_MONTHLY_USD < 5 THEN NULL
                              WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)
                              ELSE 0
                          END,
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured '
                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' -> '
                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))
                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))
                        || 'd (' || s.VERDICT || '); floor $5/mo.'
                        || IFF(s.WH_SAVED_MONTHLY_USD >= 5 AND s.RN > 1,
                               ' | LBA-1 co-attributed: warehouse saving booked once on change '
                               || s.PRIMARY_CHANGE_ID || '.', ''), 2000)
      FROM (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                   r.BASELINE_CREDITS_PER_DAY AS BASE,
                   r.AFTER_CREDITS_PER_DAY AS AFT,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
                   FIRST_VALUE(r.CHANGE_ID) OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS PRIMARY_CHANGE_ID,
                   (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                       * :rate * 30 AS WH_SAVED_MONTHLY_USD
              FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
             WHERE r.VERDICT <> 'PENDING'
               -- Rank ONLY changes that Step-1 actually booked a ledger row for (the
               -- saving-direction levers). The registry also holds non-saving changes
               -- (SIZE up, AUTO_SUSPEND up) with the SAME measured-window signature but
               -- NO ledger row; if one of those won RN=1 it would carry the full saving
               -- into a row that doesn't exist while the genuine saving lever settled at
               -- $0 -- booking a real saving as ZERO. Restricting the population to booked
               -- levers guarantees the RN=1 primary always has a row to receive the USD.
               AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
                            WHERE l2.SOURCE_CHANGE_ID = r.CHANGE_ID)) s
     WHERE l.SOURCE_CHANGE_ID = s.CHANGE_ID
       AND l.STATE = 'ESTIMATED';

    RETURN 'OK';
END;
$$;

-- Step 2 (one-time, idempotent): backfill FINDING_TYPE on existing autobook rows from the
-- source registry SETTING (SIZE -> RESIZE), so the historical by-lever rollup is right too.
-- Guarded by FINDING_TYPE-is-empty, so a re-run is a no-op and NULL-source manual rows (no
-- SOURCE_CHANGE_ID -> no registry match) stay 'unclassified'.
EXECUTE IMMEDIATE
$$
BEGIN
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET FINDING_TYPE = CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END
      FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
     WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID
       AND COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''), '') = '';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 145 AS VERSION,
       'SP_LEDGER_AUTOBOOK stamps FINDING_TYPE (the savings lever) from the source WAREHOUSE_CHANGE_REGISTRY.SETTING, SIZE -> RESIZE, so autobooked savings are no longer all "unclassified" in the Decision Studio ROI by-lever chart. Proc-only re-derive (byte-identical to V118 except the INSERT FINDING_TYPE column+value) + a one-time idempotent backfill of existing autobook rows (guarded by FINDING_TYPE-is-empty; NULL-source manual rows stay unclassified). The app savings_ledger() reader also recovers the lever via the registry join so the display is correct before this applies. No new object; teardown unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 145);

-- ======================= STEP 2: VERIFY (paste results) =====================
-- [A] Version recorded (expect 145).
SELECT VERSION FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 145;

-- [B] Verified savings now break out BY LEVER (expect real levers -- RESIZE / AUTO_SUSPEND /
--     MAX_CLUSTERS / SCALING_POLICY -- not one "unclassified" pile).
SELECT COALESCE(NULLIF(TRIM(FINDING_TYPE), ''), 'unclassified') AS LEVER,
       COUNT(*) AS ITEMS, ROUND(SUM(COALESCE(VERIFIED_USD, 0)), 2) AS VERIFIED_USD
FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
WHERE STATE = 'VERIFIED'
GROUP BY 1
ORDER BY VERIFIED_USD DESC;

-- [C] Backfill left no autobook row (has a SOURCE_CHANGE_ID) still untyped (expect 0). A
--     non-zero count = rows whose SOURCE_CHANGE_ID has no matching registry row (rare) --
--     they legitimately stay "unclassified"; report the number.
SELECT COUNT(*) AS AUTOBOOK_ROWS_STILL_UNTYPED
FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
WHERE SOURCE_CHANGE_ID IS NOT NULL
  AND COALESCE(NULLIF(TRIM(FINDING_TYPE), ''), '') = '';
