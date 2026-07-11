-- V038: the savings ledger books itself (owner ask 2026-07-11: "how can we
-- automate the savings ledger - i don't think anyone will use this. i'm not
-- even using it"). The app's remediation flows book on execute, but changes
-- made directly in Snowsight never touched the ledger. Detection already
-- exists: the daily warehouse-change scan (V024) snapshot-diffs settings,
-- freezes a 14-day baseline, and measures 14 days after. This migration
-- rides it: cost-lever changes (AUTO_SUSPEND down, SIZE down, MAX_CLUSTERS
-- down, SCALING_POLICY -> ECONOMY) auto-book an ESTIMATED item ($0 - no
-- invented numbers) the day they are seen; when the registry verdict lands,
-- the item settles itself VERIFIED (measured credits/day delta x rate x 30,
-- $5/mo noise floor) or REJECTED (no measurable saving). Forward-only:
-- settled items never rewrite. Dedupe via SAVINGS_LEDGER.SOURCE_CHANGE_ID.
-- Apply AFTER V037. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20038, 'V038 requires V037 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 37) THEN
        RAISE not_ready;
    END IF;
END;
$$;

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN SUSPEND;

ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
    ADD COLUMN IF NOT EXISTS SOURCE_CHANGE_ID VARCHAR(80);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rate NUMBER;
BEGIN
    SELECT COALESCE(TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :rate
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, SOURCE_CHANGE_ID)
    SELECT 'Detected ' || r.SETTING || ' change on ' || r.WAREHOUSE_NAME || ': '
               || COALESCE(r.OLD_VALUE, '?') || ' -> ' || COALESCE(r.NEW_VALUE, '?'),
           'ESTIMATED',
           0,
           'SELECT * FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY WHERE CHANGE_ID = ''' || r.CHANGE_ID || '''',
           'Auto-booked from the daily warehouse-change scan; the 14-day measured verdict settles it.',
           r.CHANGE_ID
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
    WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
                      WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID)
      AND (
            (r.SETTING = 'AUTO_SUSPEND'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'MAX_CLUSTERS'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'SCALING_POLICY'
             AND UPPER(COALESCE(r.NEW_VALUE, '')) = 'ECONOMY'
             AND UPPER(COALESCE(r.OLD_VALUE, '')) = 'STANDARD')
         OR (r.SETTING = 'SIZE'
             AND CASE UPPER(REPLACE(COALESCE(r.NEW_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 99 END
               < CASE UPPER(REPLACE(COALESCE(r.OLD_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)
      );

    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = IFF(s.SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),
           VERIFIED_USD = IFF(s.SAVED_MONTHLY_USD >= 5, ROUND(s.SAVED_MONTHLY_USD, 2), NULL),
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured '
                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' -> '
                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))
                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))
                        || 'd (' || s.VERDICT || '); floor $5/mo.', 2000)
      FROM (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                   r.BASELINE_CREDITS_PER_DAY AS BASE,
                   r.AFTER_CREDITS_PER_DAY AS AFT,
                   (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                       * :rate * 30 AS SAVED_MONTHLY_USD
              FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
             WHERE r.VERDICT <> 'PENDING') s
     WHERE l.SOURCE_CHANGE_ID = s.CHANGE_ID
       AND l.STATE = 'ESTIMATED';

    RETURN 'OK';
END;
$$;

-- First pass now: books + settles from the registry's existing 90 days,
-- so the ledger stops being empty the moment V038 applies.
CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LEDGER_AUTOBOOK
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LEDGER_AUTOBOOK RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN RESUME;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 38 AS VERSION, 'SP_LEDGER_AUTOBOOK + TASK_LEDGER_AUTOBOOK: savings ledger auto-books detected cost-lever changes and settles them on the 14d measured verdict' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 38);
