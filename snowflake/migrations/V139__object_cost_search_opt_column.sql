-- V139__object_cost_search_opt_column.sql
--
-- Fix: FACT_OBJECT_COST_DAILY has been STALE since 2026-09-09 (frozen at a full 916k-row
-- fill while every other fact loads daily). Root cause: Snowflake renamed the object column
-- on SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY from TABLE_NAME -> BASE_TABLE_NAME
-- (the view now carries INDEX_NAME/INDEX_ID/BASE_TABLE_ID/BASE_TABLE_NAME/INDEX_TYPE). Because
-- a Snowflake Scripting proc compiles its inner statements at RUN time, SP_LOAD_OBJECT_COST's
-- search-optimization arm started throwing
--     SQL compilation error: ... invalid identifier 'TABLE_NAME'
-- every night. The load is one atomic DELETE+INSERT (V062/V067), so the failure ROLLS BACK
-- and the last good (2026-09-09) fill is retained -- which is why the panel shows a stale
-- LOAD_TS with a full row count and no task ever paged (the proc catches its own error and
-- returns a string, so the task reports SUCCEEDED). Confirmed from APP_ERROR_LOG
-- (PAGE='ObjectCost', ERROR_TYPE='object_cost_load_failed') on 2026-09-14. NOT a timeout
-- (account 21600s / WH 1800s / task 3600000ms all healthy) and NOT an OVERWATCH change.
--
-- This re-derives SP_LOAD_OBJECT_COST from V067, changing ONLY the search-opt arm's object-name
-- column to BASE_TABLE_NAME. The clustering + MV arms (still TABLE_NAME -- verified present),
-- the serverless-task arm (TASK_NAME) and the snowpipe arm (PIPE_NAME) are byte-identical to
-- V067; verified against the live view columns on 2026-09-14. The final CALL backfills 14 days
-- so the 2026-09-10..present gap heals the moment this is applied. Owner applies in Snowsight
-- after V138; the daily TASK_LOAD_OBJECT_COST (06:45 CT) then runs clean. Never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20139, 'V139 requires V138 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 138) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
    emsg VARCHAR;
    failed BOOLEAN DEFAULT FALSE;   -- V067 #10: a rolled-back load must return non-OK
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());

    -- One-pass staging (V050): QUERY_ATTRIBUTION_HISTORY is aggregated ONCE
    -- and ACCESS_HISTORY flattened once per array (V049 re-scanned QAH per
    -- insert and flattened AH four times); both attribution inserts below
    -- read the session-scoped stages. Same staged-extract pattern as V041's
    -- OW_QH_EXTRACT.
    -- B34 (V062): the two CREATE ... TEMPORARY TABLE stages are built HERE,
    -- ABOVE the transaction, on purpose. Snowflake auto-commits DDL, so a
    -- stage build inside the BEGIN TRANSACTION below would implicitly COMMIT
    -- the DELETE and defeat the atomic wrap. The stages read only
    -- ACCOUNT_USAGE, so hoisting them is order-safe.
    CREATE OR REPLACE TEMPORARY TABLE DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE AS
    SELECT a.QUERY_ID, MIN(a.START_TIME)::DATE AS DAY,
           -- V067 #17: carry the executing warehouse so the residual (unattributed-to-object)
           -- row can resolve its COMPANY from the warehouse instead of being forced to
           -- 'UNKNOWN'. QUERY_ATTRIBUTION_HISTORY has no WAREHOUSE_NAME, so LEFT JOIN
           -- QUERY_HISTORY on QUERY_ID (the V036 pattern); LEFT + MAX keeps one row per
           -- QUERY_ID and never drops a query whose history row is missing.
           MAX(q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
           SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
    LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
      ON q.QUERY_ID = a.QUERY_ID
     AND q.START_TIME >= :lo
    WHERE a.START_TIME >= :lo
    GROUP BY a.QUERY_ID
    HAVING SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) > 0;

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

    -- B34 (V062): the DELETE + all seven INSERTs are ONE atomic transaction —
    -- a crash between the wipe and the refills can no longer leave
    -- FACT_OBJECT_COST_DAILY half-empty; a failed INSERT rolls the DELETE back
    -- and readers keep the previous fill.
    BEGIN
    BEGIN TRANSACTION;
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
    SELECT START_TIME::DATE, COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(BASE_TABLE_NAME, 'UNKNOWN'),
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
    -- V067 #17: resolve the residual COMPANY from the executing WAREHOUSE_NAME
    -- (carried through OW_OBJCOST_QA_STAGE) via COMPANY_FOR_WAREHOUSE, so a
    -- company-filtered object-cost total reconciles when the warehouse identifies the
    -- company; COMPANY_FOR_WAREHOUSE returns 'UNKNOWN' only when the warehouse does not
    -- resolve. The company key joins the GROUP BY, so residual credits split by company
    -- while the per-day total stays additive.
    -- TODO(#18, deferred): storing BOTH the consumer-company (warehouse) and the
    -- object-owner-company is a separate dual-lens enhancement, not done here.
    SELECT qa.DAY, 'UNATTRIBUTED', 'RESIDUAL', 'QUERY_COMPUTE_RESIDUAL',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qa.WAREHOUSE_NAME), SUM(qa.CREDITS)
    FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE qa
    LEFT JOIN (SELECT DISTINCT QUERY_ID FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE) obj_q
      ON obj_q.QUERY_ID = qa.QUERY_ID
    WHERE obj_q.QUERY_ID IS NULL
    GROUP BY 1, 5;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            failed := TRUE;   -- V067 #10
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ObjectCost', 'object_cost_load_failed', :emsg, 'FACT_OBJECT_COST_DAILY - previous fill retained on rollback', CURRENT_ROLE();
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_OBJECT_COST_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT, t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT) VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    IF (failed) THEN
        RETURN 'FAILED: object-cost load rolled back - see APP_ERROR_LOG';
    END IF;
    RETURN 'OK';
END;
$$;

-- Heal the gap on apply: the search-opt arm has failed (rolling the whole load back) every
-- night since 2026-09-10, so FACT_OBJECT_COST_DAILY is frozen at its 2026-09-09 fill. Now that
-- the proc compiles, reload 14 days (matches the V048 first-fill window) to fill 2026-09-10..today.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 139 AS VERSION,
       'Object-cost loader search-opt column fix: Snowflake renamed SEARCH_OPTIMIZATION_HISTORY.TABLE_NAME -> BASE_TABLE_NAME, so SP_LOAD_OBJECT_COST failed nightly with invalid identifier TABLE_NAME and rolled back (FACT_OBJECT_COST_DAILY frozen at 2026-09-09). Re-derived from V067 changing ONLY the search-opt arm object-name column to BASE_TABLE_NAME; the clustering/MV (TABLE_NAME), serverless-task (TASK_NAME) and snowpipe (PIPE_NAME) arms are byte-identical. Backfills 14d on apply.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 139);
