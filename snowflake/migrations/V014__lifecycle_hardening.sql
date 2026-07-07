-- V014__lifecycle_hardening.sql — predictive contract breach, fingerprint
-- drift detection, and fact retention.
--
-- 1. COST_CONTRACT_BREACH: SP_ALERT_SCAN v5 (full v4 body carried) projects
--    contract exhaustion from settings + trailing 30d burn; fires weekly
--    inside the threshold horizon, CRITICAL inside 14 days.
-- 2. PERF_FINGERPRINT_DRIFT: SP_ANOMALY_SWEEP v2 (full body carried) adds a
--    Monday scan of p95 per QUERY_PARAMETERIZED_HASH, 7d vs prior 28d —
--    regressions with NO change event (complements V010).
-- 3. SP_PURGE_FACTS + monthly task: settings-driven retention so fact
--    tables stop growing forever (floors prevent foot-guns). Idempotent.

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'COST_CONTRACT_BREACH' AS RULE_ID, 'COST' AS FAMILY,
           'Contract projected to exhaust within threshold days' AS NAME,
           TRUE AS ENABLED, 'HIGH' AS SEVERITY, 30 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
    UNION ALL
    SELECT 'PERF_FINGERPRINT_DRIFT', 'PERFORMANCE',
           'Query-family p95 up threshold % (7d vs prior 28d, weekly)',
           TRUE, 'MEDIUM', 100, 168
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('FACT_RETENTION_DAYS_HOURLY', '400'),
        ('FACT_RETENTION_DAYS_DAILY',  '800'),
        ('ERROR_LOG_RETENTION_DAYS',   '180')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

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
    )
    SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e WHERE e.DEDUPE_KEY = c.DEDUPE_KEY
    );

    RETURN 'alert scan v5 complete';
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

    RETURN 'anomaly sweep v2 complete';
END;
$$;

-- Retention: facts are TRANSIENT rebuildable telemetry; keeping them forever
-- just buys storage bills and slower marts. Floors stop a typo from wiping
-- recent history.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    hourly_days FLOAT;
    daily_days FLOAT;
    err_days FLOAT;
    total INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_HOURLY', VALUE, NULL))), 400),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'FACT_RETENTION_DAYS_DAILY', VALUE, NULL))), 800),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'ERROR_LOG_RETENTION_DAYS', VALUE, NULL))), 180)
      INTO :hourly_days, :daily_days, :err_days
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    hourly_days := GREATEST(hourly_days, 90);
    daily_days := GREATEST(daily_days, 180);
    err_days := GREATEST(err_days, 30);

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS < DATEADD('day', -1 * :hourly_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
     WHERE LOGGED_AT < DATEADD('day', -1 * :err_days, CURRENT_TIMESTAMP());
    total := total + SQLROWCOUNT;

    RETURN 'purged ' || :total || ' row(s)';
END;
$$;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PURGE_FACTS
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 20 5 1 * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PURGE_FACTS RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 14 AS VERSION,
       'lifecycle hardening: contract breach projection, fingerprint drift, fact retention' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
