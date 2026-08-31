-- 05_validate.sql — BYTE-IDENTICAL copy of snowflake/validate.sql (locked by
-- tests/test_rebuild_bundle.py); numbered for the rebuild order.

-- validate.sql — post-install contract. Two parts, one worksheet paste:
--   1. The SELECT below prints one OK/FAIL row per check (manual visibility —
--      run it by hand to see every line).
--   2. The EXECUTE IMMEDIATE block at the bottom re-evaluates the load-bearing
--      assertions with TEETH: it RAISEs on the first failure so a headless
--      run ('snow sql -f validate.sql') exits nonzero and CI goes red.
-- Safe to run at the documented deploy step (right after migrations + roles,
-- before the first load): EMPTY key facts PASS — nothing has loaded yet. The
-- freshness assertions only bite once data EXISTS and then goes stale (a loader
-- that ran and stopped), which is the real dead-man signal on re-runs / DR.

WITH checks AS (
    SELECT 'V001..V114 applied' AS CHECK_NAME,
           IFF((SELECT COUNT(DISTINCT VERSION) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION BETWEEN 1 AND 114) = 114,
               'OK', 'FAIL: run missing migrations') AS RESULT
    UNION ALL
    SELECT 'Settings seeded',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                 WHERE KEY IN ('CREDIT_PRICE_USD', 'AI_CREDIT_PRICE_USD')) = 2,
               'OK', 'FAIL: rates missing from SETTINGS')
    UNION ALL
    SELECT 'Credit rate is 3.68 unless deliberately changed',
           IFF((SELECT VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD') IS NOT NULL,
               'OK', 'FAIL')
    UNION ALL
    SELECT 'Company scope seeded (Trexis warehouses)',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
                 WHERE COMPANY = 'Trexis' AND SCOPE_TYPE = 'WAREHOUSE') = 5,
               'OK', 'FAIL: expected 5 Trexis warehouses')
    UNION ALL
    SELECT 'KEBARR1 override present',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
                        WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = 'KEBARR1' AND COMPANY = 'ALFA'),
               'OK', 'FAIL: KEBARR1 must classify as ALFA')
    UNION ALL
    SELECT 'KEBARR1 classifies as ALFA',
           IFF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER('KEBARR1') = 'ALFA', 'OK', 'FAIL')
    UNION ALL
    -- V019 made USER classification role-membership-based (a user is Trexis
    -- if they hold a %TRXS% role); the old check probed a fictional user
    -- against V001's retired prefix rule and failed on every fresh install
    -- (live finding, 2026-07-12 rebuild). The deterministic prefix rule
    -- still governs DATABASES — test that, and the unknown-user fallback.
    SELECT 'TRXS_ database prefix classifies as Trexis',
           IFF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE('TRXS_EDW_PRD') = 'Trexis', 'OK', 'FAIL')
    UNION ALL
    SELECT 'Unknown user classifies UNKNOWN (V044: evidence-based)',
           IFF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER('OW_VALIDATE_PROBE_NOBODY') = 'UNKNOWN', 'OK', 'FAIL')
    UNION ALL
    -- This account does not expose ACCOUNT_USAGE.WAREHOUSES; use
    -- WAREHOUSE_EVENTS_HISTORY (any lifecycle event proves the warehouse
    -- exists). Events can lag ~1-3h after CREATE WAREHOUSE, hence CHECK not
    -- FAIL — confirm with SHOW WAREHOUSES on a fresh install.
    SELECT 'WH_ALFA_ADMIN exists (event evidence)',
           IFF(EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY
                        WHERE WAREHOUSE_NAME = 'WH_ALFA_ADMIN'),
               'OK', 'CHECK: no events yet (lag) — confirm WH_ALFA_ADMIN via SHOW WAREHOUSES')
    UNION ALL
    SELECT 'Alert rules seeded',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG) >= 7, 'OK', 'FAIL')
    UNION ALL
    -- Shared-schema collision checks: if an old-app table with the same name
    -- survived, it keeps the old shape and these columns are missing.
    SELECT 'ALERT_EVENTS has new shape (DEDUPE_KEY)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_EVENTS'
                          AND COLUMN_NAME = 'DEDUPE_KEY'),
               'OK', 'FAIL: old-app ALERT_EVENTS present — rename it and rerun V004')
    UNION ALL
    SELECT 'ALERT_CONFIG has new shape (THRESHOLD_NUM)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_CONFIG'
                          AND COLUMN_NAME = 'THRESHOLD_NUM'),
               'OK', 'FAIL: old-app ALERT_CONFIG present — rename it and rerun V004')
    UNION ALL
    SELECT 'FACT_QUERY_HOURLY has new shape (COMPANY)',
           IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'FACT_QUERY_HOURLY'
                          AND COLUMN_NAME = 'COMPANY'),
               'OK', 'FAIL: old-app FACT_QUERY_HOURLY present — rename it and rerun V002')
    UNION ALL
    -- Real correctness (not just presence). These same assertions are
    -- re-evaluated with teeth in the EXECUTE IMMEDIATE contract below.
    SELECT 'Credit rate is a positive number',
           IFF(TRY_TO_DOUBLE((SELECT VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                               WHERE KEY = 'CREDIT_PRICE_USD')) > 0,
               'OK', 'FAIL: CREDIT_PRICE_USD <= 0 or non-numeric — everything prices to $0')
    UNION ALL
    -- Deliberate-override convention: to run a non-3.68 contracted rate,
    -- also seed SETTINGS('CREDIT_PRICE_OVERRIDE','TRUE'). Absent that flag a
    -- drifted rate is treated as an accident and fails.
    SELECT 'Credit rate = 3.68 (or CREDIT_PRICE_OVERRIDE set)',
           IFF(ABS(COALESCE(TRY_TO_DOUBLE((SELECT VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                                            WHERE KEY = 'CREDIT_PRICE_USD')), -1) - 3.68) <= 0.0001
               OR EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                           WHERE KEY = 'CREDIT_PRICE_OVERRIDE'
                             AND UPPER(COALESCE(VALUE, '')) IN ('TRUE', 'Y', 'YES', '1')),
               'OK', 'FAIL: rate != 3.68 and no deliberate CREDIT_PRICE_OVERRIDE flag')
    UNION ALL
    -- Freshness / loader dead-man. SLA = 3 days: healthy newest DAY is
    -- CURRENT_DATE-1..-2 (ACCOUNT_USAGE metering finalization lag); 3 gives
    -- one day of slack while still catching a loader that has stalled for
    -- days. EMPTY table (MAX(DAY) IS NULL) reads as OK-pending: at the deploy
    -- step nothing has loaded yet, so it must not fail. A populated fact whose
    -- newest DAY is past the SLA is the real stalled-loader signal.
    SELECT 'FACT_METERING_DAILY fresh (empty ok pre-load; else newest DAY <= 3d)',
           IFF((SELECT MAX(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY) IS NULL, 'OK (no data yet)',
               IFF(DATEDIFF('DAY', (SELECT MAX(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY),
                            CURRENT_DATE()) <= 3,
                   'OK', 'FAIL: newest DAY older than 3d SLA — loader stalled'))
    UNION ALL
    SELECT 'FACT_WAREHOUSE_DAILY fresh (empty ok pre-load; else newest DAY <= 3d)',
           IFF((SELECT MAX(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY) IS NULL, 'OK (no data yet)',
               IFF(DATEDIFF('DAY', (SELECT MAX(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY),
                            CURRENT_DATE()) <= 3,
                   'OK', 'FAIL: newest DAY older than 3d SLA — loader stalled'))
    UNION ALL
    -- An ENABLED route with a blank integration silently drops notifications.
    SELECT 'No enabled ALERT_ROUTES with blank INTEGRATION_NAME',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
                 WHERE ENABLED = TRUE
                   AND (INTEGRATION_NAME IS NULL OR TRIM(INTEGRATION_NAME) = '')) = 0,
               'OK', 'FAIL: an enabled route has no integration — alerts drop silently')
    UNION ALL
    -- 29 = full seeded rule set across V001..V072 (V004 seeds 7; V007/V009/
    -- V010/V011/V012/V014/V016/V017/V024/V043/V061 add rules; V084 adds
    -- SEC_NEW_EXPOSURE; V085 adds PERF_SLO_BREACH; V034 retires
    -- SEC_BREAK_GLASS_USE). Floor, not exact, so a future rule cannot break CI.
    SELECT 'ALERT_CONFIG rule count >= 29 (full seed)',
           IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG) >= 29,
               'OK', 'FAIL: expected >= 29 rules — seed incomplete')
    UNION ALL
    SELECT 'Key procs present (platform + security loaders)',
           IFF((SELECT COUNT(DISTINCT PROCEDURE_NAME)
                  FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
                 WHERE PROCEDURE_SCHEMA = 'OVERWATCH'
                   AND PROCEDURE_NAME IN ('SP_LOAD_MARTS_V27', 'SP_ALERT_SCAN',
                                          'SP_NOTIFY_WEBHOOK', 'SP_NIGHTLY_RECONCILE',
                                          'SP_LOAD_SECURITY_FACTS')) = 5,
               'OK', 'FAIL: one or more key procs missing')
)
SELECT * FROM checks
ORDER BY 1;
-- Task monitoring removed from this script (owner decision 2026-07-12: not
-- used, and INFORMATION_SCHEMA.TASK_DEPENDENTS needs a database context that
-- a bare worksheet run does not have). Task-state diagnosis lives in
-- snowflake/loader_chain_check.sql when you actually need it. Task-graph
-- assertions stay out of this file on purpose — they belong to the V071
-- owner smoke test. This contract asserts only db-context-free facts.

-- ---------------------------------------------------------------------------
-- CONTRACT (part 2): the same load-bearing assertions, with teeth.
-- The SELECT above is for the human; this block is for CI. It re-checks each
-- load-bearing invariant and RAISEs a specific, actionable exception on the
-- FIRST failure, so 'snow sql -f validate.sql' exits nonzero. Fail-fast:
-- fix the reported assertion, rerun, repeat. All object references are
-- fully qualified (no db context assumed), matching this file's decision to
-- keep task-graph/db-context checks out.
-- ---------------------------------------------------------------------------
EXECUTE IMMEDIATE $$
DECLARE
    n_versions   INT;
    v_rate       FLOAT;
    n_override   INT;
    stale_meter  INT;
    stale_wh     INT;
    n_blank      INT;
    n_rules      INT;
    n_procs      INT;
    freshness_sla_days INT DEFAULT 3;   -- loader dead-man SLA (ACCOUNT_USAGE lag-aware)

    e_migrations  EXCEPTION (-20011, 'VALIDATE FAIL: V001..V114 migration floor not met — run missing migrations');
    e_rate_pos    EXCEPTION (-20012, 'VALIDATE FAIL: CREDIT_PRICE_USD is <= 0 or non-numeric — everything prices to $0');
    e_rate_368    EXCEPTION (-20013, 'VALIDATE FAIL: CREDIT_PRICE_USD != 3.68 and no CREDIT_PRICE_OVERRIDE flag — seed SETTINGS(''CREDIT_PRICE_OVERRIDE'',''TRUE'') to run a non-default rate on purpose');
    e_meter_fresh EXCEPTION (-20014, 'VALIDATE FAIL: FACT_METERING_DAILY newest DAY older than the freshness SLA — the metering loader has stalled (empty passes: nothing loaded yet)');
    e_wh_fresh    EXCEPTION (-20015, 'VALIDATE FAIL: FACT_WAREHOUSE_DAILY newest DAY older than the freshness SLA — the warehouse loader has stalled (empty passes: nothing loaded yet)');
    e_route_blank EXCEPTION (-20016, 'VALIDATE FAIL: an ENABLED ALERT_ROUTES row has a blank INTEGRATION_NAME — those alerts drop silently');
    e_alert_cfg   EXCEPTION (-20017, 'VALIDATE FAIL: ALERT_CONFIG rule count below the expected floor (29) — the rule seed is incomplete');
    e_procs       EXCEPTION (-20018, 'VALIDATE FAIL: a key platform/security proc is missing');
BEGIN
    -- (a) migration floor: V001..V114 all applied.
    SELECT COUNT(DISTINCT VERSION) INTO :n_versions
      FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
     WHERE VERSION BETWEEN 1 AND 88;
    IF (n_versions < 88) THEN RAISE e_migrations; END IF;

    -- (b) credit rate is a positive number.
    SELECT TRY_TO_DOUBLE(VALUE) INTO :v_rate
      FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
     WHERE KEY = 'CREDIT_PRICE_USD';
    IF (v_rate IS NULL OR v_rate <= 0) THEN RAISE e_rate_pos; END IF;

    -- (c) credit rate is 3.68 unless a deliberate override flag exists.
    SELECT COUNT(*) INTO :n_override
      FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
     WHERE KEY = 'CREDIT_PRICE_OVERRIDE'
       AND UPPER(COALESCE(VALUE, '')) IN ('TRUE', 'Y', 'YES', '1');
    IF (ABS(v_rate - 3.68) > 0.0001 AND n_override = 0) THEN RAISE e_rate_368; END IF;

    -- (d) daily facts are fresh. EMPTY (MAX(DAY) IS NULL -> stale_* IS NULL) is
    -- the pre-load deploy state and PASSES; only a POPULATED-but-stale fact
    -- (loader ran, then stopped) trips the dead-man. This is why validate.sql
    -- is safe to run at the documented step before the first load.
    SELECT DATEDIFF('DAY', MAX(DAY), CURRENT_DATE()) INTO :stale_meter
      FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY;
    IF (stale_meter IS NOT NULL AND stale_meter > freshness_sla_days) THEN RAISE e_meter_fresh; END IF;

    SELECT DATEDIFF('DAY', MAX(DAY), CURRENT_DATE()) INTO :stale_wh
      FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY;
    IF (stale_wh IS NOT NULL AND stale_wh > freshness_sla_days) THEN RAISE e_wh_fresh; END IF;

    -- (e) no enabled route ships to a blank integration.
    SELECT COUNT(*) INTO :n_blank
      FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
     WHERE ENABLED = TRUE
       AND (INTEGRATION_NAME IS NULL OR TRIM(INTEGRATION_NAME) = '');
    IF (n_blank > 0) THEN RAISE e_route_blank; END IF;

    -- (f) the full alert-rule set is seeded (floor of 29).
    SELECT COUNT(*) INTO :n_rules
      FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG;
    IF (n_rules < 29) THEN RAISE e_alert_cfg; END IF;

    -- (g) the load-bearing procs exist.
    SELECT COUNT(DISTINCT PROCEDURE_NAME) INTO :n_procs
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
     WHERE PROCEDURE_SCHEMA = 'OVERWATCH'
       AND PROCEDURE_NAME IN ('SP_LOAD_MARTS_V27', 'SP_ALERT_SCAN',
                              'SP_NOTIFY_WEBHOOK', 'SP_NIGHTLY_RECONCILE',
                              'SP_LOAD_SECURITY_FACTS');
    IF (n_procs < 5) THEN RAISE e_procs; END IF;

    RETURN 'validate.sql contract: all load-bearing assertions passed';
END;
$$;
