-- V118: SP_LEDGER_AUTOBOOK stops double-counting a warehouse's saving when more
-- than one cost lever changes on it in the same measured window (LBA-1, round-4
-- hunt). Root cause: BASELINE_CREDITS_PER_DAY / AFTER_CREDITS_PER_DAY in
-- WAREHOUSE_CHANGE_REGISTRY are WAREHOUSE-level measures, but the registry holds
-- one row per (warehouse, SETTING) change. When (say) AUTO_SUSPEND *and* SIZE both
-- drop on one warehouse in the same window, BOTH rows carry the same warehouse
-- credit delta, so V038's settle booked the full measured saving on EACH row —
-- the ledger total counted one physical saving N times.
--
-- Fix: the measured warehouse delta is attributed to exactly ONE primary change per
-- (warehouse, measured-window signature); co-occurring levers still settle VERIFIED
-- (the change was made and the warehouse did improve) but with VERIFIED_USD = 0 and
-- a co-attribution note, so SUM(VERIFIED_USD) books the warehouse saving once.
-- The measured-window signature is (WAREHOUSE_NAME, BASELINE_CREDITS_PER_DAY,
-- AFTER_CREDITS_PER_DAY, AFTER_DAYS): rows sharing it are, by construction, the same
-- physical measurement (identical SAVED_MONTHLY_USD). Rows that measured genuinely
-- different windows differ in the signature and each keep their own dollars.
--
-- Forward-only settle preserved (only STATE='ESTIMATED' rows move). A one-time,
-- idempotent corrective pass (Step 2) also de-duplicates already-settled VERIFIED
-- rows that V038 double-booked, so the ledger total is correct the moment V118
-- applies. Apply AFTER V117. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20118, 'V118 requires V117 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 117) THEN
        RAISE not_ready;
    END IF;
END;
$$;

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

    -- Book detected cost-lever changes as ESTIMATED $0 (unchanged from V038).
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

    -- Settle forward-only. LBA-1: rank co-occurring levers within one measured
    -- window; the primary (RN=1) carries the full warehouse saving, the rest settle
    -- VERIFIED at $0 so the physical saving is booked exactly once.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),
           VERIFIED_USD = CASE
                              WHEN s.WH_SAVED_MONTHLY_USD < 5 THEN NULL
                              WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)
                              ELSE 0
                          END,
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured '
                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' -> '
                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))
                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))
                        || 'd (' || s.VERDICT || '); floor $5/mo.'
                        || IFF(s.WH_SAVED_MONTHLY_USD >= 5 AND s.RN > 1,
                               ' | LBA-1 co-attributed: warehouse saving booked once on change '
                               || s.PRIMARY_CHANGE_ID || '.', ''), 2000)
      FROM (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                   r.BASELINE_CREDITS_PER_DAY AS BASE,
                   r.AFTER_CREDITS_PER_DAY AS AFT,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
                   FIRST_VALUE(r.CHANGE_ID) OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS PRIMARY_CHANGE_ID,
                   (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                       * :rate * 30 AS WH_SAVED_MONTHLY_USD
              FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
             WHERE r.VERDICT <> 'PENDING') s
     WHERE l.SOURCE_CHANGE_ID = s.CHANGE_ID
       AND l.STATE = 'ESTIMATED';

    RETURN 'OK';
END;
$$;

-- Step 2 (one-time, idempotent): correct rows V038 already double-booked. Zero the
-- VERIFIED_USD on non-primary VERIFIED co-occurring levers (same measured-window
-- signature as a VERIFIED primary), so the historical ledger total is right too.
-- Guarded by the ' | LBA-1 co-attributed' sentinel so re-running is a no-op, and
-- only touches rows whose group primary is itself VERIFIED (a real saving).
EXECUTE IMMEDIATE
$$
BEGIN
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET VERIFIED_USD = 0,
           NOTES = LEFT(COALESCE(l.NOTES, '')
                        || ' | LBA-1 co-attributed: warehouse saving booked once on change '
                        || g.PRIMARY_CHANGE_ID || '.', 2000)
      FROM (
            SELECT reg.CHANGE_ID,
                   ROW_NUMBER() OVER (
                       PARTITION BY reg.WAREHOUSE_NAME, reg.BASELINE_CREDITS_PER_DAY,
                                    reg.AFTER_CREDITS_PER_DAY, reg.AFTER_DAYS
                       ORDER BY reg.CHANGE_SEEN_AT, reg.CHANGE_ID) AS RN,
                   FIRST_VALUE(reg.CHANGE_ID) OVER (
                       PARTITION BY reg.WAREHOUSE_NAME, reg.BASELINE_CREDITS_PER_DAY,
                                    reg.AFTER_CREDITS_PER_DAY, reg.AFTER_DAYS
                       ORDER BY reg.CHANGE_SEEN_AT, reg.CHANGE_ID) AS PRIMARY_CHANGE_ID
              FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY reg
             WHERE reg.VERDICT <> 'PENDING') g
     WHERE l.SOURCE_CHANGE_ID = g.CHANGE_ID
       AND g.RN > 1
       AND l.STATE = 'VERIFIED'
       AND COALESCE(l.VERIFIED_USD, 0) > 0
       AND COALESCE(l.NOTES, '') NOT LIKE '%LBA-1 co-attributed%'
       AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER p
                    WHERE p.SOURCE_CHANGE_ID = g.PRIMARY_CHANGE_ID
                      AND p.STATE = 'VERIFIED');
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 118 AS VERSION, 'SP_LEDGER_AUTOBOOK dedup (LBA-1): a warehouse credit delta is booked once even when multiple cost levers change together - the primary lever carries the full VERIFIED_USD, co-occurring levers settle VERIFIED at $0; one-time corrective pass fixes rows V038 already double-booked' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 118);
