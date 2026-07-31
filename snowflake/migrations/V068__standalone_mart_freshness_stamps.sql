-- V068__standalone_mart_freshness_stamps.sql
--
-- Live finding (Brief card screenshot 2026-07-31): MART_LOCK_WAIT_DAILY read 448.5h stale.
-- The loader chain is fine - the freshness STAMP was orphaned: V041 retired the 10-minute
-- SP_SNAPSHOT_FRESHNESS sweep and moved stamping into each loader, but the two marts with
-- their OWN standalone tasks (lock-wait V035, pattern-cost V036/37/47) never got one, so
-- their SOURCE_FRESHNESS_STATE rows froze at apply time. Both loaders are re-derived from
-- their latest defs (V035 / V047) via outputs/gen_v068.py with the standard V041-R6
-- freshness MERGE appended - stamping LAST_LOAD_TS = CURRENT_TIMESTAMP() (a RUN stamp) so
-- an account with zero lock waits reads FRESH, not stale (no-news is not no-load).
-- The tail CALLs both procs once so the rows heal immediately.
-- Idempotent; apply AFTER V067. No new objects.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20068, 'V068 requires V067 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 67) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_LOCK_WAIT_MART  (freshness run-stamp appended)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_LOCK_WAIT_MART(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Failures surface in TASK_HISTORY and the task retries next day — no
-- swallow, no partial state (MERGE is idempotent on the day grain).
BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY t
    USING (
        SELECT g.DAY, g.DATABASE_NAME, g.SCHEMA_NAME, g.OBJECT_NAME, g.LOCK_TYPE,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
               g.WAIT_EVENTS, g.ACQUIRED_WAIT_SEC, g.NEVER_ACQUIRED, g.LAST_SEEN
        FROM (
            SELECT CAST(REQUESTED_AT AS DATE) AS DAY,
                   COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                   COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                   COALESCE(OBJECT_NAME, 'NONE') AS OBJECT_NAME,
                   COALESCE(LOCK_TYPE, 'NONE') AS LOCK_TYPE,
                   COUNT(*) AS WAIT_EVENTS,
                   SUM(IFF(ACQUIRED_AT IS NOT NULL,
                           DATEDIFF('second', REQUESTED_AT, ACQUIRED_AT), 0)) AS ACQUIRED_WAIT_SEC,
                   COUNT_IF(ACQUIRED_AT IS NULL) AS NEVER_ACQUIRED,
                   MAX(REQUESTED_AT) AS LAST_SEEN
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY
            WHERE REQUESTED_AT >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
            GROUP BY 1, 2, 3, 4, 5
        ) g
    ) s
    ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME
       AND t.SCHEMA_NAME = s.SCHEMA_NAME AND t.OBJECT_NAME = s.OBJECT_NAME
       AND t.LOCK_TYPE = s.LOCK_TYPE
    WHEN MATCHED THEN UPDATE SET
        t.COMPANY = s.COMPANY, t.WAIT_EVENTS = s.WAIT_EVENTS,
        t.ACQUIRED_WAIT_SEC = s.ACQUIRED_WAIT_SEC, t.NEVER_ACQUIRED = s.NEVER_ACQUIRED,
        t.LAST_SEEN = s.LAST_SEEN, t.LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, LOCK_TYPE, COMPANY,
         WAIT_EVENTS, ACQUIRED_WAIT_SEC, NEVER_ACQUIRED, LAST_SEEN)
    VALUES (s.DAY, s.DATABASE_NAME, s.SCHEMA_NAME, s.OBJECT_NAME, s.LOCK_TYPE, s.COMPANY,
            s.WAIT_EVENTS, s.ACQUIRED_WAIT_SEC, s.NEVER_ACQUIRED, s.LAST_SEEN);

    -- V068: loader-owned freshness stamp (V041-R6 pattern; this standalone-task loader
    -- was missed in the V041 handoff, freezing its SOURCE_FRESHNESS_STATE row at apply
    -- time). LAST_LOAD_TS is a RUN stamp (CURRENT_TIMESTAMP()), not MAX(LOAD_TS) of the
    -- mart, so a window with ZERO source events still reads fresh - no news is not
    -- no load.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_LOCK_WAIT_DAILY' AS SOURCE_NAME, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS,
               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY) AS ROW_COUNT
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

-- >>> derived:SP_LOAD_PATTERN_COST  (freshness run-stamp appended)
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
                       SUM(a.CREDITS_ATTRIBUTED_COMPUTE + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS_ATTRIBUTED,
                       HLL_ACCUMULATE(q.USER_NAME) AS USERS_HLL
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
                JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                  ON q.QUERY_ID = a.QUERY_ID
                 AND q.START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
                WHERE a.START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
                  AND q.QUERY_PARAMETERIZED_HASH IS NOT NULL
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

-- Heal the two frozen rows now (cheap 3-day windows - the same cadence their tasks use);
-- the next scheduled task fires keep them current from here on.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_LOCK_WAIT_MART(3);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 68 AS VERSION,
       'Standalone-mart freshness stamps (screenshot finding): SP_LOAD_LOCK_WAIT_MART + SP_LOAD_PATTERN_COST gain the V041-R6 loader-owned SOURCE_FRESHNESS_STATE stamp they were never given when V041 retired the snapshot sweep - their rows had been frozen since apply time (the Brief card showed MART_LOCK_WAIT_DAILY 448h stale). Stamped as a RUN timestamp so zero-event windows read fresh; migration tail heals both rows immediately. Re-derived from V035/V047; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 68);
