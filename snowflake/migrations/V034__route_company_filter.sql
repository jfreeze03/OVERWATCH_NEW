-- V034__route_company_filter.sql — per-route company scoping (sender v4).
--
-- Owner decision 2026-07-10: Teams delivery is ALFA-only for now. Routes
-- gain COMPANY_FILTER ('ALL' default): a route carries its company's events
-- plus account-level (COMPANY='ALL') ones — the open_alert_events
-- convention, applied to delivery. Existing routes flip to 'ALFA' below;
-- set a route back to 'ALL' (or add a Trexis route) when Trexis wants
-- delivery. In-app visibility is untouched — this scopes SENDING only.
--
-- The expiry watchdog learns the same policy: an event no enabled route
-- will ever carry is out of delivery scope, not "undelivered" — otherwise
-- every Trexis event would spam undelivered_expired hourly forever.
--
-- Sender derived VERBATIM from V026's v3 with five enumerated edits
-- (tests/test_live_round8.py holds the revert-equality lock).
--
-- Idempotent. Apply IN ORDER after V033. No new grants needed.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20034, 'BLOCKED: SCHEMA_VERSION < 33 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 33) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
    ADD COLUMN IF NOT EXISTS COMPANY_FILTER VARCHAR(40) DEFAULT 'ALL';

-- Owner decision: every existing route (the Teams set) goes ALFA-only.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
   SET COMPANY_FILTER = 'ALFA'
 WHERE COALESCE(COMPANY_FILTER, 'ALL') = 'ALL';

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
    r_compfilter VARCHAR;   -- v4: per-route company scope (owner: Teams = ALFA-only for now)
    c1 CURSOR FOR
        SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME,
               COALESCE(r.COMPANY_FILTER, 'ALL') AS COMPANY_FILTER
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
BEGIN
    FOR rec IN c1 DO
        r_route_id := rec.ROUTE_ID;
        r_family := rec.FAMILY;
        r_minsev := rec.MIN_SEVERITY;
        r_integration := rec.INTEGRATION_NAME;
        r_compfilter := rec.COMPANY_FILTER;
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
          AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')
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
                  AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')
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
      AND e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
      -- v4: an event NO route will ever carry (company-filtered out) is out
      -- of delivery scope by policy, not undelivered — no hourly noise.
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2
                  WHERE r2.ENABLED
                    AND (COALESCE(r2.COMPANY_FILTER, 'ALL') = 'ALL'
                         OR e.COMPANY = r2.COMPANY_FILTER
                         OR UPPER(e.COMPANY) = 'ALL'));
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

-- Owner ask 2026-07-10: retire SEC_BREAK_GLASS_USE entirely — V025 muted
-- it (ENABLED=FALSE); admins know what they are doing, so the rule row goes
-- and any lingering open events close as EXPECTED. The Security activity
-- panel (evidence, no alert) stays. History in ALERT_EVENTS is preserved.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
       RESOLVED_AT = CURRENT_TIMESTAMP()
 WHERE RULE_ID = 'SEC_BREAK_GLASS_USE' AND STATUS IN ('OPEN', 'ACK');

DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
 WHERE RULE_ID = 'SEC_BREAK_GLASS_USE';

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 34 AS VERSION,
       'route company filter (sender v4, ALFA-only) + SEC_BREAK_GLASS_USE retired' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
