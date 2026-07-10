-- V033__change_attribution.sql — who made each warehouse change.
--
-- Pre-Terraform half of the managed-vs-manual story (design:
-- docs/design/V029_INCIDENT_OBJECT.md, IaC assumptions). The registry knows
-- WHAT changed (snapshot diff, V024); this adds WHO: a best-effort join to
-- the QUERY_HISTORY ALTER that landed just before the snapshot saw the
-- change. Multi-match picks the latest — evidence, not lineage (the V010
-- rule). MANAGED vs MANUAL derives at READ time from the DEPLOY_ACTORS
-- setting (comma list of service users — Flyway/Terraform once they land;
-- empty today, so everything honestly reads MANUAL or UNKNOWN).
--
-- OPS_UNMANAGED_CHANGE deliberately does NOT ship here: an alert rule with
-- no populated DEPLOY_ACTORS would be pure noise, and decorative config is
-- against house rules (review #8). It arrives with its scan arm the day
-- DEPLOY_ACTORS is first populated.
--
-- Idempotent. Apply IN ORDER after V032. No new grants needed.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20033, 'BLOCKED: SCHEMA_VERSION < 32 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 32) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

ALTER TABLE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
    ADD COLUMN IF NOT EXISTS CHANGED_BY VARCHAR(200);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (SELECT 'DEPLOY_ACTORS' AS KEY, '' AS VALUE) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    -- Attribute unattributed registry rows from the last 7 days: the ALTER
    -- that ran within the 65 minutes before the hourly snapshot saw the
    -- change (5-minute forward grace for clock skew). Best effort.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET CHANGED_BY = s.USER_NAME
      FROM (
          SELECT r.CHANGE_ID, MAX_BY(q.USER_NAME, q.START_TIME) AS USER_NAME
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -8, CURRENT_TIMESTAMP())
           AND q.START_TIME BETWEEN DATEADD('minute', -65, r.CHANGE_SEEN_AT)
                                AND DATEADD('minute', 5, r.CHANGE_SEEN_AT)
           AND q.EXECUTION_STATUS = 'SUCCESS'
           AND q.QUERY_TYPE ILIKE 'ALTER%'
           AND q.QUERY_TEXT ILIKE '%' || r.WAREHOUSE_NAME || '%'
          WHERE r.CHANGED_BY IS NULL
            AND r.CHANGE_SEEN_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    RETURN 'attribution pass complete';
END;
$$;

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CHANGE_ATTRIBUTION
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CHANGE_ATTRIBUTION RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_INCIDENT_AUTODECLARE RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;

-- First pass now so existing registry rows get their who.
CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION();

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 33 AS VERSION,
       'change attribution: CHANGED_BY on the registry + DEPLOY_ACTORS (managed vs manual at read time)' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
