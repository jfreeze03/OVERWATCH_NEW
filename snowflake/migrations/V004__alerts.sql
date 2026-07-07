-- V004__alerts.sql — alert rules, events, lifecycle audit, hourly scan.
-- Consolidated to 3 tables (the old app had 9). Idempotent.

CREATE TABLE IF NOT EXISTS OVERWATCH.CORE.ALERT_CONFIG (
    RULE_ID       VARCHAR(60)   NOT NULL PRIMARY KEY,
    FAMILY        VARCHAR(40)   NOT NULL,   -- COST | PERFORMANCE | PIPELINE | SECURITY
    NAME          VARCHAR(200)  NOT NULL,
    ENABLED       BOOLEAN       NOT NULL DEFAULT TRUE,
    SEVERITY      VARCHAR(20)   NOT NULL DEFAULT 'HIGH',
    THRESHOLD_NUM NUMBER(18,4),
    WINDOW_HOURS  NUMBER(6,0)   NOT NULL DEFAULT 24,
    OWNER         VARCHAR(200)  NOT NULL DEFAULT 'DBA',
    CHANNEL       VARCHAR(60)   NOT NULL DEFAULT 'IN_APP',
    UPDATED_AT    TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

MERGE INTO OVERWATCH.CORE.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_DAILY_CREDITS',   'COST',        'Account daily credits above threshold',            TRUE, 'HIGH',     100,  24),
        ('COST_WH_DAILY_CREDITS','COST',        'Single warehouse daily credits above threshold',   TRUE, 'MEDIUM',    25,  24),
        ('PERF_QUERY_FAIL_PCT',  'PERFORMANCE', 'Query failure rate above threshold (pct)',         TRUE, 'HIGH',       5,  24),
        ('PERF_QUEUED_MINUTES',  'PERFORMANCE', 'Warehouse queueing above threshold (minutes)',     TRUE, 'MEDIUM',    30,  24),
        ('PERF_SPILL_GB',        'PERFORMANCE', 'Remote spill above threshold (GB)',                TRUE, 'MEDIUM',    10,  24),
        ('PIPE_TASK_FAILURES',   'PIPELINE',    'Task failures above threshold (count)',            TRUE, 'HIGH',       3,  24),
        ('SEC_FAILED_LOGINS',    'SECURITY',    'Failed logins for one user above threshold',       TRUE, 'HIGH',      10,  24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

CREATE TABLE IF NOT EXISTS OVERWATCH.CORE.ALERT_EVENTS (
    EVENT_ID     VARCHAR(80)   NOT NULL DEFAULT UUID_STRING() PRIMARY KEY,
    RULE_ID      VARCHAR(60)   NOT NULL,
    RAISED_AT    TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    COMPANY      VARCHAR(40)   NOT NULL DEFAULT 'ALL',
    SEVERITY     VARCHAR(20)   NOT NULL,
    TITLE        VARCHAR(300)  NOT NULL,
    DETAIL       VARCHAR(2000),
    METRIC_VALUE NUMBER(18,4),
    STATUS       VARCHAR(20)   NOT NULL DEFAULT 'OPEN',  -- OPEN | ACK | RESOLVED
    ACK_BY       VARCHAR(200),
    ACK_AT       TIMESTAMP_NTZ,
    RESOLVED_AT  TIMESTAMP_NTZ,
    DEDUPE_KEY   VARCHAR(300)
);

-- Lifecycle + remediation audit: who did what, with what proof.
CREATE TABLE IF NOT EXISTS OVERWATCH.CORE.ALERT_AUDIT (
    AUDIT_ID    VARCHAR(80)   NOT NULL DEFAULT UUID_STRING() PRIMARY KEY,
    EVENT_ID    VARCHAR(80)   NOT NULL,
    ACTION      VARCHAR(40)   NOT NULL,   -- ACK | RESOLVE | REMEDIATE | NOTE
    ACTED_BY    VARCHAR(200)  NOT NULL DEFAULT CURRENT_USER(),
    ACTED_AT    TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    NOTE        VARCHAR(2000),
    PROOF_SQL   VARCHAR(4000)
);

-- ---------------------------------------------------------------------------
-- Hourly scan over facts. Deterministic thresholds live here; statistical
-- anomaly detection stays in-app (labeled) by design.
-- Dedupe: one event per rule+scope+day.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE OVERWATCH.CORE.SP_ALERT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    INSERT INTO OVERWATCH.CORE.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM OVERWATCH.CORE.ALERT_CONFIG WHERE ENABLED
    ),
    candidates AS (
        -- COST_DAILY_CREDITS: account credits yesterday+today
        SELECT c.RULE_ID, 'ALL' AS COMPANY, c.SEVERITY,
               'Account daily credits ' || ROUND(f.CREDITS, 1) || ' >= ' || c.THRESHOLD_NUM AS TITLE,
               'Warehouse metering total for ' || f.DAY AS DETAIL,
               f.CREDITS AS METRIC_VALUE,
               c.RULE_ID || '|ALL|' || f.DAY AS DEDUPE_KEY
        FROM cfg c
        JOIN (
            SELECT DAY, SUM(CREDITS_TOTAL) AS CREDITS
            FROM OVERWATCH.MART.FACT_WAREHOUSE_DAILY
            WHERE DAY >= DATEADD('day', -1, CURRENT_DATE())
            GROUP BY DAY
        ) f ON c.RULE_ID = 'COST_DAILY_CREDITS' AND f.CREDITS >= c.THRESHOLD_NUM

        UNION ALL
        -- COST_WH_DAILY_CREDITS: single warehouse
        SELECT c.RULE_ID, f.COMPANY, c.SEVERITY,
               f.WAREHOUSE_NAME || ' used ' || ROUND(f.CREDITS_TOTAL, 1) || ' credits on ' || f.DAY,
               'Per-warehouse daily metering.',
               f.CREDITS_TOTAL,
               c.RULE_ID || '|' || f.WAREHOUSE_NAME || '|' || f.DAY
        FROM cfg c
        JOIN OVERWATCH.MART.FACT_WAREHOUSE_DAILY f
          ON c.RULE_ID = 'COST_WH_DAILY_CREDITS'
         AND f.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND f.CREDITS_TOTAL >= c.THRESHOLD_NUM

        UNION ALL
        -- PERF_QUERY_FAIL_PCT over the rule window
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               'Query failure rate ' || ROUND(q.FAIL_PCT, 1) || '% >= ' || c.THRESHOLD_NUM || '%',
               q.FAILED || ' of ' || q.TOTAL || ' queries failed in last ' || c.WINDOW_HOURS || 'h.',
               q.FAIL_PCT,
               c.RULE_ID || '|' || q.COMPANY || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, SUM(FAILED_COUNT) AS FAILED, SUM(QUERY_COUNT) AS TOTAL,
                   IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
            FROM OVERWATCH.MART.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY COMPANY
            HAVING SUM(QUERY_COUNT) >= 20
        ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT' AND q.FAIL_PCT >= c.THRESHOLD_NUM

        UNION ALL
        -- PERF_QUEUED_MINUTES per warehouse
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' queued ' || ROUND(q.QUEUED_MIN, 1) || ' min in 24h',
               'Queued overload + provisioning time.',
               q.QUEUED_MIN,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
            FROM OVERWATCH.MART.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES' AND q.QUEUED_MIN >= c.THRESHOLD_NUM

        UNION ALL
        -- PERF_SPILL_GB per warehouse
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' spilled ' || ROUND(q.SPILL_GB, 1) || ' GB remote in 24h',
               'Remote spill indicates undersized memory for the workload.',
               q.SPILL_GB,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
            FROM OVERWATCH.MART.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_SPILL_GB' AND q.SPILL_GB >= c.THRESHOLD_NUM

        UNION ALL
        -- PIPE_TASK_FAILURES per task
        SELECT c.RULE_ID, tk.COMPANY, c.SEVERITY,
               tk.TASK_NAME || ' failed ' || tk.FAILED || 'x on ' || tk.DAY,
               LEFT(COALESCE(tk.LAST_ERROR, 'No error text captured.'), 500),
               tk.FAILED,
               c.RULE_ID || '|' || tk.TASK_NAME || '|' || tk.DAY
        FROM cfg c
        JOIN OVERWATCH.MART.FACT_TASK_DAILY tk
          ON c.RULE_ID = 'PIPE_TASK_FAILURES'
         AND tk.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND tk.FAILED >= c.THRESHOLD_NUM

        UNION ALL
        -- SEC_FAILED_LOGINS per user
        SELECT c.RULE_ID, lg.COMPANY, c.SEVERITY,
               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY,
               'Investigate credential stuffing / lockouts.',
               lg.FAILED_LOGINS,
               c.RULE_ID || '|' || lg.USER_NAME || '|' || lg.DAY
        FROM cfg c
        JOIN OVERWATCH.MART.FACT_LOGIN_DAILY lg
          ON c.RULE_ID = 'SEC_FAILED_LOGINS'
         AND lg.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND lg.FAILED_LOGINS >= c.THRESHOLD_NUM
    )
    SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1 FROM OVERWATCH.CORE.ALERT_EVENTS e WHERE e.DEDUPE_KEY = c.DEDUPE_KEY
    );

    RETURN 'alert scan complete';
END;
$$;

CREATE TASK IF NOT EXISTS OVERWATCH.CORE.TASK_ALERT_SCAN
    WAREHOUSE = OVERWATCH_WH
    AFTER OVERWATCH.MART.TASK_LOAD_HOURLY
AS
    CALL OVERWATCH.CORE.SP_ALERT_SCAN();

-- Resume the whole chain now that all children exist (children first).
ALTER TASK IF EXISTS OVERWATCH.MART.TASK_REFRESH_EXEC_BOARD RESUME;
ALTER TASK IF EXISTS OVERWATCH.CORE.TASK_ALERT_SCAN RESUME;
ALTER TASK IF EXISTS OVERWATCH.MART.TASK_LOAD_HOURLY RESUME;
ALTER TASK IF EXISTS OVERWATCH.MART.TASK_LOAD_DAILY RESUME;

MERGE INTO OVERWATCH.CORE.SCHEMA_VERSION t
USING (SELECT 4 AS VERSION, 'alerts: config/events/audit, hourly scan, task chain resumed' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);

SELECT 'V004 applied' AS STATUS;
