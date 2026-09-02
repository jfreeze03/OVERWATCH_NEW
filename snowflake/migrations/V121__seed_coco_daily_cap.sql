-- V121__seed_coco_daily_cap.sql
--
-- Seed COCO_DAILY_CAP_CREDITS, the per-user daily Cortex Code allowance the AI-chargeback
-- token-economics efficiency review measures against (round-8 hunt SC-1). The panel reads
-- settings.get("COCO_DAILY_CAP_CREDITS") and the CHANGELOG advertised it as configurable,
-- but the key was never in DEFAULT_SETTINGS, never seeded, and not offered in the Admin
-- editor — so it was pinned to the 15.0 code fallback with no way to change it (an org whose
-- CoCo cap is 30/day got the review measured against the wrong allowance). It is now in
-- DEFAULT_SETTINGS + _SETTING_EDITORS; this migration puts its code-default row in place so
-- it appears in the Admin Settings table (the writer already UPSERTs, so editing persists).
-- WHEN NOT MATCHED only (never overwrites an operator's edited value), mirroring V093.
--
-- Data-seed only: no schema change, no proc/view/task, no reload. Owner applies in Snowsight
-- after V120. The app never runs this migration.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20121, 'V121 requires V120 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 120) THEN
        RAISE not_ready;
    END IF;
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('COCO_DAILY_CAP_CREDITS', '15.0')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 121 AS VERSION,
       'Seed COCO_DAILY_CAP_CREDITS (per-user daily Cortex Code allowance for the token-economics efficiency review) with its code default 15.0, so the advertised-configurable knob is finally in the Admin Settings table and editable (it was read but never in DEFAULT_SETTINGS/seeded/editable, pinned to the 15.0 fallback). WHEN NOT MATCHED only. Data-seed only, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 121);
