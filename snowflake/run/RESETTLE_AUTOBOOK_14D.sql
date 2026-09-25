-- snowflake/resettle_autobook_14d.sql -- OPT-IN historical re-settle of autobooked savings (Next-Fifty #11, owner
-- decision O-8). NOT a migration: stage a copy as its OWN runbox file (never inside RUN_NEXT.sql), and run it
-- only AFTER V153 is applied. Grid 1 / 1b are read-only; grid 2 is commented until you decide.
--
-- Why: before V153, SP_LEDGER_AUTOBOOK settled every autobook row as soon as its verdict left PENDING --
-- ~3 days of after-data, NO_BASELINE rows on day 0 with a NULL after-rate COALESCEd to 0 (the WHOLE
-- baseline booked as VERIFIED), and a credit rate read with TRY_TO_NUMBER (the seeded '3.68' priced at 4,
-- +8.7%). V153 fixes all three going forward; rows already settled keep their old figures. The app shows
-- the gap without writing anything (Cost > Optimize > Savings ledger, "Remeasured 14d monthly USD").
--
-- The plan mirrors V153 exactly: settle on the CLOSED window (CURRENT_DATE() > TRACKING_UNTIL) when
-- credits were metered after the change, over the 5 closed verdicts, the $5/mo floor, the LBA-1 RN
-- (co-attributed rows $0) and the FLOAT rate; a closed NO_BASELINE / INSUFFICIENT_AFTER change with
-- nothing metered after it closes REJECTED with no dollars. Scope: AUTO-settled rows only
-- (VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'), never a hand-verified row, never a row V153 already
-- settled, and only rows whose state or dollars would actually change.
--
-- Grid 2 is a RESTATEMENT: it rewrites STATE / VERIFIED_USD and appends a NOTES sentinel, but keeps
-- VERIFIED_AT, so no row moves between quarters. The sentinel makes a re-run a no-op. Record the run in
-- the CHANGELOG (O-8 disclosure).

USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- CURRENT_DATE() must be the task clock that wrote TRACKING_UNTIL

-- GRID 1 (read-only preview): every row that would change -- old vs new state and dollars, and the
-- old / new VERIFIED totals across the changed rows only (SUM OVER, no LIMIT).
SELECT p.ITEM_ID, p.DESCRIPTION, p.VERDICT, p.AFTER_DAYS, p.VOL_RATIO, p.RN,
       p.OLD_STATE, p.OLD_USD, p.NEW_STATE, p.NEW_USD,
       COALESCE(p.NEW_USD, 0) - COALESCE(p.OLD_USD, 0) AS DELTA_USD, p.VERIFIED_AT,
       SUM(IFF(p.OLD_STATE = 'VERIFIED', COALESCE(p.OLD_USD, 0), 0)) OVER () AS OLD_VERIFIED_TOTAL_CHANGED_ROWS,
       SUM(COALESCE(p.NEW_USD, 0)) OVER () AS NEW_VERIFIED_TOTAL_CHANGED_ROWS
  FROM (SELECT q.*
          FROM (SELECT l2.ITEM_ID, l2.DESCRIPTION, l2.STATE AS OLD_STATE, l2.VERIFIED_USD AS OLD_USD,
                       l2.VERIFIED_AT, COALESCE(g.VERDICT, c.VERDICT) AS VERDICT, g.AFTER_DAYS,
                       ROUND(g.VOL_RATIO, 2) AS VOL_RATIO, g.RN,
                       IFF(g.CHANGE_ID IS NOT NULL, IFF(g.WH_USD >= 5, 'VERIFIED', 'REJECTED'), 'REJECTED') AS NEW_STATE,
                       IFF(g.WH_USD >= 5, IFF(g.RN = 1, ROUND(g.WH_USD, 2), 0), NULL) AS NEW_USD
                  FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
                  LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                         ROW_NUMBER() OVER (
                             PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                          r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                             ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
                         (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                             * px.RATE * 30 AS WH_USD,
                         (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))
                             / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO
                    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                    CROSS JOIN (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS RATE
                                  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) px
                   WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')
                     AND CURRENT_DATE() > r.TRACKING_UNTIL
                     AND r.AFTER_CREDITS_PER_DAY IS NOT NULL
                     AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER x
                                  WHERE x.SOURCE_CHANGE_ID = r.CHANGE_ID)) g ON g.CHANGE_ID = l2.SOURCE_CHANGE_ID
                  LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT
                    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                   WHERE r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
                     AND CURRENT_DATE() > r.TRACKING_UNTIL
                     AND r.AFTER_CREDITS_PER_DAY IS NULL) c ON c.CHANGE_ID = l2.SOURCE_CHANGE_ID
                 WHERE (g.CHANGE_ID IS NOT NULL OR c.CHANGE_ID IS NOT NULL)
                   AND l2.VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'
                   AND l2.STATE IN ('VERIFIED', 'REJECTED')
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%measured on the full window%'
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%not measurable (%'
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%re-settled on the full 14-day window%') q
         WHERE q.NEW_STATE <> q.OLD_STATE
            OR COALESCE(q.NEW_USD, -1) <> COALESCE(q.OLD_USD, -1)) p
 ORDER BY ABS(COALESCE(p.NEW_USD, 0) - COALESCE(p.OLD_USD, 0)) DESC;

-- GRID 1b (read-only): the same plan rolled up by verdict and state transition. The
-- NO_BASELINE / INSUFFICIENT_AFTER VERIFIED rows are the whole-baseline false verifies (O-8 minimum).
SELECT p.VERDICT, p.OLD_STATE, p.NEW_STATE, COUNT(*) AS ITEMS,
       SUM(COALESCE(p.OLD_USD, 0)) AS OLD_USD, SUM(COALESCE(p.NEW_USD, 0)) AS NEW_USD,
       SUM(COALESCE(p.NEW_USD, 0)) - SUM(COALESCE(p.OLD_USD, 0)) AS DELTA_USD
  FROM (SELECT q.*
          FROM (SELECT l2.ITEM_ID, l2.DESCRIPTION, l2.STATE AS OLD_STATE, l2.VERIFIED_USD AS OLD_USD,
                       l2.VERIFIED_AT, COALESCE(g.VERDICT, c.VERDICT) AS VERDICT, g.AFTER_DAYS,
                       ROUND(g.VOL_RATIO, 2) AS VOL_RATIO, g.RN,
                       IFF(g.CHANGE_ID IS NOT NULL, IFF(g.WH_USD >= 5, 'VERIFIED', 'REJECTED'), 'REJECTED') AS NEW_STATE,
                       IFF(g.WH_USD >= 5, IFF(g.RN = 1, ROUND(g.WH_USD, 2), 0), NULL) AS NEW_USD
                  FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
                  LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                         ROW_NUMBER() OVER (
                             PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                          r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                             ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
                         (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                             * px.RATE * 30 AS WH_USD,
                         (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))
                             / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO
                    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                    CROSS JOIN (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS RATE
                                  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) px
                   WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')
                     AND CURRENT_DATE() > r.TRACKING_UNTIL
                     AND r.AFTER_CREDITS_PER_DAY IS NOT NULL
                     AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER x
                                  WHERE x.SOURCE_CHANGE_ID = r.CHANGE_ID)) g ON g.CHANGE_ID = l2.SOURCE_CHANGE_ID
                  LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT
                    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                   WHERE r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
                     AND CURRENT_DATE() > r.TRACKING_UNTIL
                     AND r.AFTER_CREDITS_PER_DAY IS NULL) c ON c.CHANGE_ID = l2.SOURCE_CHANGE_ID
                 WHERE (g.CHANGE_ID IS NOT NULL OR c.CHANGE_ID IS NOT NULL)
                   AND l2.VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'
                   AND l2.STATE IN ('VERIFIED', 'REJECTED')
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%measured on the full window%'
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%not measurable (%'
                   AND COALESCE(l2.NOTES, '') NOT LIKE '%re-settled on the full 14-day window%') q
         WHERE q.NEW_STATE <> q.OLD_STATE
            OR COALESCE(q.NEW_USD, -1) <> COALESCE(q.OLD_USD, -1)) p
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3;

-- GRID 2 (WRITES -- commented). After reading grid 1, uncomment every line of this statement and run
-- it ONCE. It re-applies the exact grid-1 plan (the same subquery).
-- UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
--    SET STATE = p.NEW_STATE,
--        VERIFIED_USD = p.NEW_USD,
--        NOTES = LEFT(COALESCE(l.NOTES, '') || ' | re-settled on the full 14-day window (owner opt-in '
--                     || TO_VARCHAR(CURRENT_DATE()) || '; was ' || l.STATE || ' '
--                     || COALESCE(TO_VARCHAR(l.VERIFIED_USD), '-') || ').', 2000)
--   FROM (SELECT q.*
--           FROM (SELECT l2.ITEM_ID, l2.DESCRIPTION, l2.STATE AS OLD_STATE, l2.VERIFIED_USD AS OLD_USD,
--                        l2.VERIFIED_AT, COALESCE(g.VERDICT, c.VERDICT) AS VERDICT, g.AFTER_DAYS,
--                        ROUND(g.VOL_RATIO, 2) AS VOL_RATIO, g.RN,
--                        IFF(g.CHANGE_ID IS NOT NULL, IFF(g.WH_USD >= 5, 'VERIFIED', 'REJECTED'), 'REJECTED') AS NEW_STATE,
--                        IFF(g.WH_USD >= 5, IFF(g.RN = 1, ROUND(g.WH_USD, 2), 0), NULL) AS NEW_USD
--                   FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
--                   LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
--                          ROW_NUMBER() OVER (
--                              PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
--                                           r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
--                              ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
--                          (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
--                              * px.RATE * 30 AS WH_USD,
--                          (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))
--                              / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO
--                     FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
--                     CROSS JOIN (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS RATE
--                                   FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) px
--                    WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')
--                      AND CURRENT_DATE() > r.TRACKING_UNTIL
--                      AND r.AFTER_CREDITS_PER_DAY IS NOT NULL
--                      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER x
--                                   WHERE x.SOURCE_CHANGE_ID = r.CHANGE_ID)) g ON g.CHANGE_ID = l2.SOURCE_CHANGE_ID
--                   LEFT JOIN (SELECT r.CHANGE_ID, r.VERDICT
--                     FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
--                    WHERE r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
--                      AND CURRENT_DATE() > r.TRACKING_UNTIL
--                      AND r.AFTER_CREDITS_PER_DAY IS NULL) c ON c.CHANGE_ID = l2.SOURCE_CHANGE_ID
--                  WHERE (g.CHANGE_ID IS NOT NULL OR c.CHANGE_ID IS NOT NULL)
--                    AND l2.VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'
--                    AND l2.STATE IN ('VERIFIED', 'REJECTED')
--                    AND COALESCE(l2.NOTES, '') NOT LIKE '%measured on the full window%'
--                    AND COALESCE(l2.NOTES, '') NOT LIKE '%not measurable (%'
--                    AND COALESCE(l2.NOTES, '') NOT LIKE '%re-settled on the full 14-day window%') q
--          WHERE q.NEW_STATE <> q.OLD_STATE
--             OR COALESCE(q.NEW_USD, -1) <> COALESCE(q.OLD_USD, -1)) p
--  WHERE l.ITEM_ID = p.ITEM_ID
--    AND l.STATE IN ('VERIFIED', 'REJECTED')
--    -- O-8 minimum scope (uncomment to re-settle ONLY the whole-baseline false verifies):
--    -- AND p.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
-- ;

ALTER SESSION UNSET TIMEZONE;
