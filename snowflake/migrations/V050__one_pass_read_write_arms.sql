-- V050__one_pass_read_write_arms.sql — one-pass loader + read/write arms
-- (Codex adjudication 2026-07-27, Tranche B; owner go: "tranche B").
--
--   One-pass staging: V049 scanned QUERY_ATTRIBUTION_HISTORY twice and
--   flattened ACCESS_HISTORY four times per run; V050 stages each once in
--   session-scoped temp tables and both attribution inserts read the stages.
--   Read/write arms: the equal split keeps credits/N additivity but labels
--   each share by role — QUERY_COMPUTE_WRITE (production: the cost of
--   building the object) vs QUERY_COMPUTE_READ (consumption). Write wins on
--   a read+write collapse, so the object keeps ONE share.
--   Riding fix: a query whose only touched object has a NULL name previously
--   vanished (V049 counted it attributed; the split had no row); its credits
--   now land in QUERY_COMPUTE_RESIDUAL.
--
--   Readers treat legacy 'QUERY_COMPUTE' rows (pre-V050 days outside the
--   reload window) and the two new arms as query credits alike. Proc swap +
--   14-day reload; no new permanent objects (temp stages are session-scoped).
--
-- Derivation law: SP_LOAD_OBJECT_COST from V049 verbatim + one enumerated
-- edit (the two attribution inserts become the staged, role-labeled design);
-- tests/test_v050_one_pass.py re-derives and byte-compares. Apply AFTER
-- V049. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20050, 'V050 requires V049 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 49) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_OBJECT_COST
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY WHERE DAY >= :lo;

    -- Direct per-object serverless arms -----------------------------------
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT START_TIME::DATE, COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN'),
           'TABLE', 'CLUSTERING',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME), SUM(COALESCE(CREDITS_USED, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
    WHERE START_TIME >= :lo AND CREDITS_USED > 0
    GROUP BY 1, 2, 3, 4, 5;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT START_TIME::DATE, COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN'),
           'MATERIALIZED_VIEW', 'MV_REFRESH',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME), SUM(COALESCE(CREDITS_USED, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.MATERIALIZED_VIEW_REFRESH_HISTORY
    WHERE START_TIME >= :lo AND CREDITS_USED > 0
    GROUP BY 1, 2, 3, 4, 5;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT START_TIME::DATE, COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN'),
           'TABLE', 'SEARCH_OPT',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME), SUM(COALESCE(CREDITS_USED, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY
    WHERE START_TIME >= :lo AND CREDITS_USED > 0
    GROUP BY 1, 2, 3, 4, 5;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT START_TIME::DATE, COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TASK_NAME, 'UNKNOWN'),
           'TASK', 'SERVERLESS_TASK',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME), SUM(COALESCE(CREDITS_USED, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY
    WHERE START_TIME >= :lo AND CREDITS_USED > 0
    GROUP BY 1, 2, 3, 4, 5;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT START_TIME::DATE, COALESCE(PIPE_NAME, 'UNKNOWN_PIPE'), 'PIPE', 'SNOWPIPE',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(SPLIT_PART(PIPE_NAME, '.', 1)), SUM(COALESCE(CREDITS_USED, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.PIPE_USAGE_HISTORY
    WHERE START_TIME >= :lo AND CREDITS_USED > 0
    GROUP BY 1, 2, 3, 4, 5;

    -- One-pass staging (V050): QUERY_ATTRIBUTION_HISTORY is aggregated ONCE
    -- and ACCESS_HISTORY flattened once per array (V049 re-scanned QAH per
    -- insert and flattened AH four times); both attribution inserts below
    -- read the session-scoped stages. Same staged-extract pattern as V041's
    -- OW_QH_EXTRACT.
    CREATE OR REPLACE TEMPORARY TABLE DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE AS
    SELECT QUERY_ID, MIN(START_TIME)::DATE AS DAY,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= :lo
    GROUP BY QUERY_ID
    HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0;

    -- Read/write role rides the union (V050): write wins when one query both
    -- reads and writes an object, so the object keeps ONE share (additivity)
    -- and that share is labeled production, not consumption.
    CREATE OR REPLACE TEMPORARY TABLE DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE AS
    SELECT QUERY_ID, OBJECT_FQN, OBJECT_DOMAIN, MAX(IS_WRITE) AS IS_WRITE
    FROM (
        SELECT ah.QUERY_ID,
               f.value:"objectName"::STRING AS OBJECT_FQN,
               f.value:"objectDomain"::STRING AS OBJECT_DOMAIN,
               0 AS IS_WRITE
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectName" IS NOT NULL
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
        UNION ALL
        SELECT ah.QUERY_ID,
               f.value:"objectName"::STRING,
               f.value:"objectDomain"::STRING,
               1 AS IS_WRITE
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectName" IS NOT NULL
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
    )
    GROUP BY QUERY_ID, OBJECT_FQN, OBJECT_DOMAIN;

    -- Measured query compute, split EQUALLY across touched objects; the arm
    -- carries the role (V050): QUERY_COMPUTE_WRITE = production share (the
    -- cost of building the object), QUERY_COMPUTE_READ = consumption share.
    -- credits/N is unchanged, so per-query and per-company sums stay additive.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    WITH counts AS (
        SELECT QUERY_ID, COUNT(*) AS N
        FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE
        GROUP BY QUERY_ID
    )
    SELECT qa.DAY, d.OBJECT_FQN, UPPER(REPLACE(d.OBJECT_DOMAIN, ' ', '_')),
           IFF(d.IS_WRITE = 1, 'QUERY_COMPUTE_WRITE', 'QUERY_COMPUTE_READ'),
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(SPLIT_PART(d.OBJECT_FQN, '.', 1)),
           SUM(qa.CREDITS / c.N)
    FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE qa
    JOIN DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE d ON d.QUERY_ID = qa.QUERY_ID
    JOIN counts c ON c.QUERY_ID = qa.QUERY_ID
    GROUP BY 1, 2, 3, 4, 5;

    -- Residual: measured credits with no attributable object. Anti-join the
    -- SAME stage the split used, so the arms partition the credits exactly.
    -- (V050 fix: a query whose only touched object has a NULL name previously
    -- VANISHED — V049's obj_q counted it attributed while the split had no
    -- row for it; it now lands here, where unattributable compute belongs.)
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    SELECT qa.DAY, 'UNATTRIBUTED', 'RESIDUAL', 'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)
    FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE qa
    LEFT JOIN (SELECT DISTINCT QUERY_ID FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE) obj_q
      ON obj_q.QUERY_ID = qa.QUERY_ID
    WHERE obj_q.QUERY_ID IS NULL
    GROUP BY 1;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_OBJECT_COST_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT, t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT) VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- Reload the working window under the staged split: read/write arms land,
-- residual re-derived (incl. the NULL-name credits that used to vanish).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 50 AS VERSION,
       'One-pass object-cost loader (QAH aggregated once, ACCESS_HISTORY flattened once per array, session-scoped stages) + read/write arm split: QUERY_COMPUTE_READ/WRITE label consumption vs production shares, additivity unchanged (Codex Tranche B)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 50);
