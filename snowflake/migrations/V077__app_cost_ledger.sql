-- V077: FACT_APP_COST_DAILY — measured cost by CLIENT APPLICATION x USER. One
-- row per day / application / user / company. Answers "how much did program X (or
-- user Y) cost, and is a program misconfigured and burning credits". The
-- "application" is what the client self-reports in SESSIONS.CLIENT_ENVIRONMENT:
-- APPLICATION (Tableau, dbt, a named tool); when a driver does not report one it
-- falls back to the CLIENT_APPLICATION_ID driver family (JDBC/Python/ODBC/Go),
-- else '(unknown)'. Credits are MEASURED warehouse compute+QAS from
-- QUERY_ATTRIBUTION_HISTORY, joined SESSIONS -> QUERY_HISTORY (SESSION_ID) ->
-- attribution (QUERY_ID). Each query maps to exactly one session/app/user/
-- warehouse, so credits are additive (no split). Excludes idle, serverless,
-- storage, and any query whose session is not joinable in the SESSIONS window
-- (-> '(unknown)'). COMPANY is warehouse-grain (COMPANY_FOR_WAREHOUSE).
-- Apply AFTER V076. Idempotent; safe to re-run. Self-trims to 400 days.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20077, 'V077 requires V076 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 76) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY (
    DAY         DATE          NOT NULL,
    APPLICATION VARCHAR(300)  NOT NULL,
    USER_NAME   VARCHAR(300)  NOT NULL,
    COMPANY     VARCHAR(40),
    QUERIES     NUMBER(18,0),
    CREDITS     NUMBER(24,6),
    LOAD_TS     TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    -- Reload the trailing window and self-trim beyond 400 days (advertised max
    -- window is 365; keep a buffer). No central purge touches this fact.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
     WHERE DAY >= :lo OR DAY < DATEADD('day', -400, CURRENT_DATE());

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY (DAY, APPLICATION, USER_NAME, COMPANY, QUERIES, CREDITS)
    WITH cred AS (
        SELECT QUERY_ID,
               SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= :lo
        GROUP BY QUERY_ID
        HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
    ),
    q AS (
        SELECT QUERY_ID, SESSION_ID, START_TIME::DATE AS DAY,
               COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME, WAREHOUSE_NAME
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= :lo
    ),
    sess AS (
        -- One row per session; widen the lookback so a query's earlier-started
        -- session still resolves. Prefer the self-reported program, else the
        -- driver family (version stripped), else '(unknown)'.
        SELECT SESSION_ID,
               COALESCE(
                   NULLIF(GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')::STRING, ''),
                   NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                   '(unknown)'
               ) AS APPLICATION
        FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
        WHERE CREATED_ON >= DATEADD('day', -7, :lo)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
    ),
    joined AS (
        SELECT q.DAY,
               COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
               q.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) AS COMPANY,
               c.CREDITS
        FROM q
        JOIN cred c ON c.QUERY_ID = q.QUERY_ID
        LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
    )
    SELECT DAY, APPLICATION, USER_NAME, COMPANY, COUNT(*), SUM(CREDITS)
    FROM joined
    GROUP BY DAY, APPLICATION, USER_NAME, COMPANY;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_APP_COST_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT, t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT) VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- First fill: 14 days (the SESSIONS x QUERY_HISTORY x QUERY_ATTRIBUTION join is
-- heavy; the daily task keeps 3d fresh).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(14);

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_APP_COST
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 55 6 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(3);

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_APP_COST RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 77 AS VERSION,
       'FACT_APP_COST_DAILY measured cost by client application x user: SESSIONS (CLIENT_ENVIRONMENT:APPLICATION / driver) x QUERY_HISTORY (SESSION_ID) x QUERY_ATTRIBUTION_HISTORY (QUERY_ID) measured compute+QAS credits, COMPANY_FOR_WAREHOUSE; daily task' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 77);
