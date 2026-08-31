-- V108__cost_contract_breach_fires_when_exhausted.sql
--
-- COST_CONTRACT_BREACH false all-clear. The [16] arm of SP_ALERT_SCAN_DAILY fired only when
-- DAYS_LEFT BETWEEN 0 AND THRESHOLD_NUM, so once CONSUMED crossed CONTRACT_CREDITS the projected
-- DAYS_LEFT went negative and the alert went permanently silent -- exactly in the over-contract
-- state that bills on-demand overage at premium rates. No other scan proc covered the gap.
--
-- Re-derives SP_ALERT_SCAN_DAILY from V079 with the guard relaxed to DAYS_LEFT <= THRESHOLD_NUM so
-- the over-contract state (DAYS_LEFT <= 0) also fires, with a distinct 'Contract EXHAUSTED: N credits
-- over' CRITICAL title/metric and an EXHAUSTED dedupe band so WARN -> CRIT -> EXHAUSTED crossings each
-- re-fire. The p.TOTAL > 0 AND p.DAILY_BURN > 0 gates (unconfigured contract / no burn) and every
-- other arm are byte-identical. No schema change; owner applies in Snowsight after V107 and the next
-- daily SP_ALERT_SCAN_DAILY evaluates the corrected guard. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20108, 'V108 requires V107 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 107) THEN
        RAISE not_ready;
    END IF;
END;
$$;

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
               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY,
               'Investigate credential stuffing / lockouts.',
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
                   ROUND(m.MTD_USD / NULLIF(:budget_usd * m.DAY_OF_MONTH / m.DAYS_IN_MONTH, 0), 2) ||
                   'x the budget pace',
               'Budget $' || ROUND(:budget_usd, 0) || '/mo; elapsed-share allowance $' ||
                   ROUND(:budget_usd * m.DAY_OF_MONTH / m.DAYS_IN_MONTH, 0) || '.',
               m.MTD_USD,
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_BUDGET_PACE'
         AND :budget_usd > 0
         AND m.MTD_USD > :budget_usd * m.DAY_OF_MONTH / m.DAYS_IN_MONTH * c.THRESHOLD_NUM

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
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 6 daily alert rule block(s) failed this run',
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

    RETURN 'alert scan daily v1 (task/login/budget-pace/forecast/AI-creep/contract): ' || (6 - :fails) || '/6 rule blocks ok (daily)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 108 AS VERSION,
       'COST_CONTRACT_BREACH fires when exhausted: SP_ALERT_SCAN_DAILY re-derived from V079 so the [16] arm guard is DAYS_LEFT <= THRESHOLD_NUM instead of BETWEEN 0 AND THRESHOLD_NUM. Once CONSUMED crossed CONTRACT_CREDITS the projected DAYS_LEFT went negative and the alert went permanently silent in the over-contract (on-demand overage) state; it now fires there too with a distinct Contract EXHAUSTED: N credits over CRITICAL title/metric and an EXHAUSTED dedupe band so WARN -> CRIT -> EXHAUSTED crossings each re-fire. The p.TOTAL > 0 AND p.DAILY_BURN > 0 gates and every other arm are byte-identical. Proc only, no schema change; forward-healing on the next daily SP_ALERT_SCAN_DAILY.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 108);
