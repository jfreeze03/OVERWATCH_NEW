-- V049__write_target_attribution.sql — writes join the object-cost split
-- (r28+ queue; owner go 2026-07-15: "let's do both").
--
--   V048's split used ACCESS_HISTORY.BASE_OBJECTS_ACCESSED (reads only), so
--   write-only ETL — COPY INTO, INSERT ... VALUES, CTAS from constants —
--   read no base table and its credits landed in QUERY_COMPUTE_RESIDUAL
--   instead of on the tables it builds. V049 folds
--   ACCESS_HISTORY.OBJECTS_MODIFIED (write targets) into the same equal
--   split: loads attribute to their targets; the residual shrinks to
--   genuinely unattributable compute (no read, no write). DISTINCT over the
--   union keeps a read+write of one table to a single share. Credits stay
--   additive across arms and companies.
--
--   Proc swap + 14-day reload; no new objects (table and task are V048's).
--   The reload window matches the V048 first fill, so the working window is
--   re-attributed under the new split in one pass.
--
-- Derivation law: SP_LOAD_OBJECT_COST from V048 verbatim + two enumerated
-- edits (dedup CTE + obj_q CTE — split and residual must agree on what
-- "attributed" means); tests/test_v049_write_targets.py re-derives and
-- byte-compares. Apply AFTER V048. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20049, 'V049 requires V048 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 48) THEN
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

    -- Measured query compute, split EQUALLY across accessed base objects ---
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    WITH qa AS (
        SELECT QUERY_ID, MIN(START_TIME)::DATE AS DAY,
               SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= :lo
        GROUP BY QUERY_ID
        HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
    ),
    dedup AS (
        -- V049: write targets join the split — OBJECTS_MODIFIED alongside
        -- BASE_OBJECTS_ACCESSED, so write-only ETL (COPY INTO, INSERT..VALUES,
        -- CTAS from constants) attributes to the tables it builds. DISTINCT
        -- over the union keeps a read+write of one table to a single share.
        SELECT DISTINCT QUERY_ID, OBJECT_FQN, OBJECT_DOMAIN
        FROM (
            SELECT ah.QUERY_ID,
                   f.value:"objectName"::STRING AS OBJECT_FQN,
                   f.value:"objectDomain"::STRING AS OBJECT_DOMAIN
            FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
                 LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
            WHERE ah.QUERY_START_TIME >= :lo
              AND f.value:"objectName" IS NOT NULL
              AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
            UNION ALL
            SELECT ah.QUERY_ID,
                   f.value:"objectName"::STRING,
                   f.value:"objectDomain"::STRING
            FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
                 LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED) f
            WHERE ah.QUERY_START_TIME >= :lo
              AND f.value:"objectName" IS NOT NULL
              AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
        )
    ),
    counts AS (SELECT QUERY_ID, COUNT(*) AS N FROM dedup GROUP BY QUERY_ID)
    SELECT qa.DAY, d.OBJECT_FQN, UPPER(REPLACE(d.OBJECT_DOMAIN, ' ', '_')), 'QUERY_COMPUTE',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(SPLIT_PART(d.OBJECT_FQN, '.', 1)),
           SUM(qa.CREDITS / c.N)
    FROM qa
    JOIN dedup d ON d.QUERY_ID = qa.QUERY_ID
    JOIN counts c ON c.QUERY_ID = qa.QUERY_ID
    GROUP BY 1, 2, 3, 4, 5;

    -- Residual: measured credits for queries that touched no base object ---
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY (DAY, OBJECT_FQN, OBJECT_DOMAIN, COST_ARM, COMPANY, CREDITS)
    WITH qa AS (
        SELECT QUERY_ID, MIN(START_TIME)::DATE AS DAY,
               SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= :lo
        GROUP BY QUERY_ID
        HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
    ),
    obj_q AS (
        -- V049: attributed = read OR wrote a base object; the residual is
        -- only what genuinely touched nothing.
        SELECT ah.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
        UNION
        SELECT ah.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
    )
    SELECT qa.DAY, 'UNATTRIBUTED', 'RESIDUAL', 'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)
    FROM qa
    LEFT JOIN obj_q ON obj_q.QUERY_ID = qa.QUERY_ID
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

-- Reload the working window under the new split: write targets attributed,
-- residual re-derived. Same 14-day horizon as the V048 first fill.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 49 AS VERSION,
       'Write-target attribution: ACCESS_HISTORY.OBJECTS_MODIFIED joins the object-cost equal split, so write-only ETL attributes to its target tables; QUERY_COMPUTE_RESIDUAL shrinks to no-read-no-write compute (r28+ queue)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 49);
