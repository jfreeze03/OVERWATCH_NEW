-- V135__seed_etl_control_run_params_fqn.sql
--
-- Seed the ETL process-control run-inventory config (Operations ▸ Pipeline ▸ "Run
-- inventory & parameters"). The Informatica cycle registers each run in CONTROL_RUN_ID
-- and records the parameters it ran with in CONTROL_PARAMS; this panel reads both so
-- the operator can see which runs happened, when, and what knobs they used (RUN_DATE,
-- thresholds, load indicators) — none of which Snowflake's own task history captures:
--
--   ETL_CONTROL_RUN_ID_FQN  the CONTROL_RUN_ID registry table FQN.
--   ETL_CONTROL_PARAMS_FQN  the CONTROL_PARAMS parameters table FQN.
--
-- Seeded to the PRD database (owner ask 2026-09-09: focus on ALFA_EDW_PRD). Both keys
-- are in DEFAULT_SETTINGS ('' code default) and auto-appear in the Admin editor, so
-- re-pointing them elsewhere is an Admin edit, not a code change. WHEN NOT MATCHED only
-- (never overwrites an operator's edited value), mirroring V134/V128/V093/V121.
--
-- GRANTS: the app role needs SELECT on both control tables for the live panel to read
-- them (GRANT SELECT ON TABLE ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID / CONTROL_PARAMS TO
-- ROLE <app role>;). Until granted, the panel fails closed with a grant hint — no error.
-- Grants are outside DBA_MAINT_DB and are applied separately (not in this migration).
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in
-- Snowsight after V134. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20135, 'V135 requires V134 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 134) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ETL_CONTROL_RUN_ID_FQN', 'ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID'),
        ('ETL_CONTROL_PARAMS_FQN', 'ALFA_EDW_PRD.PUBLIC.CONTROL_PARAMS')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 135 AS VERSION,
       'Seed the ETL run-inventory config (ETL_CONTROL_RUN_ID_FQN + ETL_CONTROL_PARAMS_FQN) to ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID / CONTROL_PARAMS, so Operations Pipeline Run inventory and parameters reads the Informatica run registry + parameters that Snowflake task history cannot see. WHEN NOT MATCHED only. Data-seed only, no schema change. App role needs SELECT on both control tables (granted separately) for the live read.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 135);
