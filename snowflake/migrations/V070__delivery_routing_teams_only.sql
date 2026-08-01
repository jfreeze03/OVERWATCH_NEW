-- V070__delivery_routing_teams_only.sql
--
-- Three delivery defects in already-applied migrations (V012, V018), fixed forward. This
-- account is Teams-only: it uses OVERWATCH_WEBHOOK_TEAMS; the Slack-era OVERWATCH_WEBHOOK
-- integration does NOT exist (snowflake/webhook_delivery.sql enumerates these same three
-- items as the outstanding Teams-only hazards). Idempotent; apply AFTER V069.
--
--   #23 (P1) SP_DAILY_DIGEST hardcoded the retired Slack integration and swallowed the
--       send with WHEN OTHER THEN NULL, so the morning digest has NEVER been delivered on a
--       Teams-only account while the proc still returned 'delivery attempted'. Re-derived
--       from its latest def (V018) via outputs/gen_v070.py + count-asserted needle edits to
--       walk the ENABLED ALERT_ROUTES rows and send through EACH row's INTEGRATION_NAME
--       (SP_NOTIFY_WEBHOOK's per-route idiom, V034), LEDGERING each per-route outcome to
--       APP_ERROR_LOG ('digest_send_failed' + the integration in CONTEXT) instead of
--       discarding it. The in-app digest write is untouched (it stands even if every send
--       fails); the return is a machine-readable 'digest written; sent N/M routes'.
--   #25 (P1) an idempotent block DISABLES any enabled ALERT_ROUTES row whose integration is
--       absent from SHOW NOTIFICATION INTEGRATIONS (so the dead V012 default Slack route
--       stops writing a route_send_failed every cycle that buries real errors). Reversible.
--   #24 (P2) an idempotent block RESUMEs TASK_ALERT_NOTIFY when any enabled route names an
--       integration SHOW NOTIFICATION INTEGRATIONS confirms exists (a healthy Teams
--       integration now satisfies the gate). Never suspends.
--
-- The only proc re-defined is SP_DAILY_DIGEST; #25/#24 are idempotent data/DDL blocks - no
-- NEW objects. Byte-verified by tests/test_v070_delivery_routing.py. No owner smoke test is
-- required to apply, but delivery is runtime-only, so confirm a real card arrives on the
-- Teams route once (see DEPLOYMENT.md "V070 verify").

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20070, 'V070 requires V069 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 69) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- #11: additive digest-eligibility flag so only digest routes receive the digest.
-- V070 #11 (defect): SP_DAILY_DIGEST walked EVERY enabled ALERT_ROUTES row, ignoring a
-- route's family/company/severity/purpose, so a future PagerDuty/tactical route would get
-- executive digest prose. Add an idempotent, additive flag so the digest can target only
-- digest-eligible routes; existing routes DEFAULT TRUE, so today's behavior is unchanged.
-- ADD COLUMN IF NOT EXISTS keeps this a safe re-runnable no-op. ALERT_ROUTES is not in the
-- exec board, so this is lockstep-neutral.
ALTER TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
    ADD COLUMN IF NOT EXISTS DELIVER_DIGEST BOOLEAN NOT NULL DEFAULT TRUE;

-- >>> derived:SP_DAILY_DIGEST  (route-walk delivery + per-route ledger; no hardcoded integration)
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
    routes_total INT DEFAULT 0;   -- V070 #23: M = enabled routes walked
    routes_sent INT DEFAULT 0;    -- V070 #23: N = routes the digest reached
    emsg VARCHAR;
    r_route_id VARCHAR;
    r_integration VARCHAR;
    c_routes CURSOR FOR
        SELECT r.ROUTE_ID, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED AND r.DELIVER_DIGEST   -- V070 #11: only digest-eligible routes
        ORDER BY r.ROUTE_ID;
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

    -- V070 #39: replace today's digest atomically. Under autocommit a crash between
    -- the DELETE and the INSERT would leave today's digest BLANK; an explicit transaction
    -- makes it all-or-nothing (on any error ROLLBACK restores the prior row and re-raise).
    BEGIN TRANSACTION;
    BEGIN
        DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (DIGEST_DATE, COMPANY, MODEL, BODY)
        VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000));
        COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            RAISE;
    END;


    -- V070 #23: deliver the digest through EVERY enabled ALERT_ROUTES row's own
    -- integration (SP_NOTIFY_WEBHOOK's per-route walk idiom, V034), not the retired
    -- hardcoded Slack integration that does not exist on a Teams-only account. Each
    -- route's outcome is LEDGERED: a failed send logs one 'digest_send_failed' row to
    -- APP_ERROR_LOG naming the integration, replacing the old blanket WHEN OTHER THEN
    -- NULL that hid a never-delivered digest behind a 'delivery attempted' string. The
    -- in-app digest was already written above and stands regardless of any send.
    FOR rec IN c_routes DO
        r_route_id := rec.ROUTE_ID;
        r_integration := rec.INTEGRATION_NAME;
        routes_total := routes_total + 1;
        BEGIN
            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                    'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                    LEFT(:body, 3000)),
                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
            routes_sent := routes_sent + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'DailyDigest', 'digest_send_failed', :emsg,
                       'route ' || :r_route_id || ' integration ' || :r_integration ||
                       ' - digest still written in-app; other routes unaffected',
                       CURRENT_ROLE();
        END;
    END FOR;

    -- V070 #12: without this a fully-failed run is silent — only per-route failures were
    -- logged and the proc still returned a bland 'sent 0/M' string. Log one loud
    -- 'digest_undelivered' row when routes were eligible but NONE received the digest, so
    -- an all-failed run is observable, and mark the zero-success case in the return string.
    IF (routes_total > 0 AND routes_sent = 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'DailyDigest', 'digest_undelivered',
               'digest written in-app but delivered to 0 of ' || :routes_total || ' enabled route(s)',
               'every enabled digest route failed - see digest_send_failed rows for per-route detail',
               CURRENT_ROLE();
    END IF;

    RETURN 'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes'
           || IFF(:routes_total > 0 AND :routes_sent = 0, ' [UNDELIVERED]', '');
END;
$$;

-- #25: retire any enabled route whose integration is absent from the account.
-- V070 #25 (defect): V012 seeded an ENABLED default ALERT_ROUTES row through the
-- Slack-era integration OVERWATCH_WEBHOOK, which does NOT exist on this Teams-only
-- account. Every SP_NOTIFY_WEBHOOK cycle then logs a route_send_failed for it, burying
-- the real Teams delivery errors. DISABLE any enabled route whose INTEGRATION_NAME is
-- absent from the account's live notification integrations. SHOW NOTIFICATION
-- INTEGRATIONS + RESULT_SCAN needs a query-id, so this runs inside EXECUTE IMMEDIATE.
-- Reversible: set ENABLED = TRUE (or create the integration) to bring a route back.
-- Idempotent: a second run finds nothing enabled-but-absent to disable.
EXECUTE IMMEDIATE
$$
BEGIN
    SHOW NOTIFICATION INTEGRATIONS;
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
       SET ENABLED = FALSE
     WHERE ENABLED
       AND UPPER(INTEGRATION_NAME) NOT IN (
           SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    RETURN 'ok: disabled any enabled route whose integration is absent from the account';
END;
$$;

-- #24: bring delivery live when an enabled route resolves to a real integration.
-- V070 #24 (defect): V018's auto-resume gate RESUMEd TASK_ALERT_NOTIFY only when the
-- Slack-era OVERWATCH_WEBHOOK integration existed, so a healthy Teams integration never
-- satisfied it and delivery stayed suspended. RESUME the task when ANY enabled
-- ALERT_ROUTES row names an integration that SHOW NOTIFICATION INTEGRATIONS confirms
-- exists (evaluated AFTER the #25 disable above, so only live routes count). This never
-- suspends - a running task is left running. Idempotent: RESUME on a running task is a
-- no-op; if no enabled route resolves to a live integration, the task is left as-is.
EXECUTE IMMEDIATE
$$
DECLARE
    n INT DEFAULT 0;
BEGIN
    SHOW NOTIFICATION INTEGRATIONS;
    SELECT COUNT(*) INTO :n
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
    WHERE r.ENABLED
      AND UPPER(r.INTEGRATION_NAME) IN (
          SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    IF (n > 0) THEN
        ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
        RETURN 'delivery LIVE (' || :n || ' enabled route(s) resolve to a live integration; notify task resumed)';
    END IF;
    RETURN 'no enabled route resolves to a live integration - notify task left as-is';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 70 AS VERSION,
       'Teams-only delivery routing (V012/V018 forward fix): SP_DAILY_DIGEST hardcoded the retired Slack integration OVERWATCH_WEBHOOK and swallowed the send with WHEN OTHER THEN NULL, so the morning digest had NEVER been delivered on this Teams-only account while it still returned delivery attempted. Re-derived from V018 to walk the enabled ALERT_ROUTES rows and send through each row INTEGRATION_NAME (SP_NOTIFY_WEBHOOK per-route idiom), ledgering each per-route outcome to APP_ERROR_LOG as digest_send_failed instead of discarding it; the in-app digest write is untouched and the return is machine-readable sent N/M routes (#23). An idempotent block disables any enabled route whose integration is absent from SHOW NOTIFICATION INTEGRATIONS so the dead V012 default Slack route stops burying real errors (#25); another resumes TASK_ALERT_NOTIFY when any enabled route names an integration that exists, so a healthy Teams integration satisfies the gate V018 keyed to OVERWATCH_WEBHOOK (#24). Only SP_DAILY_DIGEST is re-defined; the route-disable and auto-resume are idempotent data/DDL blocks - no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 70);
