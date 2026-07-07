-- V011__proactive_alerts.sql — the CoCo prevention pack.
-- Five preventive rules distilled from the metadata review: catch the
-- failure BEFORE the invoice or the outage. Supersedes SP_ALERT_SCAN (V009)
-- with a v4 that carries every earlier block unchanged. Idempotent.

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'COST_CLOUD_SVC_RATIO' AS RULE_ID, 'COST' AS FAMILY,
           'Warehouse cloud-services ratio above threshold % (24h)' AS NAME,
           TRUE AS ENABLED, 'HIGH' AS SEVERITY, 20 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
    UNION ALL
    SELECT 'COST_STORAGE_SURGE', 'COST',
           'Database grew more than threshold GB in a day', TRUE, 'MEDIUM', 100, 24
    UNION ALL
    SELECT 'COST_SERVERLESS_CREEP', 'COST',
           'Serverless service credits up threshold % week-over-week', TRUE, 'MEDIUM', 100, 168
    UNION ALL
    SELECT 'PIPE_COPY_FAILURES', 'PIPELINE',
           'Failed COPY / Snowpipe file loads in 24h (threshold = allowed failures)', TRUE, 'HIGH', 0, 24
    UNION ALL
    SELECT 'SEC_BREAK_GLASS_USE', 'SECURITY',
           'Statements run under break-glass admin roles above threshold (24h)', TRUE, 'MEDIUM', 25, 24
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

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

        -- Credential expiry: one event per credential per week until rotated
        UNION ALL
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(cr.USER_NAME),
               IFF(cr.EXPIRES_AT < CURRENT_TIMESTAMP(), 'CRITICAL', c.SEVERITY),
               cr.USER_NAME || ' ' || LOWER(cr.TYPE) || ' ''' || cr.NAME || ''' ' ||
                   IFF(cr.EXPIRES_AT < CURRENT_TIMESTAMP(),
                       'EXPIRED ' || ABS(DATEDIFF('day', cr.EXPIRES_AT, CURRENT_TIMESTAMP())) || ' day(s) ago',
                       'expires in ' || DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRES_AT) || ' day(s)'),
               'Rotate before ' || TO_VARCHAR(cr.EXPIRES_AT, 'YYYY-MM-DD') ||
                   ' to avoid auth failures for jobs and integrations using this credential.',
               DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRES_AT),
               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || DATE_TRUNC('week', CURRENT_DATE())
        FROM cfg c
        JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
          ON c.RULE_ID = 'SEC_CRED_EXPIRY'
         AND cr.DELETED_ON IS NULL
         AND cr.EXPIRES_AT IS NOT NULL
         AND cr.EXPIRES_AT <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())

        UNION ALL
        -- COST_CLOUD_SVC_RATIO: cloud-services share of a warehouse's credits
        -- (CoCo finding: WH_TRXS_TRANSFORM at ~30%; normal is <10%). Fires
        -- daily per warehouse while the ratio stays above threshold.
        SELECT c.RULE_ID,
               IFF(w.WAREHOUSE_NAME LIKE 'WH_TRXS%', 'Trexis', 'ALFA'),
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
            GROUP BY 1
            HAVING SUM(CREDITS_USED) >= 1
        ) w ON c.RULE_ID = 'COST_CLOUD_SVC_RATIO'
           AND w.RATIO_PCT > c.THRESHOLD_NUM AND w.CS >= 0.5

        UNION ALL
        -- COST_STORAGE_SURGE: day-over-day database growth above threshold GB
        -- (the '600 GB in 4 days' class of surprise).
        SELECT c.RULE_ID,
               IFF(g.DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA'),
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

        UNION ALL
        -- COST_SERVERLESS_CREEP: any serverless/managed service type doubling
        -- week-over-week (auto-clustering, MV refresh, search optimization,
        -- SPCS, serverless tasks, pipes...). Warehouses and AI have their own
        -- rules, so they are excluded here. Re-alerts weekly while creeping.
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
                   (SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0))
                    / NULLIF(SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)), 0)
                    - 1) * 100 AS GROWTH_PCT
            FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
              AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES')
            GROUP BY 1
            HAVING SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) >= 5
        ) s ON c.RULE_ID = 'COST_SERVERLESS_CREEP' AND s.GROWTH_PCT > c.THRESHOLD_NUM

        UNION ALL
        -- PIPE_COPY_FAILURES: failed or partial file loads in the last 24h.
        -- Broken ingestion is the most preventable 'found out too late' class.
        SELECT c.RULE_ID,
               IFF(p.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
               IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY),
               p.DB || '.' || p.SCH || '.' || p.TBL || ': ' || p.FAILED_FILES || ' failed file load(s) (24h)',
               'Schema ' || p.DB || '.' || p.SCH ||
                   IFF(p.PIPE IS NOT NULL, ' | pipe ' || p.PIPE, ' | bulk COPY') ||
                   ' | sample error: ' || LEFT(COALESCE(p.SAMPLE_ERROR, 'n/a'), 300),
               p.FAILED_FILES,
               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || TO_VARCHAR(CURRENT_DATE())
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

        UNION ALL
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
    )
    SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e WHERE e.DEDUPE_KEY = c.DEDUPE_KEY
    );

    RETURN 'alert scan v4 complete';
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 11 AS VERSION,
       'proactive alerts: cloud-svc ratio, storage surge, serverless creep, copy failures, break-glass' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
