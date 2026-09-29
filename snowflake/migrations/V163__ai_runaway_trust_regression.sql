-- V163__ai_runaway_trust_regression.sql
--
-- Next Fifty wave 4 (#37a, #44b, #39): two new counting arms in the nightly SP_ALERT_SCAN_DAILY -- a per-user
-- AI runaway and a Trust Center regression -- and clearer text for the nightly failed-logins alert.
--
-- WHY: (#37a) one user burning AI credits was visible only to someone who opened Cost Intelligence > Chargeback
-- & AI; the account-level COST_AI_CREEP (week over week) cannot see a single user. (#44b) nothing pushed a Trust
-- Center regression: V_SECURITY_TRUST_DELTA is pull-only and compares only the two newest snapshot days. (#39)
-- the nightly SEC_FAILED_LOGINS text read the same whether or not the burst got in; since V162 the hourly
-- SEC_LOGIN_TAKEOVER pages a burst that ended in a success.
--
--   ~ SP_ALERT_SCAN_DAILY re-derived from V160 (its current definer), byte-identical except:
--     + counting arm [28] COST_AI_USER_RUNAWAY (FACT_AI_USAGE_DAILY, mart-only): a user's AI credits on one complete
--       day above THRESHOLD_NUM (2) x COCO_DAILY_CAP_CREDITS (15) AND a robust z >= AI_RUNAWAY_ROBUST_Z (3.5)
--       against the user's own active days in the 90 days before (median/MAD, the V150 engine; the scored day
--       is never its own baseline). Fewer than 5 prior active days = no baseline yet: the cap alone decides
--       (owner 2026-09-29). AI Functions rows count only with AI_RUNAWAY_INCLUDE_FUNCTIONS = TRUE and a named
--       user (inert today: the loader books them to ACCOUNT). The last 3 complete days are re-scored each
--       morning, one event per user-day. HIGH; METRIC_VALUE = the cap multiple; COMPANY = COMPANY_FOR_USER,
--       ALL when UNKNOWN.
--     + counting arm [29] SEC_TRUST_REGRESSION (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): a
--       CRITICAL or HIGH scanner's at-risk count rose by >= THRESHOLD_NUM (1) against its previous
--       snapshot day; today's and yesterday's rows are checked, so a rise after the morning scan lands the next
--       morning. A first-ever snapshot never raises. HIGH, company ALL, one event per scanner per snapshot day.
--     ~ [07] SEC_FAILED_LOGINS: TITLE and DETAIL say whether the day also had successful logins and point a
--       burst that got in to the hourly SEC_LOGIN_TAKEOVER. Predicate, severity and key unchanged (no re-fire).
--     ~ tally 12 -> 14 (self-alert, heartbeat, RETURN).
--   + ALERT_CONFIG COST_AI_USER_RUNAWAY (COST, HIGH, 2, 24h) and SEC_TRUST_REGRESSION (SECURITY, HIGH,
--     1, 24h), WHEN NOT MATCHED only; AUTO_CLEAR_ENABLED keeps its default.
--   + SETTINGS AI_RUNAWAY_ROBUST_Z = 3.5 and AI_RUNAWAY_INCLUDE_FUNCTIONS = FALSE, WHEN NOT MATCHED only.
--
-- COST: two mart-only INSERTs per nightly run (a few compile-seconds a day); COMPANY_FOR_USER runs only on a
-- raised row. No new table, view, task, proc or UDF.
-- LATENCY: daily (~07:00 Central). A runaway day the mart had not loaded yet is raised on the next morning's run;
-- a Trust Center rise after the morning scan, the next morning.
-- FIRST RUN: the next daily scan raises runaways from the last 3 complete mart days and regressions dated today
-- or yesterday; preview both with the read-only PREFLIGHT (P163.1, P163.3). Nothing runs at apply time.
-- ROLLBACK: re-run V160's SP_ALERT_SCAN_DAILY (the tally goes back to 12 and the old [07] text returns);
-- optionally disable the two rules in Alerts > Rules. The settings can stay.
-- Apply AFTER V162 (the [07] text names the hourly SEC_LOGIN_TAKEOVER). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20163, 'V163 requires V162 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 162) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The two rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered. THRESHOLD_NUM is the cap
-- multiple for the runaway (the rule editor tunes it; the robust-z bar is the AI_RUNAWAY_ROBUST_Z setting) and
-- the at-risk entities added for the regression.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_AI_USER_RUNAWAY', 'COST', 'Per-user AI runaway: one user''s AI credits on a day above THRESHOLD_NUM x the daily cap (COCO_DAILY_CAP_CREDITS) and a robust-z outlier against their own prior 90 days', TRUE, 'HIGH', 2, 24),
        ('SEC_TRUST_REGRESSION', 'SECURITY', 'Trust Center regression: a CRITICAL or HIGH scanner''s at-risk entity count rose by at least THRESHOLD_NUM since its previous snapshot day', TRUE, 'HIGH', 1, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- The runaway's two knobs (Admin > Settings). WHEN NOT MATCHED only.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('AI_RUNAWAY_ROBUST_Z', '3.5'),
        ('AI_RUNAWAY_INCLUDE_FUNCTIONS', 'FALSE')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- >>> derived:SP_ALERT_SCAN_DAILY  (from V160; + [28] COST_AI_USER_RUNAWAY + [29] SEC_TRUST_REGRESSION counting arms, [07] burst-vs-lockout wording, tally 12 -> 14, V163)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- C9: daily-cadence sibling of SP_ALERT_SCAN. The 6 rule blocks whose signal
-- is a DAILY-loaded fact (FACT_TASK_DAILY, FACT_LOGIN_DAILY, FACT_METERING_DAILY)
-- moved here and chained AFTER TASK_LOAD_DAILY, so they scan once the daily
-- facts are fresh instead of 24x/day over stale/partial rows. Same v7 per-block
-- isolation and the SAME SETTINGS read (budget + credit + AI price) as the
-- hourly scan. The self-alert uses a DISTINCT '|DAILY|' dedupe key so it never
-- collides with the hourly OPS_SCAN_DEGRADED event on the same date.
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

    -- [06] PIPE_TASK_FAILURES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, tk.COMPANY, c.SEVERITY,
               COALESCE(tk.DATABASE_NAME || '.', '') || COALESCE(tk.SCHEMA_NAME || '.', '')
                   || tk.TASK_NAME || ' failed ' || tk.FAILED || 'x on ' || tk.DAY,
               'Database: ' || COALESCE(tk.DATABASE_NAME, 'unknown') || '. '
                   || LEFT(COALESCE(tk.LAST_ERROR, 'No error text captured.'), 450),
               tk.FAILED,
               c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || COALESCE(tk.SCHEMA_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY tk
          ON c.RULE_ID = 'PIPE_TASK_FAILURES'
         AND tk.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND tk.FAILED >= c.THRESHOLD_NUM

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
                   'rule PIPE_TASK_FAILURES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [07] SEC_FAILED_LOGINS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, lg.COMPANY, c.SEVERITY,
               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY
                   || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,
                          ', ' || (lg.LOGINS - lg.FAILED_LOGINS) || ' successful', ' and no successful login'),
               IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,
                   'The same day also had successful logins. A failed burst followed within 60 minutes by a '
                   || 'success raises SEC_LOGIN_TAKEOVER from the hourly scan (CRITICAL off-hours or for an admin '
                   || 'role); this nightly count covers the whole day. ',
                   'No successful login that day: most likely a lockout or a job still sending an old secret '
                   || '(a guessing attempt that never got in looks the same). ')
                   || 'Review Security > Access > Authentication: failed-login reasons and client IPs.',
               lg.FAILED_LOGINS,
               c.RULE_ID || '|' || lg.USER_NAME || '|' || lg.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY lg
          ON c.RULE_ID = 'SEC_FAILED_LOGINS'
         AND lg.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND lg.FAILED_LOGINS >= c.THRESHOLD_NUM

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
                   'rule SEC_FAILED_LOGINS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [08] COST_BUDGET_PACE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'MTD spend $' || ROUND(m.MTD_USD, 0) || ' is ' ||
                   ROUND(m.MTD_USD / NULLIF(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 2) ||
                   'x the budget pace',
               'Budget $' || ROUND(:budget_usd, 0) || '/mo; elapsed-share allowance $' ||
                   ROUND(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0) || '.',
               m.MTD_USD,
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_BUDGET_PACE'
         AND :budget_usd > 0
         AND m.DAY_OF_MONTH > 1
         AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM

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
                   'rule COST_BUDGET_PACE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [09] COST_FORECAST_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Projected month-end $' ||
                   ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0) ||
                   ' exceeds budget $' || ROUND(:budget_usd, 0),
               'MTD $' || ROUND(m.MTD_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) ||
                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH) || ' remaining days.',
               m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH),
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_FORECAST_BREACH'
         AND :budget_usd > 0
         AND (m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH))
             > :budget_usd * c.THRESHOLD_NUM

        -- Credential expiry: one event per credential per week until rotated
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
                   'rule COST_FORECAST_BREACH - other rules unaffected', CURRENT_ROLE();
    END;
    -- [13b] COST_AI_CREEP
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_AI_CREEP: the canonical AI/Cortex bucket (SERVICE_TYPE ILIKE
        -- '%CORTEX%'/'AI%'/'%INTELLIGENCE%') from FACT_METERING_DAILY growing
        -- week-over-week, dollarized at the AI credit rate (AI_CREDIT_PRICE_USD,
        -- NOT the compute rate). COST_SERVERLESS_CREEP carves AI out; this rule
        -- owns it. Re-alerts weekly while creeping.
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'AI/Cortex spend up ' || ROUND(a.GROWTH_PCT, 0) || '% week-over-week ($' ||
                   ROUND(a.THIS_WK_USD, 0) || ' vs $' || ROUND(a.PRIOR_WK_USD, 0) || ' prior 7d)',
               'Last 7d ' || ROUND(a.THIS_WK_CR, 2) || ' AI credits ($' || ROUND(a.THIS_WK_USD, 2) ||
                   ' @ $' || ROUND(:ai_credit_price, 2) || '/cr) vs ' || ROUND(a.PRIOR_WK_CR, 2) ||
                   ' credits prior. Cortex/AI usage grows silently - confirm the workload is ' ||
                   'intentional and priced in. Breakdown: Cost > Spend (by service).',
               a.GROWTH_PCT,
               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM cfg c
        JOIN (
            SELECT SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR,
                   SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS THIS_WK_USD,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS PRIOR_WK_USD,
                   -- Onset (prior week 0) is an infinite ratio: emit a finite 999%
                   -- sentinel so a brand-new AI workload FIRES (the case budget-pace
                   -- misses) instead of GROWTH_PCT going NULL and dropping the row.
                   CASE WHEN SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) = 0
                        THEN IFF(SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) > 0, 999, 0)
                        ELSE (SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              / SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              - 1) * 100 END AS GROWTH_PCT
            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())
              AND DAY < CURRENT_DATE()   -- V065 rank3: exclude today so THIS_WK and PRIOR_WK are equal 7 complete days
              AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%')
        ) a ON c.RULE_ID = 'COST_AI_CREEP'
           AND a.THIS_WK_CR >= 5 AND a.GROWTH_PCT > c.THRESHOLD_NUM

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
                   'rule COST_AI_CREEP - other rules unaffected', CURRENT_ROLE();
    END;
    -- [16] COST_CONTRACT_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_CONTRACT_BREACH: current contract projected to exhaust within
        -- threshold days at the trailing 30 complete-day burn rate. Weekly-recurring
        -- until the contract or the burn changes; CRITICAL inside 14 days. Also fires once the contract is already EXHAUSTED (DAYS_LEFT <= 0, over-contract / on-demand overage) with a distinct EXHAUSTED band so the WARN -> CRIT -> EXHAUSTED crossings each re-fire (cost-hunt6).
        SELECT c.RULE_ID, 'ALL',
               IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY),
               IFF(p.DAYS_LEFT <= 0,
                   'Contract EXHAUSTED: ' || ROUND(p.CONSUMED - p.TOTAL, 0) ||
                       ' credits over (crossed ' || TO_VARCHAR(p.EXHAUST_DATE) || ', ' ||
                       ABS(p.DAYS_LEFT) || ' day(s) ago)',
                   'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||
                       TO_VARCHAR(p.EXHAUST_DATE) || ')'),
               'Consumed ' || ROUND(p.CONSUMED, 0) || ' of ' || ROUND(p.TOTAL, 0) ||
                   ' contracted credits; trailing 30 complete-day burn ' || ROUND(p.DAILY_BURN, 1) ||
                   ' credits/day (straight-line). Scenario planning: Cost > Contract > Renewal planner.',
               p.DAYS_LEFT,
               c.RULE_ID || '|' || IFF(p.DAYS_LEFT <= 0, 'EXH', IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN')) || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))  -- V066 #2: band matches the CRITICAL severity so a mid-week HIGH->CRITICAL crossing re-fires
        FROM cfg c
        JOIN (
            SELECT TOTAL, CONSUMED, DAILY_BURN,
                   CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
                   DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
                           CURRENT_DATE()) AS EXHAUST_DATE
            FROM (
                SELECT
                    (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())) AS CONSUMED,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
            )
        ) p ON c.RULE_ID = 'COST_CONTRACT_BREACH'
           AND p.TOTAL > 0 AND p.DAILY_BURN > 0
           AND p.DAYS_LEFT <= c.THRESHOLD_NUM

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
                   'rule COST_CONTRACT_BREACH - other rules unaffected', CURRENT_ROLE();
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
    -- [22] OPS_PIPELINE_DEGRADED (V157, Next-Fifty #10: OVERWATCH watches its own pipeline from inside BOTH
    --      task graphs. Byte-identical in SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY with shared dedupe keys, so
    --      whichever graph is still alive raises each finding once. (a) STALE: a SOURCE_FRESHNESS_STATE row
    --      past the shared name-rule cadence (DAILY/METERING in the name 30h, else 3h -- the app health strip,
    --      Admin, Control Room and NATIVE_ALERT_STALE_FACTS judge it the same way), including the scans' own
    --      heartbeat rows ALERT_SCAN_HOURLY / ALERT_SCAN_DAILY -- at most one event per source per last-load
    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged and swallowed
    --      (its task still reads SUCCEEDED) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one
    --      event per (type, source, Central day). The three OPTIONAL SP_LOAD_MARTS_V27 arm sources (tag
    --      coverage, task node, AI usage) are left to the STALE leg, so a persistently failing optional arm
    --      raises once per episode instead of every day. (c) NOTIFY: SP_NOTIFY_WEBHOOK has not acquired its
    --      sender lease for 3h while a delivery route is enabled (V064 stamps OW_SENDER_LEASE.ACQUIRED_AT at
    --      the start of every run that acquires the lease). Clocks are pinned to Central -- every NTZ stamp
    --      read here is Central wall-clock -- and each free-text value is LEFT()-bounded to its column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        fresh AS (
            SELECT SOURCE_NAME, LAST_LOAD_TS, STATUS,
                   DATEDIFF('minute', LAST_LOAD_TS,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN,
                   IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIM_H
            FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
        ),
        errs AS (
            SELECT ERROR_TYPE, SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) AS SRC,
                   TO_DATE(LOGGED_AT) AS ERR_DAY, COUNT(*) AS N,
                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG
            FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            WHERE ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                                 'cloud_svc_mart_failed', 'object_cost_load_failed')
              AND LOGGED_AT >= DATEADD('hour', -24,
                                       CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
              AND SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) NOT IN ('MART_TAG_COVERAGE_DAILY', 'MART_TASK_NODE_DAILY', 'FACT_AI_USAGE_DAILY')
            GROUP BY 1, 2, 3
        ),
        lease AS (
            SELECT ACQUIRED_AT,
                   DATEDIFF('minute', ACQUIRED_AT,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN
            FROM DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
            WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
              AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r WHERE r.ENABLED)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(f.SOURCE_NAME || IFF(f.LAST_LOAD_TS IS NULL, ' has never loaded',
                   ' is stale: ' || FLOOR(f.AGE_MIN / 60) || 'h'
                   || IFF(MOD(f.AGE_MIN, 60) > 0, ' ' || MOD(f.AGE_MIN, 60) || 'm', '')
                   || ' since its last load (limit ' || ROUND(f.LIM_H) || 'h)'), 300),
               LEFT(IFF(f.SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'ALERT_SCAN_DAILY'),
                   'Heartbeat of ' || IFF(f.SOURCE_NAME = 'ALERT_SCAN_HOURLY',
                       'SP_ALERT_SCAN (hourly graph TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_ALERT_SCAN)',
                       'SP_ALERT_SCAN_DAILY (daily graph TASK_LOAD_DAILY -> TASK_NIGHTLY_RECONCILE -> TASK_ALERT_SCAN_DAILY)')
                   || ': the scan stopped, or its heartbeat stamp failed (APP_ERROR_LOG scan_heartbeat_failed). '
                   || 'SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH -- a root auto-suspends after 10 consecutive '
                   || 'failures (V071).',
                   'Loader-owned freshness row (SOURCE_FRESHNESS_STATE, status ' || COALESCE(f.STATUS, '—')
                   || '). A stalled loader, a suspended task or a failed arm leaves this row behind while '
                   || 'TASK_HISTORY still reads SUCCEEDED. Admin > Migrations & freshness > Diagnose stale sources; '
                   || 'snowflake/loader_chain_check.sql.'), 2000),
               ROUND(f.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|STALE|' || f.SOURCE_NAME || '|' || COALESCE(TO_VARCHAR(TO_DATE(f.LAST_LOAD_TS)), 'NEVER')
        FROM cfg c
        JOIN fresh f
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (f.LAST_LOAD_TS IS NULL OR f.AGE_MIN / 60.0 > f.LIM_H)
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(x.ERROR_TYPE || ': ' || x.SRC || ' failed ' || x.N || 'x on ' || TO_VARCHAR(x.ERR_DAY), 300),
               LEFT('The loader logged this and returned normally, so its task still reads SUCCEEDED and readers '
                   || 'keep the previous fill. Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '
                   || COALESCE(LEFT(x.LAST_MSG, 600), '—') || '. Admin > Errors & telemetry (persisted error log).', 2000),
               x.N,
               c.RULE_ID || '|ERR|' || x.ERROR_TYPE || '|' || x.SRC || '|' || TO_VARCHAR(x.ERR_DAY)
        FROM cfg c
        JOIN errs x ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Alert notifier idle: SP_NOTIFY_WEBHOOK ' || IFF(l.ACQUIRED_AT IS NULL, 'has never run',
                   'has not started a run in ' || FLOOR(l.AGE_MIN / 60) || 'h'
                   || IFF(MOD(l.AGE_MIN, 60) > 0, ' ' || MOD(l.AGE_MIN, 60) || 'm', ''))
                   || ' while a delivery route is enabled', 300),
               LEFT('TASK_ALERT_NOTIFY runs after TASK_ALERT_SCAN and stamps OW_SENDER_LEASE.ACQUIRED_AT at the start '
                   || 'of every run that acquires the lease (V064). Teams/webhook delivery has stopped: SHOW TASKS '
                   || 'LIKE ''TASK_ALERT_NOTIFY'' IN SCHEMA DBA_MAINT_DB.OVERWATCH; fix the cause, then ALTER TASK '
                   || '... RESUME.', 2000),
               ROUND(l.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|NOTIFY|' || COALESCE(TO_VARCHAR(TO_DATE(l.ACQUIRED_AT)), 'NEVER')
        FROM cfg c
        JOIN lease l
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (l.ACQUIRED_AT IS NULL OR l.AGE_MIN > 180)

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
                   'rule OPS_PIPELINE_DEGRADED - other rules unaffected', CURRENT_ROLE();
    END;
    -- [24] COST_IDLE_OPPORTUNITY (V157, Next-Fifty #13: weekly idle-waste push closing the loop alert -> one
    --      ALTER -> change scan + SP_LEDGER_AUTOBOOK. DB-side twin of the Cost Intelligence > Optimization &
    --      Savings > Idle & sizing ACTIONABLE figure (insights.idle_advisor + remediation.tighten_suspend_plan):
    --      FLAGGED = >=20% idle AND >=1 idle credit; recoverable = idle minus one 60s resume tail per active
    --      metered hour; only a settings-VERIFIED timer (the newest WAREHOUSE_CONFIG_SNAPSHOT batch, <=36h
    --      old -- a warehouse missing from it, dropped or renamed, never raises) that is
    --      disabled (<=0) or above 60s. Trailing 14 COMPLETE Central days, run-rated over the days the mart
    --      covers (at least 7). One event per warehouse per ISO week (Monday, Central); the HIGH band (>= 5x
    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        win AS (
            SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
                   COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
            FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
            CROSS JOIN clk
            WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
              AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
        ),
        cov AS (
            SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win
        ),
        idle AS (
            SELECT WAREHOUSE_NAME,
                   SUM(BILLED_HOURS) AS METERED_HOURS,
                   GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
                   SUM(CREDITS_TOTAL) AS TOTAL_CREDITS,
                   SUM(IDLE_CR) AS IDLE_CREDITS
            FROM win
            GROUP BY WAREHOUSE_NAME
            HAVING SUM(CREDITS_TOTAL) > 0
        ),
        scored AS (
            SELECT i.WAREHOUSE_NAME, i.TOTAL_CREDITS, i.IDLE_CREDITS, v.COVERED_DAYS,
                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,
                   GREATEST(i.IDLE_CREDITS
                            - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0)
                              * COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS
            FROM idle i
            CROSS JOIN cov v
        ),
        newest AS (
            SELECT MAX(SNAPSHOT_AT) AS BATCH_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())
        ),
        cur AS (
            SELECT s.WAREHOUSE_NAME, s.AUTO_SUSPEND, s.SNAPSHOT_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT s
            JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1
        ),
        opp AS (
            SELECT s.WAREHOUSE_NAME, s.TOTAL_CREDITS, s.IDLE_CREDITS, s.COVERED_DAYS, s.IDLE_PCT,
                   s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
                   GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
            FROM scored s
            JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
            WHERE s.COVERED_DAYS >= 7
              AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
              AND w.AUTO_SUSPEND IS NOT NULL
              AND (w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > 60)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL'),
               IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', c.SEVERITY),
               LEFT(o.WAREHOUSE_NAME || ' idle waste ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo: AUTO_SUSPEND '
                   || IFF(o.AUTO_SUSPEND <= 0, 'disabled', o.AUTO_SUSPEND || 's') || ' -> ' || o.TARGET_SEC || 's', 300),
               LEFT('Trailing ' || o.COVERED_DAYS || ' complete day(s): ' || ROUND(o.IDLE_CREDITS, 1) || ' of '
                   || ROUND(o.TOTAL_CREDITS, 1) || ' credits burned in hours with zero queries (' || o.IDLE_PCT
                   || '% idle). After the ~60s resume tail per active hour ' || ROUND(o.RECOVERABLE_CREDITS, 1)
                   || ' credits are recoverable: ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo at $' || ROUND(:credit_price, 2)
                   || '/credit (about the ACTIONABLE figure Optimize shows with a 14-day window; this alert uses 14 '
                   || 'complete days). Timer verified by the daily SHOW WAREHOUSES snapshot at '
                   || TO_VARCHAR(o.SNAPSHOT_AT, 'YYYY-MM-DD HH24:MI') || '. Fix: '
                   || IFF(REGEXP_LIKE(o.WAREHOUSE_NAME, '^[A-Za-z_][A-Za-z0-9_$]*$'),
                          'ALTER WAREHOUSE ' || UPPER(o.WAREHOUSE_NAME) || ' SET AUTO_SUSPEND = ' || o.TARGET_SEC || ';',
                          'the name needs quoting - generate the statement in Cost Intelligence > Optimization & Savings > Remediation & ledger.')
                   || IFF(o.AUTO_SUSPEND > 0,
                          ' The next daily change scan registers the lower timer and SP_LEDGER_AUTOBOOK books and settles the measured saving (a $0 closed-loop row booked from this alert is adopted as that booking, not duplicated).',
                          ' Enabling a timer on a never-suspend warehouse is not auto-booked (SP_LEDGER_AUTOBOOK books only a decrease from a positive timer) - book it in Cost Intelligence > Optimization & Savings > Remediation & ledger.'),
                   2000),
               o.MONTHLY_USD,
               c.RULE_ID || '|' || UPPER(o.WAREHOUSE_NAME) || '|'
                   || IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', 'MED') || '|'
                   || TO_VARCHAR(DATEADD('day', 1 - DAYOFWEEKISO(k.TODAY), k.TODAY))
        FROM cfg c
        JOIN opp o
          ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY'
         AND o.MONTHLY_USD >= c.THRESHOLD_NUM
        CROSS JOIN clk k

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
                   'rule COST_IDLE_OPPORTUNITY - other rules unaffected', CURRENT_ROLE();
    END;
    -- [25] COST_SLEEP_POLLING (V160: chronic SYSTEM$WAIT sleep polling, the DB-side push of Cost > Spend > Which
    --      statement families bill the most cloud services. SP_SCAN_SLEEP_POLLING gates ITSELF: it works once per
    --      ISO week (Central) -- the first daily scan with no completed receipt and complete metering -- and
    --      otherwise returns after one small read; a not-due day counts as ok. COUNTING, like [24]: an internal
    --      mart-only rule, so a failure trips OPS_SCAN_DEGRADED and, with no receipt, the next daily scan redoes it.)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_SLEEP_POLLING - other rules unaffected', CURRENT_ROLE();
    END;
    -- [28] COST_AI_USER_RUNAWAY (V163, Next-Fifty #37a: a runaway AI user, raised the morning after. BOTH signals:
    --      the user's AI credits on one complete mart DAY exceed THRESHOLD_NUM x COCO_DAILY_CAP_CREDITS
    --      (2 x 15 by default) AND sit >= AI_RUNAWAY_ROBUST_Z (3.5) robust-z above the user's OWN active days
    --      in the 90 days before it (median/MAD 0.6745, mean-absolute-deviation 0.7979 fallback -- the V150
    --      engine, but the scored day is never its own baseline). Fewer than 5 prior active days = no baseline
    --      yet: the cap leg alone decides (owner 2026-09-29), so a brand-new heavy user is not silent.
    --      FACT_AI_USAGE_DAILY, Cortex Code (Snowsight + CLI); AI Functions rows count only when
    --      AI_RUNAWAY_INCLUDE_FUNCTIONS is TRUE AND the row names a user (the loader books them to ACCOUNT
    --      today, so the switch is inert until it attributes them). The last 3 complete days are re-scored
    --      with a per-user-day key: this scan is a sibling of TASK_LOAD_MARTS_V27_DAILY under
    --      TASK_NIGHTLY_RECONCILE (V071), so a day the mart had not loaded yet is raised on the next run, once.
    --      METRIC_VALUE = the cap multiple (THRESHOLD_NUM units); COMPANY = the user's company, ALL when it is
    --      UNKNOWN. HIGH (c.SEVERITY), never a literal CRITICAL.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        knob AS (
            SELECT COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'COCO_DAILY_CAP_CREDITS', VALUE, NULL))), 15), 0), 0), 15) AS CAP_CR,
                   COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_RUNAWAY_ROBUST_Z', VALUE, NULL))), 3.5) AS Z_MIN,
                   COALESCE(TRY_TO_BOOLEAN(MAX(IFF(KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS', VALUE, NULL))), FALSE) AS INCL_FN
            FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
        ),
        ud AS (
            SELECT f.USER_NAME, f.DAY, SUM(f.CREDITS) AS CR,
                   LISTAGG(DISTINCT f.SOURCE, '+') WITHIN GROUP (ORDER BY f.SOURCE) AS SOURCES
            FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY f
            CROSS JOIN clk k
            CROSS JOIN knob n
            WHERE f.DAY >= DATEADD('day', -93, k.TODAY) AND f.DAY < k.TODAY
              AND f.USER_NAME NOT IN ('ACCOUNT', 'UNKNOWN')
              AND (f.SOURCE <> 'Functions' OR n.INCL_FN)
            GROUP BY f.USER_NAME, f.DAY
            HAVING SUM(f.CREDITS) > 0
        ),
        cand AS (
            SELECT u.USER_NAME, u.DAY, u.CR, u.SOURCES
            FROM ud u
            CROSS JOIN clk k
            CROSS JOIN knob n
            JOIN cfg c ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
            WHERE u.DAY >= DATEADD('day', -3, k.TODAY)
              AND u.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2)
        ),
        hist AS (
            SELECT c.USER_NAME, c.DAY, h.CR AS HCR
            FROM cand c
            JOIN ud h ON h.USER_NAME = c.USER_NAME AND h.DAY < c.DAY AND h.DAY >= DATEADD('day', -90, c.DAY)
        ),
        med AS (
            SELECT USER_NAME, DAY, MEDIAN(HCR) AS MED, COUNT(*) AS N_HIST
            FROM hist GROUP BY USER_NAME, DAY
        ),
        disp AS (
            SELECT h.USER_NAME, h.DAY, m.MED, m.N_HIST,
                   MEDIAN(ABS(h.HCR - m.MED)) AS MAD, AVG(ABS(h.HCR - m.MED)) AS MEAN_AD
            FROM hist h
            JOIN med m ON m.USER_NAME = h.USER_NAME AND m.DAY = h.DAY
            GROUP BY h.USER_NAME, h.DAY, m.MED, m.N_HIST
        ),
        scored AS (
            SELECT c.USER_NAME, c.DAY, c.CR, c.SOURCES, d.MED, COALESCE(d.N_HIST, 0) AS N_HIST,
                   CASE WHEN COALESCE(d.N_HIST, 0) < 5 THEN NULL
                        WHEN d.MAD > 0 THEN 0.6745 * (c.CR - d.MED) / d.MAD
                        WHEN d.MEAN_AD > 0 THEN 0.7979 * (c.CR - d.MED) / d.MEAN_AD
                        ELSE IFF(c.CR > d.MED, 999, 0) END AS Z
            FROM cand c
            LEFT JOIN disp d ON d.USER_NAME = c.USER_NAME AND d.DAY = c.DAY
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(s.USER_NAME), 'UNKNOWN'), 'ALL'),
               c.SEVERITY,
               LEFT(s.USER_NAME || ' used ' || ROUND(s.CR, 1) || ' AI credits on ' || TO_VARCHAR(s.DAY)
                   || ' (~$' || ROUND(s.CR * :ai_credit_price)::INT || '): ' || ROUND(s.CR / n.CAP_CR, 1)
                   || 'x the ' || ROUND(n.CAP_CR, 1) || '-credit daily cap', 300),
               LEFT(IFF(s.Z IS NULL,
                        'No baseline yet: ' || s.N_HIST || ' active day(s) in the 90 days before, fewer than 5, '
                            || 'so the cap alone raised this (a new or newly active heavy user). ',
                        'Robust z ' || ROUND(s.Z, 1) || ' (bar ' || n.Z_MIN || ', AI_RUNAWAY_ROBUST_Z) against '
                            || 'this user''s own median of ' || ROUND(s.MED, 2) || ' credits/day over '
                            || s.N_HIST || ' active day(s) in the 90 days before. ')
                   || 'Cap: above ' || ROUND(n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2), 1) || ' credits (THRESHOLD_NUM '
                   || 'x COCO_DAILY_CAP_CREDITS ' || ROUND(n.CAP_CR, 1) || '). Sources: ' || s.SOURCES || '. About '
                   || ROUND(s.CR * :ai_credit_price)::INT || ' USD at ' || ROUND(:ai_credit_price, 2)
                   || ' USD/credit (AI_CREDIT_PRICE_USD). Per-user daily credits: Cost Intelligence > Chargeback '
                   || '& AI > AI users. A per-user AI quota (Snowsight, Cost Management) caps it; size one with the '
                   || 'suggested per-user quota table under Per-user AI quotas & blocks there.', 2000),
               ROUND(s.CR / n.CAP_CR, 4),
               c.RULE_ID || '|' || s.USER_NAME || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN scored s
          ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
        CROSS JOIN knob n
        WHERE s.Z IS NULL OR s.Z >= n.Z_MIN

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
                   'rule COST_AI_USER_RUNAWAY - other rules unaffected', CURRENT_ROLE();
    END;
    -- [29] SEC_TRUST_REGRESSION (V163, Next-Fifty #44b: a Trust Center scanner's at-risk count went UP. Reads
    --      SECURITY_TRUST_SNAPSHOT (hourly SP_LOAD_SECURITY_FACTS: one row per scanner per Central day, today's
    --      row rewritten each hour) and compares each of the last two snapshot days with the scanner's previous
    --      snapshot day -- the V_SECURITY_TRUST_DELTA REGRESSED test, but not that view: it compares only the
    --      newest day with the one before, so a once-a-day reader at ~07:00 would miss a rise that landed after
    --      07:00 (by the next morning both days carry the new count). CRITICAL/HIGH scanners only; a scanner's
    --      first-ever snapshot (no previous day) never raises, so enabling a package does not flood. The loader
    --      books a scanner missing from FINDINGS as 0, so its return reads as a rise (the DETAIL says so). One
    --      event per scanner per snapshot day, company ALL, HIGH (c.SEVERITY), no self-clear.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        snap AS (
            SELECT t.SCANNER_ID, t.SCANNER_NAME, UPPER(t.SEVERITY) AS SEV, t.DAY, t.SCANNED_AT,
                   t.TOTAL_AT_RISK_COUNT AS CUR_N,
                   LAG(t.TOTAL_AT_RISK_COUNT) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_N,
                   LAG(t.DAY) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_DAY
            FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT t
            CROSS JOIN clk k
            WHERE t.DAY >= DATEADD('day', -30, k.TODAY)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Trust Center ' || s.SEV || ' scanner regressed: ' || COALESCE(s.SCANNER_NAME, s.SCANNER_ID)
                   || ' at-risk entities ' || s.PRIOR_N || ' -> ' || s.CUR_N || ' on ' || TO_VARCHAR(s.DAY), 300),
               LEFT('Scanner ' || s.SCANNER_ID || ': ' || s.CUR_N || ' entities at risk in the ' || TO_VARCHAR(s.DAY)
                   || ' snapshot (latest scan ' || COALESCE(TO_VARCHAR(s.SCANNED_AT, 'YYYY-MM-DD HH24:MI'), 'unknown')
                   || '), up from ' || s.PRIOR_N || ' on ' || TO_VARCHAR(s.PRIOR_DAY) || '. '
                   || IFF(s.PRIOR_N = 0,
                          'The previous day read 0: the loader books a scanner missing from FINDINGS as 0, so this '
                              || 'may be the scanner returning rather than new risk. ',
                          '')
                   || 'Security > Trust Center for the scanner delta; the entities and the suggested fix are in '
                   || 'Snowsight > Monitoring > Trust Center > Findings. Fix there, then re-scan.', 2000),
               s.CUR_N - s.PRIOR_N,
               c.RULE_ID || '|' || s.SCANNER_ID || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN snap s
          ON c.RULE_ID = 'SEC_TRUST_REGRESSION'
         AND s.PRIOR_N IS NOT NULL
         AND s.CUR_N > 0
         AND s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)
         AND s.SEV IN ('CRITICAL', 'HIGH')
        CROSS JOIN clk k
        WHERE s.DAY >= DATEADD('day', -1, k.TODAY)

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
                   'rule SEC_TRUST_REGRESSION - other rules unaffected', CURRENT_ROLE();
    END;
    -- [17] PIPE_REF_GAP  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads customer staging/XLAT tables that
    -- are SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        g AS (
            SELECT CHECK_NAME,
                   COUNT(*) AS N,
                   LISTAGG(NEW_CODE, ', ') WITHIN GROUP (ORDER BY NEW_CODE) AS CODES
            FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS
            GROUP BY CHECK_NAME
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               g.CHECK_NAME || ': ' || g.N || ' new source code(s) missing from XLAT',
               'The nightly load will fail on the missing code(s). Add the XLAT translation '
                   || 'row(s) before the next cycle. New codes: ' || LEFT(g.CODES, 1700),
               g.N,
               c.RULE_ID || '|' || g.CHECK_NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN g ON c.RULE_ID = 'PIPE_REF_GAP' AND g.N >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (unlike the six core arms). The ref-gap
            -- scan depends on SELECT grants on EXTERNAL customer tables applied out-of-band, so a
            -- grant gap (or any ref-gap-specific error) is recorded in APP_ERROR_LOG but must not
            -- trip the OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals the
            -- moment grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'ref_gap_scan_failed', :emsg,
                   'rule PIPE_REF_GAP - optional external add-on; needs SELECT on staging/XLAT tables', CURRENT_ROLE();
    END;
    -- [18] DQ_RECON_ERROR  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads the customer RECON_MTRC_ERROR table
    -- SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        r AS (
            SELECT COUNT(*) AS METRICS,
                   COALESCE(SUM(N), 0) AS ERRORS,
                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS
            FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               r.ERRORS || ' reconciliation error(s) across ' || r.METRICS || ' metric(s)',
               'Source and target layers did not reconcile in the last '
                   || COALESCE(c.WINDOW_HOURS, 48) || 'h -- investigate before the numbers are '
                   || 'trusted downstream. Metric(s): ' || LEFT(r.TOP_METRICS, 1700),
               r.ERRORS,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN r ON c.RULE_ID = 'DQ_RECON_ERROR' AND r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (like the ref-gap arm). The recon scan depends
            -- on a SELECT grant on the EXTERNAL RECON_MTRC_ERROR table applied out-of-band, so a grant
            -- gap (or any recon-specific error) is recorded in APP_ERROR_LOG but must not trip the
            -- OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals when grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'recon_scan_failed', :emsg,
                   'rule DQ_RECON_ERROR - optional external add-on; needs SELECT on RECON_MTRC_ERROR', CURRENT_ROLE();
    END;
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 14 daily alert rule block(s) failed this run',
               'APP_ERROR_LOG has the SQL errors (rule_block_failed). The other rules '
                   || 'kept firing - that is the point of the v7 decomposition.',
               :fails,
               c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_SCAN_DEGRADED' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    -- [hb] scan heartbeat (V157, Next-Fifty #10b): stamp this scan's own SOURCE_FRESHNESS_STATE row LAST --
    -- after every arm and sweep -- so a row older than its cadence means the scan stopped (or this stamp
    -- keeps failing: APP_ERROR_LOG scan_heartbeat_failed). The hourly graph's [22] arm, the app freshness
    -- boards and NATIVE_ALERT_STALE_FACTS read it with the shared name rule (ALERT_SCAN_HOURLY -> 3h,
    -- ALERT_SCAN_DAILY -> 30h). LAST_LOAD_TS is Central wall-clock like every loader stamp. Cheapest shape:
    -- ONE point UPDATE of this scan's own row; the INSERT runs only when it matched no row (the first run,
    -- or after the row was deleted), so the stamp self-heals. Isolated; does NOT touch :fails.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
           SET LAST_LOAD_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
               ROW_COUNT = (14 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)'
         WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_DAILY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (14 - :fails), 1, 'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_DAILY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout wording): ' || (14 - :fails) || '/14 rule blocks ok (daily)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 163 AS VERSION,
       'Next Fifty wave 4 (#37a, #44b, #39). SP_ALERT_SCAN_DAILY re-derived from V160, byte-identical except: + counting arm [28] COST_AI_USER_RUNAWAY (FACT_AI_USAGE_DAILY, mart-only): the AI credits of one user on a complete day above THRESHOLD_NUM (2) x COCO_DAILY_CAP_CREDITS AND a robust z at least AI_RUNAWAY_ROBUST_Z (3.5) against the active days of that user in the 90 days before (median/MAD; fewer than 5 prior active days = no baseline, the cap alone decides); Cortex Code only unless AI_RUNAWAY_INCLUDE_FUNCTIONS and a named user; the last 3 complete days re-scored, one event per user-day; HIGH; METRIC_VALUE = the cap multiple; COMPANY = COMPANY_FOR_USER, ALL when UNKNOWN. + counting arm [29] SEC_TRUST_REGRESSION (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): the at-risk count of a CRITICAL or HIGH scanner rose by at least THRESHOLD_NUM (1) against its previous snapshot day, today and yesterday checked; a first snapshot never raises; HIGH, company ALL. [07] SEC_FAILED_LOGINS TITLE and DETAIL now say whether the day had successful logins and point a burst that got in to the hourly SEC_LOGIN_TAKEOVER (predicate, severity and key unchanged). Tally 12 -> 14. Seeds the two rules and the settings AI_RUNAWAY_ROBUST_Z (3.5) and AI_RUNAWAY_INCLUDE_FUNCTIONS (FALSE), WHEN NOT MATCHED only. No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 163);
