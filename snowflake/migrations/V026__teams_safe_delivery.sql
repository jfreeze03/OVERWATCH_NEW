-- V026__teams_safe_delivery.sql — sender v3: JSON-safe webhook payloads.
--
-- Live finding (2026-07-08): every webhook integration splices the message
-- INSIDE a JSON string in its WEBHOOK_BODY_TEMPLATE — but the sender built
-- the message with real newlines (LISTAGG '\n' separator + the prefix) and
-- alert titles can carry double quotes. Raw newlines/quotes inside a JSON
-- string literal are invalid JSON: Slack tolerated some of it, Microsoft
-- Teams (Workflows) rejected the card outright — the hourly
-- route_send_failed errors and the "text card" failure.
--
-- v3 escapes the message for JSON embedding using CHR() codes (the V022 and
-- CALLs+ lessons: never trust backslashes to survive multiple string
-- layers). Channels render the escaped \n as a line break — Slack, Teams
-- Adaptive Cards, and PagerDuty summaries all behave.
--
-- Pair with snowflake/webhook_delivery.sql v2 for the Teams Workflows
-- integration recipe (Adaptive Card body template — the retired O365
-- connector's {"text": ...} shape does NOT work on Workflows URLs).
-- Everything else in the sender is byte-identical to V022's v2.
-- Idempotent. Apply IN ORDER after V025.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20026, 'BLOCKED: SCHEMA_VERSION < 25 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 25) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    sent_total INT DEFAULT 0;
    routes_hit INT DEFAULT 0;
    expired INT DEFAULT 0;
    message VARCHAR;
    emsg VARCHAR;
    r_route_id VARCHAR;
    r_family VARCHAR;
    r_minsev VARCHAR;
    r_integration VARCHAR;
    c1 CURSOR FOR
        SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
BEGIN
    FOR rec IN c1 DO
        r_route_id := rec.ROUTE_ID;
        r_family := rec.FAMILY;
        r_minsev := rec.MIN_SEVERITY;
        r_integration := rec.INTEGRATION_NAME;
        -- Eligible = open, young enough, matches this route, and THIS ROUTE
        -- has not delivered it yet (other routes' successes are irrelevant).
        SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\n')
               WITHIN GROUP (ORDER BY e.RAISED_AT DESC)
          INTO :message
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
        WHERE e.STATUS = 'OPEN'
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
          AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
              >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                          WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);

        -- v3: the body templates embed this string inside a JSON string
        -- literal, so it must arrive JSON-escaped. CHR() codes only —
        -- backslash first, then quote, newline, CR, tab.
        IF (:message IS NOT NULL) THEN
            message := REPLACE(:message, CHR(92), CHR(92) || CHR(92));
            message := REPLACE(:message, CHR(34), CHR(92) || CHR(34));
            message := REPLACE(:message, CHR(10), CHR(92) || 'n');
            message := REPLACE(:message, CHR(13), '');
            message := REPLACE(:message, CHR(9),  CHR(92) || 't');
        END IF;

        IF (:message IS NOT NULL AND :message != '') THEN
            BEGIN
                CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                    SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                        'OVERWATCH alerts:' || CHR(92) || 'n' || LEFT(:message, 3000)),
                    SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
                routes_hit := routes_hit + 1;

                -- Ledger rows for THIS route only (success path).
                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)
                SELECT e.EVENT_ID, :r_route_id
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                WHERE e.STATUS = 'OPEN'
                  AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
                  AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
                      >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);
                sent_total := sent_total + SQLROWCOUNT;

                -- Back-compat: NOTIFIED_AT still means "delivered somewhere
                -- at least once" (the drill, the delivery chip, and MTTA
                -- surfaces read it).
                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()
                 WHERE e.NOTIFIED_AT IS NULL
                   AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                               WHERE d.EVENT_ID = e.EVENT_ID);
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                           'route ' || :r_route_id || ' integration ' || :r_integration ||
                           ' - will retry next run; other routes unaffected',
                           CURRENT_ROLE();
            END;
        END IF;
    END FOR;

    -- Loud, not silent: open events aging past the 24h window with NO
    -- delivery anywhere get one error-log row each run they linger.
    SELECT COUNT(*) INTO :expired
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS = 'OPEN' AND e.NOTIFIED_AT IS NULL
      AND e.RAISED_AT < DATEADD('hour', -24, CURRENT_TIMESTAMP())
      AND e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP());
    IF (expired > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'NotifyWebhook', 'undelivered_expired',
               :expired || ' open event(s) aged past the 24h delivery window with no successful send',
               'check ALERT_ROUTES integrations; events remain OPEN in-app',
               CURRENT_ROLE();
    END IF;

    RETURN 'sent ' || :sent_total || ' event-route pair(s) across ' || :routes_hit ||
           ' route(s); ' || :expired || ' expired-undelivered flagged';
END;
$$;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 26 AS VERSION, 'sender v3: JSON-safe webhook payloads (Teams Workflows compatible)' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
