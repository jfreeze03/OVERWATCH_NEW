-- V145__ledger_autobook_stamp_finding_type.sql — book the savings lever (owner ask).
--
--   The Decision Studio ROI "Where the realized savings come from — by lever" chart showed
--   ALL verified savings as "unclassified". Root cause: SP_LEDGER_AUTOBOOK (the DOMINANT
--   booking path — every detected warehouse cost-lever change books through it) never set
--   SAVINGS_LEDGER.FINDING_TYPE (added later by V053), even though the lever is right there
--   in the INSERT's source row (WAREHOUSE_CHANGE_REGISTRY.SETTING). So autobook rows landed
--   with FINDING_TYPE = NULL and the by-lever rollup pooled them all into 'unclassified'.
--   (The $0 ESTIMATED_USD is by design — no invented numbers — so Realization % is N/A there.)
--
--   Fix: re-derive SP_LEDGER_AUTOBOOK (from V118, its current form) to STAMP FINDING_TYPE from
--   r.SETTING, mapping SIZE -> 'RESIZE' so autobook resize rows share the app RESIZE bucket
--   (the same SIZE->RESIZE alias proven_fix_transfer already uses). Proc body is byte-identical
--   to V118 except the INSERT column list + value. STEP 2 is a ONE-TIME idempotent backfill that
--   stamps existing autobook rows from the registry, guarded by FINDING_TYPE-is-empty so a re-run
--   is a no-op and NULL-source manual rows stay 'unclassified'. Apply AFTER V144. Idempotent.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20145, 'V145 requires V144 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 144) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LEDGER_AUTOBOOK (from V118; stamp FINDING_TYPE from registry SETTING, V145)
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

    -- Book detected cost-lever changes as ESTIMATED $0. V145: also stamp FINDING_TYPE from the
    -- source SETTING (SIZE -> RESIZE) so the lever is on the row, not just in the free-text NOTE.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, SOURCE_CHANGE_ID, FINDING_TYPE)
    SELECT 'Detected ' || r.SETTING || ' change on ' || r.WAREHOUSE_NAME || ': '
               || COALESCE(r.OLD_VALUE, '?') || ' -> ' || COALESCE(r.NEW_VALUE, '?'),
           'ESTIMATED',
           0,
           'SELECT * FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY WHERE CHANGE_ID = ''' || r.CHANGE_ID || '''',
           'Auto-booked from the daily warehouse-change scan; the 14-day measured verdict settles it.',
           r.CHANGE_ID,
           CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END
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
             WHERE r.VERDICT <> 'PENDING'
               -- Rank ONLY changes that Step-1 actually booked a ledger row for (the
               -- saving-direction levers). The registry also holds non-saving changes
               -- (SIZE up, AUTO_SUSPEND up) with the SAME measured-window signature but
               -- NO ledger row; if one of those won RN=1 it would carry the full saving
               -- into a row that doesn't exist while the genuine saving lever settled at
               -- $0 -- booking a real saving as ZERO. Restricting the population to booked
               -- levers guarantees the RN=1 primary always has a row to receive the USD.
               AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
                            WHERE l2.SOURCE_CHANGE_ID = r.CHANGE_ID)) s
     WHERE l.SOURCE_CHANGE_ID = s.CHANGE_ID
       AND l.STATE = 'ESTIMATED';

    RETURN 'OK';
END;
$$;

-- Step 2 (one-time, idempotent): backfill FINDING_TYPE on existing autobook rows from the
-- source registry SETTING (SIZE -> RESIZE), so the historical by-lever rollup is right too.
-- Guarded by FINDING_TYPE-is-empty, so a re-run is a no-op and NULL-source manual rows (no
-- SOURCE_CHANGE_ID -> no registry match) stay 'unclassified'.
EXECUTE IMMEDIATE
$$
BEGIN
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET FINDING_TYPE = CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END
      FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
     WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID
       AND COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''), '') = '';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 145 AS VERSION,
       'SP_LEDGER_AUTOBOOK stamps FINDING_TYPE (the savings lever) from the source WAREHOUSE_CHANGE_REGISTRY.SETTING, SIZE -> RESIZE, so autobooked savings are no longer all "unclassified" in the Decision Studio ROI by-lever chart. Proc-only re-derive (byte-identical to V118 except the INSERT FINDING_TYPE column+value) + a one-time idempotent backfill of existing autobook rows (guarded by FINDING_TYPE-is-empty; NULL-source manual rows stay unclassified). The app savings_ledger() reader also recovers the lever via the registry join so the display is correct before this applies. No new object; teardown unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 145);
