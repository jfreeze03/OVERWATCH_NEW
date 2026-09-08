-- V128__seed_etl_ref_gap_config.sql
--
-- Seed the ETL reference-data gap monitor config (Operations ▸ Pipeline ▸ "Reference-data
-- gaps"). The nightly ETL load hard-fails when a source system emits a code that has no
-- translation row in the XLAT reference table, so the operator runs a manual MINUS every
-- morning to catch new codes before the cycle breaks. The app now watches that live,
-- generalized across the whole XLAT code family and driven from these two SETTINGS keys:
--
--   ETL_REF_GAP_XLAT    the translation table FQN (reference side of the MINUS).
--   ETL_REF_GAP_CHECKS  one check per entry, "[*]<name> | <staging_fqn> | <staging_code_col>"
--                       (newline- or ';'-separated). A leading '*' PINS a check so it always
--                       shows past the scope-bar Database filter. The name is the
--                       SRC_IDNTFTN_NM value; it drives both the label and the XLAT filter.
--
-- Seeded with the pinned pc_uwissuetype.code check (the every-morning check) so the panel is
-- live on apply; add the rest of the XLAT family on Admin ▸ SETTINGS (they honor the Database
-- filter). Both keys are in DEFAULT_SETTINGS ('' code default) and auto-appear in the Admin
-- editor. WHEN NOT MATCHED only (never overwrites an operator's edited value), mirroring V093/V121.
--
-- GRANTS: the app role needs SELECT on the staging + XLAT tables for the live panel to read
-- them (e.g. GRANT SELECT ON ALL TABLES IN SCHEMA ALFA_EDW_PRD.DB_T_PROD_STAG TO ROLE <app role>;
-- GRANT SELECT ON ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT TO ROLE <app role>;). Until
-- granted, the panel fails closed with a grant hint — no error. Grants are outside DBA_MAINT_DB
-- and are applied separately (not in this migration, so a missing privilege can't block the seed).
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in Snowsight
-- after V127. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20128, 'V128 requires V127 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 127) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ETL_REF_GAP_XLAT', 'ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT'),
        ('ETL_REF_GAP_CHECKS', '*pc_uwissuetype.code | ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE | CODE_STG')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 128 AS VERSION,
       'Seed the ETL reference-data gap monitor config (ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS) with the pinned pc_uwissuetype.code check, so Operations Pipeline Reference-data gaps is live on apply. Config-driven across the whole XLAT code family; other checks honor the scope-bar Database filter, pinned checks always show. WHEN NOT MATCHED only. Data-seed only, no schema change. App role needs SELECT on the staging + XLAT tables (granted separately) for the live read.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 128);
