-- V007__automation.sql — budget alerting, notification readiness, AI digest,
-- and savings auto-verification. Idempotent. Supersedes SP_ALERT_SCAN (V004).

-- ---------------------------------------------------------------------------
-- Budget-aware alert rules (fire only when MONTHLY_BUDGET_USD > 0 in SETTINGS)
-- ---------------------------------------------------------------------------
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_BUDGET_PACE',     'COST', 'MTD spend ahead of budget pace (ratio threshold)', TRUE, 'HIGH',     1.10, 24),
        ('COST_FORECAST_BREACH', 'COST', 'Projected month-end spend exceeds monthly budget', TRUE, 'CRITICAL', 1.00, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- Webhook delivery bookkeeping (see snowflake/webhook_delivery.sql, opt-in)
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS NOTIFIED_AT TIMESTAMP_NTZ;

-- ---------------------------------------------------------------------------
-- SP_ALERT_SCAN v2: all V004 rules + budget pace/forecast rules
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    budget_usd FLOAT;
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :budget_usd, :credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
    ),
    mtd AS (
        SELECT
            SUM(CREDITS_BILLED) * :credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            SUM(CREDITS_BILLED) * :credit_price
                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
    ),
    candidates AS (
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

        UNION ALL
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

        UNION ALL
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

        UNION ALL
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

        UNION ALL
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

        UNION ALL
        SELECT c.RULE_ID, tk.COMPANY, c.SEVERITY,
               COALESCE(tk.DATABASE_NAME || '.', '') || COALESCE(tk.SCHEMA_NAME || '.', '')
                   || tk.TASK_NAME || ' failed ' || tk.FAILED || 'x on ' || tk.DAY,
               'Database: ' || COALESCE(tk.DATABASE_NAME, 'unknown') || '. '
                   || LEFT(COALESCE(tk.LAST_ERROR, 'No error text captured.'), 450),
               tk.FAILED,
               c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY tk
          ON c.RULE_ID = 'PIPE_TASK_FAILURES'
         AND tk.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND tk.FAILED >= c.THRESHOLD_NUM

        UNION ALL
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

        -- Budget pace: MTD spend vs time-elapsed share of the monthly budget
        UNION ALL
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

        -- Forecast breach: MTD + run-rate x remaining days exceeds budget
        UNION ALL
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
    )
    SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e WHERE e.DEDUPE_KEY = c.DEDUPE_KEY
    );

    RETURN 'alert scan v2 complete';
END;
$$;

-- ---------------------------------------------------------------------------
-- Daily AI digest (grounded in the exec board; model from SETTINGS)
-- ---------------------------------------------------------------------------
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (
    DIGEST_DATE DATE          NOT NULL,
    COMPANY     VARCHAR(40)   NOT NULL DEFAULT 'ALL',
    MODEL       VARCHAR(80),
    BODY        VARCHAR(8000),
    CREATED_AT  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    model VARCHAR;
    facts VARCHAR;
    alerts VARCHAR;
    prompt VARCHAR;
    body VARCHAR;
BEGIN
    SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
      INTO :model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    SELECT COALESCE(LISTAGG(METRIC || '=' || COALESCE(VALUE_USD, VALUE)::VARCHAR, '; ')
           WITHIN GROUP (ORDER BY SORT_ORDER), 'no board rows')
      INTO :facts
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';

    SELECT 'open_critical=' || SUM(IFF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; open_high=' || SUM(IFF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; raised_24h=' || SUM(IFF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()), 1, 0))
      INTO :alerts
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;

    prompt := LEFT(
        'You are a senior Snowflake DBA writing the morning digest for ALFA/Trexis leadership. '
        || 'Use ONLY these 7-day platform facts and alert counts - never invent numbers. '
        || 'Write 3 short paragraphs: (1) platform health and spend in plain language, '
        || '(2) what needs attention today and why, (3) one recommended focus. No preamble. '
        || 'FACTS: ' || COALESCE(:facts, 'none') || '. ALERTS: ' || COALESCE(:alerts, 'none') || '.',
        6000);

    BEGIN
        body := SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt);
    EXCEPTION
        WHEN OTHER THEN
            body := 'Digest unavailable: Cortex COMPLETE failed for model ' || :model
                    || '. Check SNOWFLAKE.CORTEX_USER grant and regional model availability.';
    END;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();
    INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (DIGEST_DATE, COMPANY, MODEL, BODY)
    VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000));

    RETURN 'digest written';
END;
$$;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_DAILY_DIGEST
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 20 7 * * * America/Chicago'
    COMMENT = 'Grounded Cortex digest after the morning loads. Uses Cortex credits (~1 call/day).'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST();

-- ---------------------------------------------------------------------------
-- Savings auto-verification: re-measure idle spend behind ESTIMATED items
-- ---------------------------------------------------------------------------
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SAVINGS_VERIFICATION_RUNS (
    RUN_AT              TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    ITEM_ID             VARCHAR(80)   NOT NULL,
    WAREHOUSE_NAME      VARCHAR(200),
    BASELINE_EST_USD    NUMBER(18,2),
    MEASURED_IDLE_USD_30D NUMBER(18,2),
    PROPOSED_VERIFIED_USD NUMBER(18,2)
);

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
               TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', '')) AS WAREHOUSE_NAME,
               ESTIMATED_USD
        FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        WHERE STATE = 'ESTIMATED' AND DESCRIPTION LIKE 'Auto-suspend tune: %'
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

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_VERIFY_SAVINGS
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 40 7 1 * * America/Chicago'
    COMMENT = 'Monthly re-measurement of ESTIMATED auto-suspend savings; operator approves VERIFIED.'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS();

GRANT SELECT ON TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST TO ROLE OVERWATCH_MONITOR;
GRANT SELECT ON TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_VERIFICATION_RUNS TO ROLE OVERWATCH_MONITOR;

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_DAILY_DIGEST RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_VERIFY_SAVINGS RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 7 AS VERSION, 'automation: budget alerts, scan v2, AI digest, savings verification, NOTIFIED_AT' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);

SELECT 'V007 applied' AS STATUS;
