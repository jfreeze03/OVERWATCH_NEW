-- V016__closing_loops.sql — pre-explained anomalies, department budgets,
-- org rollup + volume-drop rules, self-monitoring canary, usage analytics.
-- Scan v6 and sweep v3 carry their full prior bodies (regression-tested).

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DEPT_BUDGETS (
    DEPARTMENT VARCHAR(120) NOT NULL PRIMARY KEY,
    MONTHLY_BUDGET_USD NUMBER(18,2) NOT NULL,
    UPDATED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY VARCHAR(200) NOT NULL DEFAULT CURRENT_USER()
);

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.APP_USAGE (
    AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    USER_NAME VARCHAR(200) NOT NULL DEFAULT CURRENT_USER(),
    PAGE VARCHAR(80),
    SECTION VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS (
    RUN_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CHECK_NAME VARCHAR(200) NOT NULL,
    STATUS VARCHAR(10) NOT NULL,
    ERROR VARCHAR(500)
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'COST_DEPT_BUDGET_PACE' AS RULE_ID, 'COST' AS FAMILY,
           'Department MTD spend over budget pace by threshold %' AS NAME,
           TRUE AS ENABLED, 'MEDIUM' AS SEVERITY, 10 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
    UNION ALL
    SELECT 'COST_ORG_ACCOUNT_CREEP', 'COST',
           'Org account currency spend up threshold % week-over-week', TRUE, 'MEDIUM', 50, 168
    UNION ALL
    SELECT 'PIPE_VOLUME_DROP', 'PIPELINE',
           'Table rows-added down threshold % vs prior-7d average', TRUE, 'HIGH', 50, 24
    UNION ALL
    SELECT 'OPS_CANARY_FAIL', 'PLATFORM',
           'Weekly source-canary found failing dependency views', TRUE, 'HIGH', 0, 168
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

        UNION ALL
        -- COST_CONTRACT_BREACH: current contract projected to exhaust within
        -- threshold days at the trailing 30-day burn rate. Weekly-recurring
        -- until the contract or the burn changes; CRITICAL inside 14 days.
        SELECT c.RULE_ID, 'ALL',
               IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY),
               'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||
                   TO_VARCHAR(p.EXHAUST_DATE) || ')',
               'Consumed ' || ROUND(p.CONSUMED, 0) || ' of ' || ROUND(p.TOTAL, 0) ||
                   ' contracted credits; trailing 30d burn ' || ROUND(p.DAILY_BURN, 1) ||
                   ' credits/day (straight-line). Scenario planning: Cost > Contract > Renewal planner.',
               p.DAYS_LEFT,
               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
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
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / 30
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())) AS DAILY_BURN
            )
        ) p ON c.RULE_ID = 'COST_CONTRACT_BREACH'
           AND p.TOTAL > 0 AND p.DAILY_BURN > 0
           AND p.DAYS_LEFT BETWEEN 0 AND c.THRESHOLD_NUM

        UNION ALL
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
               c.RULE_ID || '|' || d.DEPARTMENT || '|' || TO_VARCHAR(CURRENT_DATE())
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
    )
    SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e WHERE e.DEDUPE_KEY = c.DEDUPE_KEY
    );

    RETURN 'alert scan v6 complete';
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    zthr FLOAT;
    ai_model VARCHAR;
    ev_id VARCHAR;
    ev_title VARCHAR;
    day_s VARCHAR;
    series_s VARCHAR;
    wh_s VARCHAR;
    evidence VARCHAR;
    ai_prompt VARCHAR;
    ai_resp VARCHAR;
    c_new CURSOR FOR
        SELECT EVENT_ID, TITLE, DEDUPE_KEY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        WHERE RULE_ID = 'COST_ANOMALY_SWEEP'
          AND RAISED_AT >= DATEADD('minute', -15, CURRENT_TIMESTAMP())
          AND DETAIL NOT LIKE '%| AI:%'
        LIMIT 5;
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'WAREHOUSE ' || WAREHOUSE_NAME AS SERIES, COMPANY, DAY,
               SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
        UNION ALL
        SELECT 'SERVICE ' || SERVICE_TYPE, 'ALL', DAY, SUM(CREDITS_BILLED)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD,
               ABS(0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        WHERE s.DAY = (SELECT MAX(DAY) FROM series)
    )
    SELECT 'COST_ANOMALY_SWEEP', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || ' spent ' || ROUND(l.CREDITS, 1) || ' credits on ' ||
               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.ROBUST_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 1) || ' credits/day over the prior 28d. ' ||
               'Robust z-score ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. Investigate: Cost > Spend / Attribution for that day.',
           l.ROBUST_Z,
           'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.MAD > 0 AND l.ROBUST_Z >= :zthr AND l.CREDITS >= 1
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    -- Dynamic-table refresh failures (guarded: accounts without the view
    -- keep the sweep's cost half working).
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID,
               IFF(d.DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA'),
               IFF(d.FAILURES >= 5, 'CRITICAL', c.SEVERITY),
               d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   ': ' || d.FAILURES || ' dynamic-table refresh failure(s) (24h)',
               'Schema ' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                   ' | last state ' || d.LAST_STATE ||
                   '. Downstream tables are serving stale data until this refreshes.',
               d.FAILURES,
               c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DATABASE_NAME, SCHEMA_NAME, NAME,
                   COUNT_IF(STATE = 'FAILED') AS FAILURES,
                   MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
            WHERE REFRESH_END_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY 1, 2, 3
            HAVING COUNT_IF(STATE = 'FAILED') > 0
        ) d ON c.RULE_ID = 'PIPE_DT_FAILURES' AND c.ENABLED AND d.FAILURES > c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                  '.' || d.NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dynamic_tables_unavailable', 'DT refresh view not readable',
                   'cost anomaly sweep unaffected', CURRENT_ROLE();
    END;


    -- PERF_FINGERPRINT_DRIFT (Mondays): p95 per query family, last 7d vs the
    -- prior 28d — catches regressions that arrive WITHOUT a DDL change
    -- (data growth, clustering decay, plan changes). Complements the
    -- change-anchored V010 tracker.
    IF (DAYOFWEEKISO(CURRENT_DATE()) = 1) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL',
               IFF(f.P95_RECENT_S >= f.P95_BASE_S * 3, 'HIGH', c.SEVERITY),
               'Query family p95 ' || f.P95_BASE_S || 's -> ' || f.P95_RECENT_S || 's: ' ||
                   LEFT(f.SAMPLE_TEXT, 60),
               'Hash ' || f.QUERY_PARAMETERIZED_HASH || ' | runs ' || f.RUNS_BASE || ' -> ' ||
                   f.RUNS_RECENT || ' | 7d vs prior 28d, no change event required. ' ||
                   'Drill: Operations > Queries (heaviest queries).',
               ROUND(100 * (f.P95_RECENT_S / NULLIF(f.P95_BASE_S, 0) - 1), 1),
               c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                   TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
                   COUNT_IF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_RECENT,
                   COUNT_IF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_BASE,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_RECENT_S,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_BASE_S
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_PARAMETERIZED_HASH IS NOT NULL
            GROUP BY 1
            HAVING RUNS_RECENT >= 20 AND RUNS_BASE >= 20
        ) f ON c.RULE_ID = 'PERF_FINGERPRINT_DRIFT' AND c.ENABLED
           AND f.P95_BASE_S > 0
           AND f.P95_RECENT_S > f.P95_BASE_S * (1 + c.THRESHOLD_NUM / 100)
           AND f.P95_RECENT_S >= 10
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    END IF;


    -- COST_ORG_ACCOUNT_CREEP (guarded): any org account's currency spend up
    -- threshold% week-over-week — a sibling account can't surprise you.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               o.ACCOUNT_NAME || ' org spend up ' || ROUND(o.PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(o.CUR, 0) || ' vs prior ' || ROUND(o.PRV, 0) || ' ' || o.CCY ||
                   '. Breakdown: Admin > Org spend.',
               o.PCT,
               c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT ACCOUNT_NAME, CCY, CUR, PRV, (CUR / NULLIF(PRV, 0) - 1) * 100 AS PCT
            FROM (
                SELECT ACCOUNT_NAME, MAX(CURRENCY) AS CCY,
                       SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS CUR,
                       SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS PRV
                FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
                WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
                GROUP BY 1
            )
        ) o ON c.RULE_ID = 'COST_ORG_ACCOUNT_CREEP' AND c.ENABLED
           AND o.PCT > c.THRESHOLD_NUM AND o.CUR >= 100
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'org_usage_unavailable', 'ORGANIZATION_USAGE not readable',
                   'org creep check skipped', CURRENT_ROLE();
    END;

    -- PIPE_VOLUME_DROP (guarded): yesterday's rows-added collapsed vs the
    -- prior-7-day average on tables that normally move real volume.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID,
               IFF(v.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
               c.SEVERITY,
               v.DB || '.' || v.SCH || '.' || v.TBL || ' volume down ' || ROUND(v.DROP_PCT, 0) ||
                   '% (' || v.Y_ROWS || ' rows vs ~' || ROUND(v.AVG_ROWS, 0) || '/day)',
               'Yesterday vs prior-7d average. Upstream feed, failed COPY, or intentional? ' ||
                   'Check Operations > Pipeline SLA.',
               v.DROP_PCT,
               c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL || '|' ||
                   TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DB, SCH, TBL, Y_ROWS, AVG_ROWS,
                   (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 AS DROP_PCT
            FROM (
                SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
                       SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) AS Y_ROWS,
                       SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS
                FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
                WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
                  AND d.START_TIME < CURRENT_DATE()
                GROUP BY 1, 2, 3
                HAVING AVG_ROWS >= 1000
            )
        ) v ON c.RULE_ID = 'PIPE_VOLUME_DROP' AND c.ENABLED
           AND v.DROP_PCT > c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL ||
                  '|' || TO_VARCHAR(CURRENT_DATE())
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'volume-drop check skipped', CURRENT_ROLE();
    END;

    -- Pre-explain fresh anomalies (guarded): grounded Cortex hypothesis is
    -- appended to the event DETAIL so the webhook message arrives explained.
    -- Capped at 5 events/run to bound AI spend.
    BEGIN
        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
          INTO :ai_model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
        FOR e IN c_new DO
            ev_id := e.EVENT_ID;
            ev_title := e.TITLE;
            series_s := SPLIT_PART(e.DEDUPE_KEY, '|', 2);
            day_s := SPLIT_PART(e.DEDUPE_KEY, '|', 3);
            wh_s := IFF(series_s LIKE 'WAREHOUSE %', LTRIM(SUBSTR(series_s, 10)), '');
            SELECT LISTAGG(SAMPLE_TEXT || ' day=' || H_DAY || 'h prior_avg=' || H_PRI || 'h', '; ')
              INTO :evidence
            FROM (
                SELECT ANY_VALUE(LEFT(QUERY_TEXT, 60)) AS SAMPLE_TEXT,
                       ROUND(SUM(IFF(DATE(START_TIME) = TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS H_DAY,
                       ROUND(SUM(IFF(DATE(START_TIME) < TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS H_PRI
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -7, TO_DATE(:day_s))
                  AND START_TIME < DATEADD('day', 1, TO_DATE(:day_s))
                  AND (:wh_s = '' OR WAREHOUSE_NAME = :wh_s)
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY QUERY_PARAMETERIZED_HASH
                ORDER BY H_DAY DESC
                LIMIT 10
            );
            ai_prompt := 'You are a Snowflake cost analyst. ALERT: ' || :ev_title ||
                         '. EVIDENCE (top query families, elapsed hours on the day vs prior-7d avg): ' ||
                         COALESCE(:evidence, 'none') ||
                         '. Using ONLY this evidence, name the 1-2 most likely drivers with their ' ||
                         'numbers, or say evidence is inconclusive. Max 80 words. Never invent data.';
            ai_resp := SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt);
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
               SET DETAIL = LEFT(COALESCE(DETAIL, '') || ' | AI: ' || :ai_resp, 2000)
             WHERE EVENT_ID = :ev_id;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cortex_pre_explain_unavailable',
                   'CORTEX.COMPLETE failed - events remain unexplained (drawer AI still works)',
                   'model or grant issue', CURRENT_ROLE();
    END;

    RETURN 'anomaly sweep v3 complete';
END;
$$;

-- Self-monitoring: weekly 1-row probe of every source view the app depends
-- on. Results persist; failures raise OPS_CANARY_FAIL. The Admin canary
-- remains the deep per-builder check; this one runs without humans.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    checks ARRAY DEFAULT [
        'SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS',
        'SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.USERS',
        'SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS',
        'SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES',
        'SNOWFLAKE.ACCOUNT_USAGE.ROLES',
        'SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS',
        'SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS',
        'SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY',
        'DBA_MAINT_DB.OVERWATCH.SETTINGS',
        'DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY',
        'DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD',
        'DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG'
    ];
    cname VARCHAR;
    emsg VARCHAR;
    fails INT DEFAULT 0;
    i INT;
BEGIN
    FOR i IN 0 TO ARRAY_SIZE(:checks) - 1 DO
        cname := GET(:checks, i)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'SELECT 1 FROM ' || :cname || ' LIMIT 1';
            INSERT INTO DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS (CHECK_NAME, STATUS)
            SELECT :cname, 'PASS';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                fails := fails + 1;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS (CHECK_NAME, STATUS, ERROR)
                SELECT :cname, 'FAIL', LEFT(:emsg, 500);
        END;
    END FOR;

    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' source dependency check(s) failing',
               'CANARY_RESULTS has the errors. Likely ACCOUNT_USAGE column drift after a ' ||
                   'Snowflake release, or a revoked grant. Run the Admin canary for the ' ||
                   'per-builder picture.',
               :fails,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_CANARY_FAIL' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS
     WHERE RUN_AT < DATEADD('day', -180, CURRENT_TIMESTAMP());

    RETURN 'sentinel: ' || :fails || ' failure(s)';
END;
$$;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CANARY_SENTINEL
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 30 5 * * 1 America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_CANARY_SENTINEL RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 16 AS VERSION,
       'closing loops: pre-explained anomalies, dept budgets, org creep, volume drop, canary sentinel, app usage' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
