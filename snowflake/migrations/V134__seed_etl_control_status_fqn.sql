-- V134__seed_etl_control_status_fqn.sql
--
-- Seed the ETL process-control Phase 2 config (Operations ▸ Pipeline ▸ "Workflow
-- runtimes — latest ETL run"). Alfa's nightly cycle is Informatica-orchestrated
-- stored-proc CALLs, which are INVISIBLE to Snowflake's ACCOUNT_USAGE.TASK_HISTORY
-- (that only records native Snowflake TASKs). The CONTROL_STATUS table is the only
-- record of each task's runtime + status per run, so the panel reads it directly:
--
--   ETL_CONTROL_STATUS_FQN  the CONTROL_STATUS table FQN (per-task start/end/status).
--
-- Seeded to ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS (owner ask 2026-09-09: focus on the
-- PRD database). The key is in DEFAULT_SETTINGS ('' code default) and auto-appears in the Admin editor,
-- so pointing it elsewhere (a different DB/schema) is an Admin edit, not a code
-- change. WHEN NOT MATCHED only (never overwrites an operator's edited value),
-- mirroring V128/V093/V121.
--
-- GRANTS: the app role needs SELECT on the control table for the live panel to read
-- it (e.g. GRANT SELECT ON ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS TO ROLE <app role>;).
-- Until granted, the panel fails closed with a grant hint — no error. The grant is
-- outside DBA_MAINT_DB and is applied separately (not in this migration, so a missing
-- privilege can't block the seed).
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in
-- Snowsight after V133. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20134, 'V134 requires V133 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 133) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ETL_CONTROL_STATUS_FQN', 'ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 134 AS VERSION,
       'Seed the ETL process-control Phase 2 config (ETL_CONTROL_STATUS_FQN) to ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS, so Operations Pipeline Workflow runtimes reads the Informatica CONTROL_STATUS table (per-task start/end/status per run) that Snowflake TASK_HISTORY cannot see. WHEN NOT MATCHED only. Data-seed only, no schema change. App role needs SELECT on the control table (granted separately) for the live read.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 134);
