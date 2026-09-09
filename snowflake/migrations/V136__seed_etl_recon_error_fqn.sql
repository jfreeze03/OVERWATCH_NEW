-- V136__seed_etl_recon_error_fqn.sql
--
-- Seed the ETL process-control Phase 3 config (Operations ▸ Pipeline ▸ "Reconciliation
-- errors"). The nightly cycle reconciles each metric's SOURCE_LAYER against its
-- TARGET_LAYER and logs a RECON_MTRC_ERROR row when they don't tie out (over the recon
-- threshold). This panel surfaces recent reconciliation errors so a source-vs-target
-- mismatch is caught before the numbers are trusted downstream:
--
--   ETL_RECON_ERROR_FQN  the RECON_MTRC_ERROR table FQN.
--
-- Seeded to ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR (owner ask 2026-09-09: focus on
-- the PRD database). NOTE the recon table lives in DB_T_PROD_CORE, NOT the PUBLIC schema
-- the CONTROL_* tables use — so it needs its own SELECT grant. The key is in
-- DEFAULT_SETTINGS ('' code default) and auto-appears in the Admin editor. WHEN NOT
-- MATCHED only (never overwrites an operator's edited value), mirroring V135/V134/V093.
--
-- GRANTS: the app role needs SELECT on the recon table for the live panel to read it
-- (GRANT SELECT ON TABLE ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR TO ROLE <app role>;,
-- plus USAGE on the DB_T_PROD_CORE schema). Until granted, the panel fails closed with a
-- grant hint — no error. Grants are outside DBA_MAINT_DB and applied separately.
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in
-- Snowsight after V135. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20136, 'V136 requires V135 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 135) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ETL_RECON_ERROR_FQN', 'ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 136 AS VERSION,
       'Seed the ETL process-control Phase 3 config (ETL_RECON_ERROR_FQN) to ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR, so Operations Pipeline Reconciliation errors surfaces recent source-vs-target layer mismatches the nightly recon logged. WHEN NOT MATCHED only. Data-seed only, no schema change. App role needs SELECT on the recon table (granted separately, DB_T_PROD_CORE schema) for the live read.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 136);
