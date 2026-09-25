-- V153__ledger_autobook_full_window_settle.sql -- settle autobooked savings on the FULL 14-day window.
--
--   Next-Fifty #11 (+ the #5 adopt follow-up). SP_WAREHOUSE_CHANGE_SCAN (V109) refreshes a change's
--   AFTER_* stats and VERDICT daily while CURRENT_DATE() <= TRACKING_UNTIL (detection day + 14), and a
--   row leaves PENDING as soon as AFTER_DAYS >= 3 AND AFTER_QUERIES >= 20. SP_LEDGER_AUTOBOOK (V145)
--   settled any non-PENDING row, so VERIFIED_USD froze on ~3 days of after-data and was never
--   re-measured; NO_BASELINE rows settled the SAME day they were booked (no after-rate yet, COALESCEd
--   to 0 = the whole baseline booked as saved). The credit rate was also read into a NUMBER (scale 0)
--   via TRY_TO_NUMBER, so the seeded '3.68' priced as 4 (+8.7% on every autobook VERIFIED_USD).
--
--   SP_LEDGER_AUTOBOOK is re-derived from V145, its current definition (V145 already carries V118's
--   LBA-1 dedup and V038's book/settle core -- preserved byte-for-byte). Deltas, each locked by the
--   normalize-and-compare test tests/migrations/test_v153_ledger_autobook_full_window_settle.py:
--     1. rate FLOAT via TRY_TO_DOUBLE (every other proc's idiom).
--     2. ADOPT: an app-booked manual ESTIMATED row for the same change (the Next-Fifty #5 twin rule,
--        same ON/WHERE lines as mart_sql._ledger_twin_select) gets SOURCE_CHANGE_ID stamped instead of
--        the scan booking a second row for the change. One-to-one; unbooked saving-direction only.
--     3. Settle gate: CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL AND VERDICT
--        IN (IMPROVED, NEUTRAL, REGRESSED, NO_BASELINE, INSUFFICIENT_AFTER). NO_BASELINE /
--        INSUFFICIENT_AFTER mean fewer than 20 queries in a window, not missing credits -- they settle
--        on the measured credits too (a low-query warehouse is the usual auto-suspend target).
--     4. Settle NOTE: full-window wording + per-day query-volume ratio, VOLUME_CONFOUNDED outside
--        0.7-1.3x (dollars NOT adjusted), PERF_REGRESSED on a REGRESSED verdict that still saved, and
--        PERF_UNJUDGED on NO_BASELINE / INSUFFICIENT_AFTER.
--     5. Close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with nothing metered after
--        it (AFTER_CREDITS_PER_DAY NULL) closes REJECTED with no dollars.
--   No MIN_CLUSTERS arm: that lever stays registry-only (Next-Fifty #38 is wave 3).
--   Timing: a change now settles 15-16 days after it is made (the morning after its 14-day window
--   closes), not ~3; "verified this quarter" / the active run-rate move accordingly.
--   SP_VERIFY_IDLE_SAVINGS is re-derived from V053 to skip rows tied to a detected change.
--   Forward-only: already-settled rows are never rewritten here (re-settling them is a separate owner
--   opt-in, snowflake/resettle_autobook_14d.sql). No schema change, no task change, teardown
--   unchanged. Apply AFTER V152. Idempotent.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20153, 'V153 requires V152 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 152) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LEDGER_AUTOBOOK  (from V145; full-window settle gate, volume note, adopt, FLOAT rate, close-out; V153)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rate FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :rate
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- V153 ADOPT (Next-Fifty #5 follow-up): an app-booked manual ESTIMATED row for the SAME change is
    -- stamped with the change's SOURCE_CHANGE_ID instead of the INSERT below booking a second row. Same
    -- match as the app twin rule (mart_sql._ledger_twin_select): same warehouse, same lever (RESIZE ==
    -- SIZE), the scan saw it from 1h before to 3 days after the row was booked. One-to-one only (first
    -- change per row AND first row per change), only unbooked saving-direction changes; anything else
    -- falls through to the INSERT + the app twin rule exactly as before. The adopted row keeps its app
    -- ESTIMATED_USD and settles below like any autobook row.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET SOURCE_CHANGE_ID = a.CHANGE_ID,
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | adopted by the daily change scan (change '
                        || a.CHANGE_ID || '): settles on its 14-day measured window.', 2000)
      FROM (SELECT m.ITEM_ID, r.CHANGE_ID
              FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER m
              JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))
               AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))
               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)
               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', 3, m.CREATED_AT)
             WHERE m.SOURCE_CHANGE_ID IS NULL
               AND m.STATE = 'ESTIMATED'
               AND UPPER(TRIM(m.FINDING_TYPE)) IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')
               AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER b
                               WHERE b.SOURCE_CHANGE_ID = r.CHANGE_ID)
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
               )
            QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1
                AND ROW_NUMBER() OVER (PARTITION BY r.CHANGE_ID ORDER BY m.CREATED_AT, m.ITEM_ID) = 1) a
     WHERE l.ITEM_ID = a.ITEM_ID
       AND l.SOURCE_CHANGE_ID IS NULL;

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
    -- V153: settle ONLY once the change's 14-day after-window has CLOSED (CURRENT_DATE() >
    -- TRACKING_UNTIL is the scan's own close-out predicate: it stops refreshing AFTER_* and VERDICT
    -- then) and only when credits were metered after the change; the V145 gate settled on ~3 days
    -- of after-data. NO_BASELINE / INSUFFICIENT_AFTER (fewer than 20 queries in a window) settle on
    -- CREDITS too, marked PERF_UNJUDGED. The NOTE carries the per-day query-volume ratio (after vs the
    -- 14-day baseline), VOLUME_CONFOUNDED outside 0.7-1.3x. Dollars are never adjusted for volume.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),
           VERIFIED_USD = CASE
                              WHEN s.WH_SAVED_MONTHLY_USD < 5 THEN NULL
                              WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)
                              ELSE 0
                          END,
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured on the full window: '
                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' credits/day (14d baseline) -> '
                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))
                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))
                        || 'd after (' || s.VERDICT || '); floor $5/mo.'
                        || ' | volume ' || COALESCE(TO_VARCHAR(ROUND(s.VOL_RATIO, 2)), '?')
                        || 'x baseline queries/day'
                        || IFF(s.VOL_RATIO < 0.7 OR s.VOL_RATIO > 1.3,
                               ' - VOLUME_CONFOUNDED: the workload itself moved, so part of the credit delta may not be the lever (dollars not adjusted).',
                               '.')
                        || IFF(s.VERDICT = 'REGRESSED' AND s.WH_SAVED_MONTHLY_USD >= 5,
                               ' | PERF_REGRESSED: credits fell but latency, queueing or failures got worse (WH_CHANGE_REGRESSION).', '')
                        || IFF(s.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER'),
                               ' | PERF_UNJUDGED: fewer than 20 queries in a window, so latency/queue/failure axes were not judged.', '')
                        || IFF(s.WH_SAVED_MONTHLY_USD >= 5 AND s.RN > 1,
                               ' | LBA-1 co-attributed: warehouse saving booked once on change '
                               || s.PRIMARY_CHANGE_ID || '.', ''), 2000)
      FROM (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                   r.BASELINE_CREDITS_PER_DAY AS BASE,
                   r.AFTER_CREDITS_PER_DAY AS AFT,
                   -- V153: per-day query volume after the change vs the 14-day baseline
                   -- (BASELINE_QUERIES is a 14-day total, AFTER_QUERIES a total over AFTER_DAYS).
                   (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))
                       / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO,
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
             WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')
               AND CURRENT_DATE() > r.TRACKING_UNTIL
               AND r.AFTER_CREDITS_PER_DAY IS NOT NULL
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

    -- V153 close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with NO metered credits
    -- after it (AFTER_CREDITS_PER_DAY NULL: the warehouse never ran after the change) has nothing to
    -- measure, so it closes REJECTED with no dollars. The V145 gate settled these through the credit
    -- formula with the NULL after-rate COALESCEd to 0, i.e. booked the WHOLE baseline as VERIFIED.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = 'REJECTED',
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | not measurable (' || r.VERDICT
                        || '): no metered credits after the change, so no saving is booked.', 2000)
      FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
     WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID
       AND l.STATE = 'ESTIMATED'
       AND r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
       AND CURRENT_DATE() > r.TRACKING_UNTIL
       AND r.AFTER_CREDITS_PER_DAY IS NULL;

    RETURN 'OK';
END;
$$;

-- >>> derived:SP_VERIFY_IDLE_SAVINGS  (from V053; skip rows tied to a detected change; V153)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_VERIFICATION_RUNS
        (ITEM_ID, WAREHOUSE_NAME, BASELINE_EST_USD, MEASURED_IDLE_USD_30D, PROPOSED_VERIFIED_USD)
    WITH items AS (
        SELECT ITEM_ID,
               COALESCE(NULLIF(TARGET_OBJECT, ''),
                        TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', ''))) AS WAREHOUSE_NAME,
               ESTIMATED_USD
        FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        WHERE STATE = 'ESTIMATED'
          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')
          -- V153: a row tied to a detected change (autobook or adopted) settles on the change
          -- scan's measured window -- an idle-spend proposal for it is noise, or a second verify.
          AND SOURCE_CHANGE_ID IS NULL
    ),
    query_hours AS (
        SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= DATEADD('day', -30, CURRENT_DATE())
          AND WAREHOUSE_NAME IS NOT NULL
    ),
    idle_now AS (
        SELECT M.WAREHOUSE_NAME,
               SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) * :credit_price AS IDLE_USD_30D
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
        LEFT JOIN query_hours Q
               ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
              AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
        WHERE M.START_TIME >= DATEADD('day', -30, CURRENT_DATE())
        GROUP BY M.WAREHOUSE_NAME
    )
    SELECT i.ITEM_ID, i.WAREHOUSE_NAME, i.ESTIMATED_USD,
           ROUND(COALESCE(n.IDLE_USD_30D, 0), 2),
           ROUND(GREATEST(0, i.ESTIMATED_USD - COALESCE(n.IDLE_USD_30D, 0)), 2)
    FROM items i
    LEFT JOIN idle_now n ON UPPER(n.WAREHOUSE_NAME) = UPPER(i.WAREHOUSE_NAME);

    RETURN 'savings verification run complete';
END;
$$;

-- Run once at apply (the same work TASK_LEDGER_AUTOBOOK does after the 06:40 America/Chicago change
-- scan). This is NOT a no-op: it can ADOPT a manual ESTIMATED row booked in the last 3 days whose change
-- the scan has already seen but not yet booked. On a FIRST apply nothing is settle-eligible yet (the V145
-- gate already settled every non-PENDING row, and a closed window is never PENDING); on a rebuild
-- replay it simply settles whatever windows have closed, with this file's logic. Run it under the
-- RUN_NEXT prelude ALTER SESSION SET TIMEZONE = 'America/Chicago' so CURRENT_DATE() and ADOPT's
-- LTZ -> NTZ cast use the task clock, not a UTC worksheet's. If it errors, SCHEMA_VERSION 153 is not
-- written; ADOPT stamps may already be committed (harmless under V145) -- roll back by re-running the
-- V145 and V053 CREATE OR REPLACE blocks.
CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 153 AS VERSION,
       'SP_LEDGER_AUTOBOOK settles on the FULL 14-day window (Next-Fifty #11): re-derived from V145 (V118 LBA-1 dedup + V038 core preserved byte-for-byte) with settle gate CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL over the 5 closed verdicts, so a change settles 15-16 days after it is made instead of ~3; NO_BASELINE / INSUFFICIENT_AFTER settle on metered credits with a PERF_UNJUDGED note and close REJECTED only when nothing was metered after the change (V145 booked the whole baseline for them); the settle note carries the per-day query-volume ratio with a VOLUME_CONFOUNDED marker outside 0.7-1.3x (dollars not adjusted) and PERF_REGRESSED; the credit rate is FLOAT via TRY_TO_DOUBLE (TRY_TO_NUMBER rounded 3.68 to 4, +8.7%); an app-booked manual row for the same change is ADOPTED (SOURCE_CHANGE_ID stamped, Next-Fifty #5 twin rule) instead of double-booked. No MIN_CLUSTERS arm. SP_VERIFY_IDLE_SAVINGS re-derived from V053 to skip change-tied rows. Forward-only (historical rows untouched; re-settle is an owner opt-in); no schema or task change; teardown unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 153);
