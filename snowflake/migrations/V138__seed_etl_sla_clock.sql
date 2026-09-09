-- V138__seed_etl_sla_clock.sql
--
-- Seed the ETL SLA finish-forecast config (Operations ▸ Pipeline ▸ "SLA finish forecast").
-- The nightly cycle must finish before a clock deadline; it is bracketed by two anchor
-- workflows -- the STARTER that kicks off the cycle (~10pm) and the TERMINAL whose finish is
-- the cycle's completion (~early AM). Four editable keys drive the forecast:
--
--   ETL_CYCLE_START_WORKFLOW  the workflow that starts the nightly cycle
--   ETL_CYCLE_END_WORKFLOW    the workflow that ends it (its finish = cycle completion)
--   ETL_SLA_TARGET_HHMM       the target finish time (07:00)
--   ETL_SLA_BREACH_HHMM       the hard deadline (08:00)
--
-- Seeded to the ALFA PRD cycle bookends (owner-identified 2026-09-09: WF_BASE_GW_CLOSEOUT_CTL_DLY
-- starts, WF_BASE_RECON_MTRC_CMPSIT_DAILY ends). The keys are in DEFAULT_SETTINGS (code defaults),
-- so the panel forecasts from those defaults on redeploy BEFORE this migration is applied; applying
-- it seeds the editable rows so the Admin Settings table is never missing one. WHEN NOT MATCHED
-- only (never overwrites an operator's edited value), mirroring V136/V135/V134/V093.
--
-- Reads only CONTROL_STATUS (already granted for the runtimes/drift panels) -- no new grant.
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in Snowsight
-- after V137. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20138, 'V138 requires V137 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 137) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ETL_CYCLE_START_WORKFLOW', 'WF_BASE_GW_CLOSEOUT_CTL_DLY'),
        ('ETL_CYCLE_END_WORKFLOW', 'WF_BASE_RECON_MTRC_CMPSIT_DAILY'),
        ('ETL_SLA_TARGET_HHMM', '07:00'),
        ('ETL_SLA_BREACH_HHMM', '08:00')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 138 AS VERSION,
       'Seed the ETL SLA finish-forecast config (ETL_CYCLE_START_WORKFLOW / ETL_CYCLE_END_WORKFLOW / ETL_SLA_TARGET_HHMM / ETL_SLA_BREACH_HHMM) to the ALFA PRD nightly-cycle bookends + 07:00 target / 08:00 hard deadline, so Operations Pipeline SLA finish forecast trends cycle completion vs the clock. WHEN NOT MATCHED only. Data-seed only, no schema change. Reads only CONTROL_STATUS (already granted).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 138);
