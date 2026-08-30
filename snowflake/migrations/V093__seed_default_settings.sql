-- V093__seed_default_settings.sql
--
-- Seed the 17 Admin-editable DEFAULT_SETTINGS keys that no prior migration seeded:
-- the 9 platform-score weights (SCORE_PTS_*), the 5 governance-drift weights
-- (GOV_PTS_*), FORECAST_ENGINE, EXPECTED_SPIKE_CALENDAR and DATA_TRANSFER_USD_PER_TB,
-- each with its app/config.py DEFAULT_SETTINGS value. The Admin > Settings writer
-- already UPSERTs (v4.343.0), so editing an unseeded key persists; this migration
-- puts each key's code-default row in place so it also appears in the Settings table
-- view and the seed set stops drifting from DEFAULT_SETTINGS. WHEN NOT MATCHED only
-- (never overwrites an operator's edited VALUE), mirroring V001's SETTINGS seed.
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in
-- Snowsight after V092. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20093, 'V093 requires V092 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 92) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('SCORE_PTS_BUDGET_PER_PCT',    '0.5'),
        ('SCORE_PTS_PER_CRITICAL',      '6'),
        ('SCORE_PTS_PER_HIGH',          '2'),
        ('SCORE_PTS_QUERY_FAIL_PER_PCT','1.5'),
        ('SCORE_PTS_TASK_FAIL_PER_PCT', '2'),
        ('SCORE_PTS_QUEUE_PER_MIN',     '0.3'),
        ('SCORE_PTS_SPILL_PER_GB',      '0.5'),
        ('SCORE_PTS_PER_STALE_SOURCE',  '4'),
        ('SCORE_PTS_PER_OPEN_ACTION',   '1.5'),
        ('GOV_PTS_MFA_GAP',             '5'),
        ('GOV_PTS_EXPIRED_CRED',        '8'),
        ('GOV_PTS_EXPIRING_CRED',       '2'),
        ('GOV_PTS_BREAKGLASS_GRANT',    '6'),
        ('GOV_PTS_NO_AUTOSUSPEND',      '3'),
        ('FORECAST_ENGINE',             'linear'),
        ('EXPECTED_SPIKE_CALENDAR',     'MONTH_END:1;QUARTER_END:2'),
        ('DATA_TRANSFER_USD_PER_TB',    '20.0')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 93 AS VERSION,
       'Seed the 17 Admin-editable DEFAULT_SETTINGS keys no prior migration seeded (9 SCORE_PTS_* platform-score weights, 5 GOV_PTS_* governance-drift weights, FORECAST_ENGINE, EXPECTED_SPIKE_CALENDAR, DATA_TRANSFER_USD_PER_TB) with their code-default values, so they appear in the Admin Settings table and the seed stops drifting from DEFAULT_SETTINGS. WHEN NOT MATCHED only (never overwrites an edited value). Data-seed only, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 93);
