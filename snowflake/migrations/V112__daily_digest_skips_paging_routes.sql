-- V112__daily_digest_skips_paging_routes.sql
--
-- The morning digest never reaches a CRITICAL-only (paging) route. DELIVER_DIGEST defaults TRUE
-- (V070) and SP_DAILY_DIGEST walked every ENABLED digest-eligible route with no severity filter, so a
-- paging route added via the documented CRITICAL -> PagerDuty recipe (which omits DELIVER_DIGEST)
-- inherited TRUE and got the executive digest -- paging on-call for a non-incident. Snowflake cannot
-- ALTER a column default to a literal, so the digest cursor now also excludes CRITICAL-only routes
-- (MIN_SEVERITY = 'CRITICAL'), the paging targets by convention.
--
-- Re-derives SP_DAILY_DIGEST from V070; everything else byte-identical. No schema change; owner
-- applies after V111 and the next SP_DAILY_DIGEST run skips CRITICAL-only routes. Never runs from app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20112, 'V112 requires V111 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 111) THEN
        RAISE not_ready;
    END IF;
END;
$$;

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
          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'   -- alerting-hunt: never send the exec digest to a CRITICAL-only (paging) route (DELIVER_DIGEST defaults TRUE, and Snowflake cannot ALTER that default)
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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 112 AS VERSION,
       'Daily digest skips paging routes: SP_DAILY_DIGEST re-derived from V070 so its route cursor also excludes CRITICAL-only routes (UPPER(MIN_SEVERITY) <> CRITICAL). DELIVER_DIGEST defaults TRUE and cannot be ALTERed to a literal in Snowflake, so a paging route added via the CRITICAL -> PagerDuty recipe no longer receives the executive morning digest. Everything else byte-identical. Proc only, no schema change; forward-healing on the next SP_DAILY_DIGEST run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 112);
