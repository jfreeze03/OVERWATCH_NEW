-- V096__alert_scan_dedupe_keys.sql
--
-- Alert-scan dedupe/clear keys that encode the state they need. Three related alerting
-- defects, all from a dedupe/clear key not carrying the band/state token (or the sweep
-- matching the wrong token). Re-derives two procs from their latest defs:
--
--   SP_ALERT_SCAN (from V091):
--     (1) [MED] auto-clear never fires for next-day-cleared conditions -- the sweep matched
--         `DEDUPE_KEY LIKE '%|' || CURRENT_DATE()` (today only) but trailing-24h conditions
--         drop below CLEAR the NEXT day, stranding the event OPEN. Now matches a RAISED_AT
--         recency window (>= 48h ago) with the existing >=1h dwell + hysteresis NOT-IN kept;
--         the ENABLED+AUTO_CLEAR_ENABLED rule scope still excludes fact-day rules.
--     (3) [LOW] SEC_CRED_EXPIRY EXPIRING->EXPIRED double-count -- supersede sweep now resolves
--         the EXPIRING HIGH when its EXPIRED CRITICAL sibling exists (|EXPIRING|->|EXPIRED|).
--     (2b) supersede sweep also maps |HIGH|->|CRIT| for the SLO burn band below.
--
--   SP_SLO_BREACH_SCAN (from V085):
--     (2) [MED] same-day HIGH->CRITICAL escalation swallowed -- the dedupe key had no burn
--         band, so the escalated CRITICAL collided with the earlier HIGH on the identical key
--         and the NOT EXISTS guard suppressed it. Now appends a burn band token
--         (IFF(COALESCE(BURN_MULTIPLE,0)>=2,'CRIT','HIGH')) as its own pipe segment before the
--         date, so HIGH and CRIT get distinct keys (mirrors V066 PIPE/BUDGET), and the
--         supersede sweep resolves the superseded HIGH.
--
-- Two procedure re-derivations, no schema change, no new object, no backfill. Owner applies
-- in Snowsight after V095. Forward-healing (next scan); pre-existing stranded/double-counted
-- events may need a one-time manual resolve or age out. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20096, 'V096 requires V095 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 95) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- v7: every rule block runs in its OWN isolated INSERT with per-block
-- exception capture. One broken rule (revoked view, bad division, drift)
-- logs and increments a counter instead of silently killing ALL alerting —
-- the review's 'ticking bomb' finding, defused. Dedupe semantics unchanged.
DECLARE
    budget_usd FLOAT;
    credit_price FLOAT;
    ai_credit_price FLOAT;
    emsg VARCHAR;
    fails INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :budget_usd, :credit_price, :ai_credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- [wake] V086: return expired per-event snoozes to the triage feed. A snoozed
    -- event sits at STATUS='SNOOZED' (off the OPEN/ACK feed); once its wake time has
    -- passed it goes back to OPEN so it re-surfaces. Isolated + does NOT touch `fails`.
    BEGIN
        -- Restore the TRUE prior status: an ACK'd event that was snoozed wakes back
        -- to ACK (its ACK_BY/ACK_AT are intact), a never-acked one to OPEN. Waking an
        -- acked event to OPEN would strand a stale ACK_AT on an 'open' row and let a
        -- re-ack overwrite it (inflating MTTA). Clear the transient snooze metadata.
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN'),
               SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL
         WHERE STATUS = 'SNOOZED'
           AND SNOOZED_UNTIL IS NOT NULL
           AND SNOOZED_UNTIL <= CURRENT_TIMESTAMP();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_wake_failed', :emsg,
                   'V086 un-snooze - other rules unaffected', CURRENT_ROLE();
    END;

    -- [01] COST_DAILY_CREDITS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL' AS COMPANY, c.SEVERITY,
               'Account daily credits ' || ROUND(f.CREDITS, 1) || ' >= ' || c.THRESHOLD_NUM AS TITLE,
               'Warehouse metering total for ' || f.DAY AS DETAIL,
               f.CREDITS AS METRIC_VALUE,
               c.RULE_ID || '|ALL|' || f.DAY AS DEDUPE_KEY
        FROM cfg c
        JOIN (
            SELECT DAY, SUM(CREDITS_TOTAL) AS CREDITS
            FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
            WHERE DAY >= DATEADD('day', -1, CURRENT_DATE())
            GROUP BY DAY
        ) f ON c.RULE_ID = 'COST_DAILY_CREDITS' AND f.CREDITS >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_DAILY_CREDITS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [02] COST_WH_DAILY_CREDITS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, f.COMPANY, c.SEVERITY,
               f.WAREHOUSE_NAME || ' used ' || ROUND(f.CREDITS_TOTAL, 1) || ' credits on ' || f.DAY,
               'Per-warehouse daily metering.',
               f.CREDITS_TOTAL,
               c.RULE_ID || '|' || f.WAREHOUSE_NAME || '|' || f.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY f
          ON c.RULE_ID = 'COST_WH_DAILY_CREDITS'
         AND f.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND f.CREDITS_TOTAL >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_WH_DAILY_CREDITS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [03] PERF_QUERY_FAIL_PCT
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               'Query failure rate ' || ROUND(q.FAIL_PCT, 1) || '% >= ' || c.THRESHOLD_NUM || '%',
               q.FAILED || ' of ' || q.TOTAL || ' queries failed in last ' || c.WINDOW_HOURS || 'h.',
               q.FAIL_PCT,
               c.RULE_ID || '|' || q.COMPANY || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, SUM(FAILED_COUNT) AS FAILED, SUM(QUERY_COUNT) AS TOTAL,
                   IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY COMPANY
            HAVING SUM(QUERY_COUNT) >= 20
        ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT' AND q.FAIL_PCT >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_QUERY_FAIL_PCT - other rules unaffected', CURRENT_ROLE();
    END;
    -- [04] PERF_QUEUED_MINUTES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' queued ' || ROUND(q.QUEUED_MIN, 1) || ' min in 24h',
               'Queued overload + provisioning time.',
               q.QUEUED_MIN,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES' AND q.QUEUED_MIN >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_QUEUED_MINUTES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [05] PERF_SPILL_GB
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' spilled ' || ROUND(q.SPILL_GB, 1) || ' GB remote in 24h',
               'Remote spill indicates undersized memory for the workload.',
               q.SPILL_GB,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_SPILL_GB' AND q.SPILL_GB >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_SPILL_GB - other rules unaffected', CURRENT_ROLE();
    END;
    -- [10] SEC_CRED_EXPIRY
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(cr.USER_NAME),
               IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'CRITICAL', c.SEVERITY),
               cr.USER_NAME || ' ' || LOWER(cr.TYPE) || ' ''' || cr.NAME || ''' ' ||
                   IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(),
                       'EXPIRED ' || ABS(DATEDIFF('day', cr.EXPIRATION_DATE, CURRENT_TIMESTAMP())) || ' day(s) ago',
                       'expires in ' || DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRATION_DATE) || ' day(s)'),
               'Rotate before ' || TO_VARCHAR(cr.EXPIRATION_DATE, 'YYYY-MM-DD') ||
                   ' to avoid auth failures for jobs and integrations using this credential.',
               DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRATION_DATE),
               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING') || '|' || DATE_TRUNC('week', CURRENT_DATE())
        FROM cfg c
        JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
          ON c.RULE_ID = 'SEC_CRED_EXPIRY'
         -- v9: CREDENTIALS on this account has no DELETED_ON column (the
         -- sibling of the EXPIRES_AT discovery v8 fixed) - live error
         -- 2026-07-08. Without this fix, applying v8 swaps the hourly
         -- EXPIRES_AT failure for an hourly DELETED_ON failure.
         AND cr.EXPIRATION_DATE IS NOT NULL
         AND cr.EXPIRATION_DATE <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_CRED_EXPIRY - other rules unaffected', CURRENT_ROLE();
    END;
    -- [11] COST_CLOUD_SVC_RATIO
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_CLOUD_SVC_RATIO: cloud-services share of a warehouse's credits
        -- (CoCo finding: WH_TRXS_TRANSFORM at ~30%; normal is <10%). Fires
        -- daily per warehouse while the ratio stays above threshold.
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME),
               c.SEVERITY,
               w.WAREHOUSE_NAME || ' cloud-services ratio ' || ROUND(w.RATIO_PCT, 1) || '% (24h)',
               'Cloud services ' || ROUND(w.CS, 2) || ' of ' || ROUND(w.TOT, 2) ||
                   ' credits. Normal is <10% - look for many tiny queries, heavy metadata ' ||
                   'operations, or compile-heavy SQL. Diagnostics: Cost > Spend.',
               w.RATIO_PCT,
               c.RULE_ID || '|' || w.WAREHOUSE_NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            SELECT WAREHOUSE_NAME,
                   SUM(CREDITS_USED_CLOUD_SERVICES) AS CS,
                   SUM(CREDITS_USED) AS TOT,
                   SUM(CREDITS_USED_CLOUD_SERVICES) / NULLIF(SUM(CREDITS_USED), 0) * 100 AS RATIO_PCT
            FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_ID > 0
            GROUP BY 1
            HAVING SUM(CREDITS_USED) >= 1
        ) w ON c.RULE_ID = 'COST_CLOUD_SVC_RATIO'
           AND w.RATIO_PCT > c.THRESHOLD_NUM AND w.CS >= 0.5

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_CLOUD_SVC_RATIO - other rules unaffected', CURRENT_ROLE();
    END;
    -- [12] COST_STORAGE_SURGE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_STORAGE_SURGE: day-over-day database growth above threshold GB
        -- (the '600 GB in 4 days' class of surprise).
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess
               c.SEVERITY,
               g.DATABASE_NAME || ' grew ' || ROUND(g.GROWTH_GB, 1) || ' GB in a day',
               'From ' || ROUND(g.PREV_GB, 1) || ' GB to ' || ROUND(g.CUR_GB, 1) ||
                   ' GB on ' || TO_VARCHAR(g.USAGE_DATE) ||
                   '. Check for unbounded loads, missing retention, or runaway CTAS. Movers: Cost > Optimization.',
               g.GROWTH_GB,
               c.RULE_ID || '|' || g.DATABASE_NAME || '|' || TO_VARCHAR(g.USAGE_DATE)
        FROM cfg c
        JOIN (
            SELECT DATABASE_NAME, USAGE_DATE,
                   AVERAGE_DATABASE_BYTES / POWER(1024, 3) AS CUR_GB,
                   LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE)
                       / POWER(1024, 3) AS PREV_GB,
                   (AVERAGE_DATABASE_BYTES
                    - LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE))
                       / POWER(1024, 3) AS GROWTH_GB
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE DESC) = 1
        ) g ON c.RULE_ID = 'COST_STORAGE_SURGE'
           AND g.PREV_GB IS NOT NULL AND g.GROWTH_GB > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_STORAGE_SURGE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [13] COST_SERVERLESS_CREEP
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_SERVERLESS_CREEP: any serverless/managed service type doubling
        -- week-over-week (auto-clustering, MV refresh, search optimization,
        -- SPCS, serverless tasks, pipes...). Warehouses have their own daily-
        -- credit rules and AI has COST_AI_CREEP, so both are excluded here.
        -- Re-alerts weekly while creeping.
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               s.SERVICE_TYPE || ' credits up ' || ROUND(s.GROWTH_PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(s.THIS_WK, 2) || ' credits vs ' || ROUND(s.PRIOR_WK, 2) ||
                   ' prior. Serverless spend grows silently - verify the feature is intentional ' ||
                   'and priced in. Breakdown: Cost > Spend (by service).',
               s.GROWTH_PCT,
               c.RULE_ID || '|' || s.SERVICE_TYPE || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM cfg c
        JOIN (
            SELECT SERVICE_TYPE,
                   SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS THIS_WK,
                   SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS PRIOR_WK,
                   -- V067 #20: onset (prior week 0) is an infinite ratio -> emit a finite 999%
                   -- sentinel so a brand-new serverless service FIRES (mirrors COST_AI_CREEP).
                   CASE WHEN SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) = 0
                        THEN IFF(SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) > 0, 999, 0)
                        ELSE (SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) / SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) - 1) * 100 END AS GROWTH_PCT
            FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
              AND USAGE_DATE < CURRENT_DATE()   -- V066 #6: exclude today so THIS_WK/PRIOR_WK are equal 7 complete days (mirrors V065 COST_AI_CREEP)
              AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')
              AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%CORTEX%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE 'AI%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%INTELLIGENCE%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%COCO%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%COWORK%'
            GROUP BY 1
            HAVING SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) >= 5
        ) s ON c.RULE_ID = 'COST_SERVERLESS_CREEP' AND s.GROWTH_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_SERVERLESS_CREEP - other rules unaffected', CURRENT_ROLE();
    END;
    -- [14] PIPE_COPY_FAILURES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- PIPE_COPY_FAILURES: failed or partial file loads in the last 24h.
        -- Broken ingestion is the most preventable 'found out too late' class.
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(p.DB),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess
               IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY),
               p.DB || '.' || p.SCH || '.' || p.TBL || ': ' || p.FAILED_FILES || ' failed file load(s) (24h)',
               'Schema ' || p.DB || '.' || p.SCH ||
                   IFF(p.PIPE IS NOT NULL, ' | pipe ' || p.PIPE, ' | bulk COPY') ||
                   ' | sample error: ' || LEFT(COALESCE(p.SAMPLE_ERROR, 'n/a'), 300),
               p.FAILED_FILES,
               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #1: band matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires
        FROM cfg c
        JOIN (
            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,
                   MAX(PIPE_NAME) AS PIPE,
                   COUNT(*) AS FAILED_FILES,
                   MAX(FIRST_ERROR_MESSAGE) AS SAMPLE_ERROR
            FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
            WHERE LAST_LOAD_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND STATUS IN ('Load failed', 'Partially loaded')
            GROUP BY 1, 2, 3
        ) p ON c.RULE_ID = 'PIPE_COPY_FAILURES' AND p.FAILED_FILES > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PIPE_COPY_FAILURES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [15] SEC_BREAK_GLASS_USE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- SEC_BREAK_GLASS_USE: statement volume under the break-glass admin
        -- roles. Day-to-day work belongs on SNOW_SYSADMINS; a busy
        -- ACCOUNTADMIN session is either an incident or a habit to fix.
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(b.USER_NAME),
               c.SEVERITY,
               b.USER_NAME || ' ran ' || b.STMTS || ' statements as ' || b.ROLE_NAME || ' (24h)',
               'Break-glass roles are for emergencies and grants, not routine work. ' ||
                   'If this is expected, raise the threshold on the Alerts page.',
               b.STMTS,
               c.RULE_ID || '|' || b.USER_NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            SELECT USER_NAME, ROLE_NAME, COUNT(*) AS STMTS
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
            GROUP BY 1, 2
        ) b ON c.RULE_ID = 'SEC_BREAK_GLASS_USE' AND b.STMTS > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_BREAK_GLASS_USE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [17] COST_DEPT_BUDGET_PACE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_DEPT_BUDGET_PACE: department MTD spend ahead of its monthly
        -- budget pace (threshold = % over pace). Budgets live in
        -- DEPT_BUDGETS; spend = the department's warehouses (exact billing).
        SELECT c.RULE_ID, 'ALL',
               IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', c.SEVERITY),
               d.DEPARTMENT || ' is ' || ROUND(d.OVER_PCT, 0) || '% over budget pace (MTD ' ||
                   ROUND(d.MTD_USD, 0) || ' USD of ' || ROUND(d.BUDGET_USD, 0) || ')',
               'Month is ' || ROUND(d.TIME_SHARE * 100, 0) || '% elapsed. Owner lens: ' ||
                   'Cost > Chargeback (warehouses are exact; roles are allocated).',
               d.OVER_PCT,
               c.RULE_ID || '|' || d.DEPARTMENT || '|' || IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #11: band matches the HIGH severity so a MEDIUM->HIGH crossing re-fires
        FROM cfg c
        JOIN (
            SELECT DEPARTMENT, BUDGET_USD, MTD_USD, TIME_SHARE,
                   (MTD_USD / NULLIF(BUDGET_USD * TIME_SHARE, 0) - 1) * 100 AS OVER_PCT
            FROM (
                SELECT b.DEPARTMENT, b.MONTHLY_BUDGET_USD AS BUDGET_USD,
                       COALESCE(SUM(f.CREDITS_TOTAL), 0) * :credit_price AS MTD_USD,
                       DAY(CURRENT_DATE()) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE
                FROM DBA_MAINT_DB.OVERWATCH.DEPT_BUDGETS b
                LEFT JOIN DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP m
                  ON m.MAP_TYPE = 'WAREHOUSE' AND m.DEPARTMENT = b.DEPARTMENT
                LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY f
                  ON f.WAREHOUSE_NAME = UPPER(m.NAME)
                 AND f.DAY >= DATE_TRUNC('month', CURRENT_DATE())
                WHERE b.MONTHLY_BUDGET_USD > 0
                GROUP BY 1, 2
            )
        ) d ON c.RULE_ID = 'COST_DEPT_BUDGET_PACE'
           AND d.OVER_PCT > c.THRESHOLD_NUM AND d.MTD_USD >= 50
        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_DEPT_BUDGET_PACE - other rules unaffected', CURRENT_ROLE();
    END;

    -- Self-alert when any block failed: the scan reports its own degradation.
    -- [18] SEC_NEW_ADMIN_NETWORK (V043 — the r25 panel, with teeth)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               nn.USER_NAME || ' logged in from new network ' || nn.CLIENT_IP,
               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline. Auth: '
                   || COALESCE(nn.AUTH_FACTOR, '?')
                   || '. Expected after travel/VPN/host changes; anything else is the finding.',
               nn.LOGINS,
               c.RULE_ID || '|' || nn.USER_NAME || '|' || nn.CLIENT_IP
        FROM cfg c
        JOIN (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
                   MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
            JOIN (
                SELECT DISTINCT GRANTEE_NAME
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
            ) A ON A.GRANTEE_NAME = L.USER_NAME
            WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
            GROUP BY 1, 2
            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
        ) nn
          ON c.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
         AND nn.LOGINS >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_NEW_ADMIN_NETWORK - other rules unaffected', CURRENT_ROLE();
    END;
    -- [19] COST_EGRESS_SPIKE (V043 — the r25 panel, with teeth)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Egress ' || eg.GB_24H || ' GB in 24h (14d avg ' || eg.GB_AVG_14D || ' GB/day)',
               'Top destination: ' || COALESCE(eg.TOP_REGION, '(same region)')
                   || '. Source: DATA_TRANSFER_HISTORY - drill in Security -> Egress.',
               eg.GB_24H,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            SELECT ROUND(SUM(IFF(START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP()),
                                 BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 1) AS GB_24H,
                   ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(TARGET_REGION, BYTES_TRANSFERRED) AS TOP_REGION
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_24H >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_EGRESS_SPIKE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [20] SEC_NEW_EXPOSURE (V084 - CoCo Sec36: a new grant to PUBLIC widens the blast radius)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        pub AS (
            -- One row per distinct new grant to PUBLIC. A batch GRANT ON ALL ...
            -- shares one CREATED_ON, so it collapses to a single event counting
            -- its objects (N_OBJECTS) rather than flooding one alert per object.
            SELECT PRIVILEGE, GRANTED_ON, CREATED_ON,
                   COUNT(*) AS N_OBJECTS,
                   MAX(GRANTED_BY) AS GRANTED_BY,
                   MAX(NAME) AS SAMPLE_NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
            WHERE GRANTEE_NAME = 'PUBLIC'
              AND DELETED_ON IS NULL
              AND CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'New grant to PUBLIC: ' || p.PRIVILEGE || ' ON ' || p.GRANTED_ON
                   || IFF(p.N_OBJECTS > 1, ' (x' || p.N_OBJECTS || ' objects)',
                          ' ' || COALESCE(p.SAMPLE_NAME, '')),
               'A privilege granted to PUBLIC is inherited by every role in the account. '
                   || 'Granted ' || p.CREATED_ON || ' by ' || COALESCE(p.GRANTED_BY, '?')
                   || '. Source: ACCOUNT_USAGE.GRANTS_TO_ROLES - review in Security -> Access.',
               p.N_OBJECTS,
               c.RULE_ID || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)
        FROM cfg c
        JOIN pub p
          ON c.RULE_ID = 'SEC_NEW_EXPOSURE'
         AND p.N_OBJECTS >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_NEW_EXPOSURE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [21] SEC_POSTURE_METRIC (V087 - CoCo Sec35: generic, data-driven posture monitor
    --      keyed by ALERT_CONFIG.METRIC_NAME; every operator-created posture-metric rule
    --      raises here, so posture self-monitors after a finding is turned into a rule.
    --      INVARIANT: every MART_SECURITY_POSTURE_DAILY metric is a problem COUNT
    --      (higher = worse), so the comparator is a fixed VALUE >= THRESHOLD_NUM, and the
    --      app builder (posture_alert_rule_sql) only creates rules for that count
    --      vocabulary. A future lower-is-worse metric would need a comparator column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
            WHERE ENABLED AND COALESCE(METRIC_NAME, '') <> ''
        ),
        latest AS (
            -- newest posture reading per (metric, company)
            SELECT METRIC, COMPANY, VALUE, DAY
            FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
            QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC, COMPANY ORDER BY DAY DESC) = 1
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, m.COMPANY, c.SEVERITY,
               c.NAME || ': ' || m.METRIC || ' = ' || m.VALUE::INT
                   || ' (threshold >= ' || c.THRESHOLD_NUM || ')',
               'Security posture metric ' || m.METRIC || ' is ' || m.VALUE::INT || ' as of ' || m.DAY
                   || ', at or over its configured threshold ' || c.THRESHOLD_NUM
                   || '. Source: MART_SECURITY_POSTURE_DAILY - review in Security.',
               m.VALUE,
               c.RULE_ID || '|' || m.COMPANY || '|' || TO_VARCHAR(m.DAY)
        FROM cfg c
        JOIN latest m
          ON UPPER(m.METRIC) = UPPER(c.METRIC_NAME)
         AND m.VALUE >= c.THRESHOLD_NUM
         AND m.DAY >= DATEADD('day', -2, CURRENT_DATE())

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule posture-metric (generic) - other rules unaffected', CURRENT_ROLE();
    END;
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 16 alert rule block(s) failed this run',
               'APP_ERROR_LOG has the SQL errors (rule_block_failed). The other rules ' ||
                   'kept firing - that is the point of the v7 decomposition.',
               :fails,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_SCAN_DEGRADED' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    -- V067 #40: supersede the lower-severity OPEN event on escalation. V066's severity-band
    -- dedupe keys re-fire the HIGHER band as a NEW event but leave the prior lower-band event
    -- OPEN, double-counting one incident in the severity tallies + score penalties. Resolve a
    -- WARN/MED event when its CRIT/HIGH sibling (the SAME dedupe key with only the band token
    -- swapped) is also OPEN. RESOLUTION_KIND='SUPERSEDED' is excluded from the per-rule
    -- precision score (which counts only ACTIONED/NOISE), so it does not distort it. The band
    -- tokens '|WARN|'/'|MED|'/'|HIGH|'/'|EXPIRING|' occur only in banded/state keys, so this
    -- is a no-op for every other rule (V096 adds |HIGH|->|CRIT| for the SLO burn band and
    -- |EXPIRING|->|EXPIRED| for cred expiry). Wrapped so a sweep failure never breaks the scan.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
         WHERE lo.STATUS = 'OPEN'
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS hi
               WHERE hi.STATUS = 'OPEN'
                 AND hi.RULE_ID = lo.RULE_ID
                 AND hi.DEDUPE_KEY <> lo.DEDUPE_KEY
                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|'))
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'supersede_sweep_failed', :emsg, 'V067 #40 escalation supersede - other rules unaffected', CURRENT_ROLE();
    END;


    -- [auto-clear sweep] V091: resolve TODAY's still-OPEN live-window events whose
    -- scope has dropped back below the rule's CLEAR threshold (hysteresis, default
    -- 0.9 x THRESHOLD_NUM). Runs AFTER the raise arms + the supersede sweep so an
    -- escalated/superseded event is never also auto-cleared this pass. OPEN-only
    -- (manual RESOLVE wins and is never reopened; an active SNOOZE is left alone; an
    -- ACK is a human actively working it, so v1 leaves it too). The >=1h dwell plus
    -- below-CLEAR hysteresis mean an event cannot open and auto-close in one cadence.
    -- Only today's bucket (LIKE '%|<today>') is touched, so historical day-stamped
    -- exceedances are never rewritten. RESOLUTION_KIND='AUTO_CLEARED' is excluded from
    -- per-rule precision/MTTR in the app read-path exactly like SUPERSEDED. Wrapped so
    -- a sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())                    -- V096: recent window (was date-in-key); catches next-day-cleared 24h conditions
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
           AND ev.DEDUPE_KEY NOT IN (
               -- scopes STILL firing at the CLEAR threshold. Same candidate subqueries
               -- as raise arms [03]/[04]/[05], recomputed at COALESCE(CLEAR, 0.9 x RAISE).
               WITH cfg AS (
                   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                   WHERE ENABLED AND AUTO_CLEAR_ENABLED
               )
               SELECT c.RULE_ID || '|' || q.COMPANY || '|' || CURRENT_DATE() AS DEDUPE_KEY
               FROM cfg c
               JOIN (
                   SELECT COMPANY,
                          IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   GROUP BY COMPANY
                   HAVING SUM(QUERY_COUNT) >= 20
               ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT'
                  AND q.FAIL_PCT >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES'
                  AND q.QUEUED_MIN >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_SPILL_GB'
                  AND q.SPILL_GB >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'autoclear_sweep_failed', :emsg, 'V091 auto-clear sweep - other rules unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan v11 (V091: + auto-clear sweep): ' || (16 - :fails) || '/16 rule blocks ok';
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
BEGIN
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
        WHERE ENABLED AND RULE_ID = 'PERF_SLO_BREACH'
    ),
    objectives AS (
        SELECT SLO_ID, NAME, ENTITY_TYPE, ENTITY_KEY, METRIC_KEY, COMPARATOR,
               TARGET_VALUE, ERROR_BUDGET_PCT, WINDOW_DAYS, OWNER_NAME, NOTES
        FROM DBA_MAINT_DB.OVERWATCH.SLO_OBJECTIVES
        WHERE ACTIVE
    ), warehouse_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'WAREHOUSE_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.QUERIES), 0))
                 WHEN 'WAREHOUSE_P95_SEC' THEN MAX(m.P95_S)
               END AS CURRENT_VALUE,
               SUM(m.QUERIES) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'WAREHOUSE'
         AND UPPER(m.WAREHOUSE_NAME) = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('WAREHOUSE_SUCCESS_PCT', 'WAREHOUSE_P95_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), task_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'TASK_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILED, 0)) / NULLIF(SUM(m.RUNS), 0))
                 WHEN 'TASK_P95_EXEC_SEC' THEN MAX(m.P95_EXEC_SEC)
               END AS CURRENT_VALUE,
               SUM(m.RUNS) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'TASK'
         AND UPPER(m.DATABASE_NAME || '.' || m.SCHEMA_NAME || '.' || m.TASK_NAME)
             = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('TASK_SUCCESS_PCT', 'TASK_P95_EXEC_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), family_values AS (
        SELECT o.SLO_ID,
               CASE o.METRIC_KEY
                 WHEN 'QUERY_SUCCESS_PCT' THEN
                   100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.RUNS), 0))
                 WHEN 'QUERY_P95_SEC' THEN MAX(m.P95_S)
               END AS CURRENT_VALUE,
               SUM(m.RUNS) AS SAMPLE_N,
               MAX(m.DAY) AS AS_OF
        FROM objectives o
        JOIN DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY m
          ON UPPER(o.ENTITY_TYPE) = 'QUERY_FINGERPRINT'
         AND UPPER(TO_VARCHAR(m.QUERY_HASH)) = UPPER(o.ENTITY_KEY)
         AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
        WHERE o.METRIC_KEY IN ('QUERY_SUCCESS_PCT', 'QUERY_P95_SEC')
        GROUP BY o.SLO_ID, o.METRIC_KEY
    ), measured AS (
        SELECT * FROM warehouse_values
        UNION ALL SELECT * FROM task_values
        UNION ALL SELECT * FROM family_values
    ), evaluated AS (
        SELECT o.SLO_ID, o.NAME, o.ENTITY_TYPE, o.ENTITY_KEY, o.METRIC_KEY,
               o.COMPARATOR, o.TARGET_VALUE, o.WINDOW_DAYS, m.CURRENT_VALUE,
               COALESCE(m.SAMPLE_N, 0) AS SAMPLE_N, m.AS_OF,
               CASE
                 WHEN m.CURRENT_VALUE IS NULL THEN 'NO_DATA'
                 -- Don't PAGE on a statistically meaningless sample. SLO_OBJECTIVES are
                 -- operator-configured, so the target is respected and genuine low-traffic
                 -- breaches are NOT suppressed -- only the degenerate 1-4-observation case
                 -- that would read 0% off a single failure. The passive watchlist badge
                 -- still shows the raw verdict; the PAGE requires >= 5 observations.
                 WHEN COALESCE(m.SAMPLE_N, 0) < 5 THEN 'NO_DATA'
                 WHEN m.AS_OF < DATEADD('day', -2, CURRENT_DATE()) THEN 'STALE'
                 WHEN o.COMPARATOR = '>=' AND m.CURRENT_VALUE >= o.TARGET_VALUE THEN 'MET'
                 WHEN o.COMPARATOR = '<=' AND m.CURRENT_VALUE <= o.TARGET_VALUE THEN 'MET'
                 ELSE 'BREACH'
               END AS STATUS,
               IFF(o.METRIC_KEY ILIKE '%SUCCESS_PCT' AND m.CURRENT_VALUE IS NOT NULL,
                   ROUND(GREATEST(100 - m.CURRENT_VALUE, 0)
                         / NULLIF(o.ERROR_BUDGET_PCT, 0), 2), NULL) AS BURN_MULTIPLE
        FROM objectives o
        LEFT JOIN measured m ON m.SLO_ID = o.SLO_ID
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT c.RULE_ID, 'ALL',
           -- Escalate to CRITICAL when the objective is burning >= 2x its error budget;
           -- success-rate objectives only (latency/P95 have no burn) stay at the seeded HIGH.
           IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRITICAL', c.SEVERITY),
           LEFT('SLO breach: ' || e.NAME, 300),
           LEFT(e.METRIC_KEY || ' measured ' || ROUND(e.CURRENT_VALUE, 2) || ' vs target '
               || e.COMPARATOR || ' ' || e.TARGET_VALUE || ' over ' || e.WINDOW_DAYS
               || 'd on ' || e.ENTITY_TYPE || ' ' || e.ENTITY_KEY
               || ' (' || e.SAMPLE_N || ' obs'
               || IFF(e.BURN_MULTIPLE IS NOT NULL, ', burn ' || e.BURN_MULTIPLE || 'x', '')
               || ', as of ' || e.AS_OF || '). Review in Decision Studio -> SLOs.', 2000),
           e.CURRENT_VALUE,
           c.RULE_ID || '|' || e.SLO_ID || '|' || IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRIT', 'HIGH') || '|' || TO_VARCHAR(CURRENT_DATE())
    FROM cfg c, evaluated e
    WHERE e.STATUS = 'BREACH'
    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );
    RETURN 'slo breach scan v1: ' || SQLROWCOUNT || ' new SLO-breach alert(s)';
EXCEPTION
    WHEN OTHER THEN
        -- One broken scan logs and returns instead of failing the task silently; the
        -- next scheduled run heals (house pattern: SP_ALERT_SCAN's per-arm handlers).
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'SloBreachScan', 'scan_failed', :emsg, 'SP_SLO_BREACH_SCAN', CURRENT_ROLE();
        RETURN 'slo breach scan failed: ' || :emsg;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 96 AS VERSION,
       'Alert-scan dedupe/clear keys: SP_ALERT_SCAN re-derived from V091 (auto-clear sweep now matches a RAISED_AT >= 48h recency window instead of DEDUPE_KEY LIKE today, so trailing-24h conditions that clear the next day auto-resolve instead of stranding OPEN -- dwell + below-CLEAR hysteresis kept, rule scope still excludes fact-day rules; supersede sweep OR-list extended with |HIGH|->|CRIT| and |EXPIRING|->|EXPIRED|) plus SP_SLO_BREACH_SCAN re-derived from V085 (dedupe key gains a burn band token IFF(BURN_MULTIPLE>=2, CRIT, HIGH) so a same-day HIGH->CRITICAL escalation gets a distinct key and is not swallowed by the NOT EXISTS guard). Two procs, no schema change, no backfill; forward-healing on the next scan.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 96);
