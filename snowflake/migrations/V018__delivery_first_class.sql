-- V018__delivery_first_class.sql — alert delivery joins the numbered chain.
--
-- Three review rounds read the opt-in webhook file as 'detection without
-- delivery'. The wiring always existed; this migration makes it impossible
-- to misread: the notify task is created here (chained AFTER the scan),
-- auto-RESUMED when the integration already exists, and the morning digest
-- gains a guarded send. The only step that can never ship in git remains
-- creating the NOTIFICATION INTEGRATION itself (it holds the webhook
-- secret) — see snowflake/webhook_delivery.sql for that one-time setup.

-- 0) Version guard.
EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20018, 'BLOCKED: SCHEMA_VERSION < 17 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 17) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok: found version ' || v;
END;
$$;

-- 1) Notify task in-chain (idempotent; sender proc ships in V012).
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY
    WAREHOUSE = WH_ALFA_OVERWATCH
    AFTER DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK();

-- 2) Auto-resume delivery IF the integration exists; no-op otherwise.
EXECUTE IMMEDIATE $$
DECLARE
    n INT DEFAULT 0;
BEGIN
    SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH_WEBHOOK';
    SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (n > 0) THEN
        ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
        RETURN 'delivery LIVE (integration found, notify task resumed)';
    END IF;
    RETURN 'integration missing - notify task left suspended (see webhook_delivery.sql)';
END;
$$;

-- 3) Digest v2: same narrative, now also delivered through the default route.
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


    -- v2: deliver the narrative through the default webhook route (guarded —
    -- without the integration the digest still writes, just doesn't send).
    BEGIN
        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                LEFT(:body, 3000)),
            SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_WEBHOOK'));
    EXCEPTION
        WHEN OTHER THEN
            NULL;  -- integration absent/disabled: in-app digest remains the surface
    END;

    RETURN 'digest written + delivery attempted';
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 18 AS VERSION,
       'delivery first-class: notify task in-chain, guarded auto-resume, digest webhook send' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
