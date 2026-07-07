-- V012__routing_anomaly_remediation.sql — alert routing, anomaly sweep,
-- remediation log, dynamic-table failure alerting.
--
-- 1. ALERT_ROUTES: family/severity -> named webhook integration, so finops
--    alerts hit #finops and security alerts hit #security without triage.
--    SP_NOTIFY_WEBHOOK v2 walks the routes; unrouted events fall back to
--    the default OVERWATCH_WEBHOOK integration.
-- 2. SP_ANOMALY_SWEEP (daily task): robust MAD z-score over every warehouse
--    and service daily-credit series in the facts; breaches raise
--    COST_ANOMALY_SWEEP events for the latest complete day.
-- 3. REMEDIATION_LOG: every generated fix the app executes (or a user
--    copies), with statement text, outcome, and savings-ledger linkage.
-- 4. PIPE_DT_FAILURES: dynamic-table refresh failures (guarded block —
--    accounts without the view keep everything else working). Idempotent.

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (
    ROUTE_ID     VARCHAR(80)  NOT NULL DEFAULT UUID_STRING() PRIMARY KEY,
    FAMILY       VARCHAR(40)  NOT NULL,             -- COST | PERFORMANCE | PIPELINE | SECURITY | ALL
    MIN_SEVERITY VARCHAR(20)  NOT NULL DEFAULT 'HIGH',  -- CRITICAL | HIGH | MEDIUM | LOW
    INTEGRATION_NAME VARCHAR(200) NOT NULL,         -- notification integration to send through
    ENABLED      BOOLEAN      NOT NULL DEFAULT TRUE,
    CREATED_BY   VARCHAR(200) NOT NULL DEFAULT CURRENT_USER(),
    CREATED_AT   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- Default route: everything HIGH+ through the original integration. Editing
-- or disabling this row is how you go fully custom.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES t
USING (SELECT 'ALL' AS FAMILY, 'HIGH' AS MIN_SEVERITY, 'OVERWATCH_WEBHOOK' AS INTEGRATION_NAME) s
ON t.FAMILY = s.FAMILY AND t.INTEGRATION_NAME = s.INTEGRATION_NAME
WHEN NOT MATCHED THEN INSERT (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)
     VALUES (s.FAMILY, s.MIN_SEVERITY, s.INTEGRATION_NAME);

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG (
    REMEDIATION_ID VARCHAR(80)  NOT NULL DEFAULT UUID_STRING() PRIMARY KEY,
    FINDING_TYPE   VARCHAR(60)  NOT NULL,   -- IDLE_WAREHOUSE | AUTO_SUSPEND | RESIZE | RETENTION | SCHEDULE | OTHER
    TARGET_OBJECT  VARCHAR(600) NOT NULL,
    STATEMENT_SQL  VARCHAR(4000) NOT NULL,
    EST_MONTHLY_SAVINGS_USD NUMBER(18,2),
    EXECUTED_BY    VARCHAR(200) NOT NULL DEFAULT CURRENT_USER(),
    EXECUTED_AT    TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    STATUS         VARCHAR(20)  NOT NULL DEFAULT 'EXECUTED',  -- EXECUTED | COPIED | FAILED
    RESULT_NOTE    VARCHAR(2000)
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'COST_ANOMALY_SWEEP' AS RULE_ID, 'COST' AS FAMILY,
           'Daily credits anomalous vs trailing 28d (threshold = robust z)' AS NAME,
           TRUE AS ENABLED, 'MEDIUM' AS SEVERITY, 3.5 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
    UNION ALL
    SELECT 'PIPE_DT_FAILURES', 'PIPELINE',
           'Dynamic table refresh failures in 24h (threshold = allowed failures)',
           TRUE, 'HIGH', 0, 24
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- Route-aware webhook sender. Falls back cleanly: no enabled routes match ->
-- default integration; a route's integration missing -> that route logs one
-- APP_ERROR_LOG row and the remaining routes still send.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    sent_total INT DEFAULT 0;
    routes_hit INT DEFAULT 0;
    message VARCHAR;
    emsg VARCHAR;
    r_family VARCHAR;
    r_minsev VARCHAR;
    r_integration VARCHAR;
    c1 CURSOR FOR
        SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.FAMILY;
BEGIN
    FOR rec IN c1 DO
        r_family := rec.FAMILY;
        r_minsev := rec.MIN_SEVERITY;
        r_integration := rec.INTEGRATION_NAME;
        SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\n')
               WITHIN GROUP (ORDER BY e.RAISED_AT DESC)
          INTO :message
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
        WHERE e.STATUS = 'OPEN'
          AND e.NOTIFIED_AT IS NULL
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
          AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END;

        IF (:message IS NOT NULL AND :message != '') THEN
            BEGIN
                CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                    SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                        'OVERWATCH alerts:\n' || LEFT(:message, 3000)),
                    SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
                routes_hit := routes_hit + 1;

                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()
                  FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
                 WHERE c.RULE_ID = e.RULE_ID
                   AND e.STATUS = 'OPEN' AND e.NOTIFIED_AT IS NULL
                   AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
                   AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END;
                sent_total := sent_total + SQLROWCOUNT;
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                           'integration ' || :r_integration || ' - other routes unaffected',
                           CURRENT_ROLE();
            END;
        END IF;
    END FOR;

    RETURN 'notified ' || :sent_total || ' event(s) across ' || :routes_hit || ' route(s)';
END;
$$;

-- Daily robust-z sweep over every warehouse + service daily-credit series.
-- Iglewicz-Hoaglin: |0.6745 * (x - median) / MAD| — the same statistic the
-- in-app anomaly flags use, so chart flags and alerts agree.
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

    RETURN 'anomaly sweep complete';
END;
$$;

CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 40 6 * * * America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();

ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP RESUME;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 12 AS VERSION,
       'routing + anomaly sweep + remediation log + dynamic-table failure alerts' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
