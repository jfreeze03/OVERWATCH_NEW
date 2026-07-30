-- V063__webhook_capture_once_daily_facts_failguard.sql
--
-- The two correctness/robustness fixes deferred from V062 (adversarial review
-- wf_0ae6f51b). See gen_v063.py header for detail.
--   B9   SP_NOTIFY_WEBHOOK: capture the fitting EVENT_IDs ONCE into an ARRAY so the
--        message, the ledger, and NOTIFIED_AT share one immutable set (no send-vs-
--        ledger race). !! OWNER SMOKE TEST REQUIRED (DEPLOYMENT.md) — ARRAY binding
--        is runtime-only and a byte-compare cannot prove it.
--   B34  SP_LOAD_DAILY_FACTS: a per-table failure now holds the DAILY_FACTS watermark
--        and returns a non-success string (was: swallowed -> advanced -> false
--        success). Per-table isolation preserved; the failed day self-heals next run.
--
-- The T3.1-T3.4 perf-loader restructures are handled in V064 (isolated by risk).
-- Idempotent; apply AFTER V062. No data heal (forward-healing proc swaps).

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20063, 'V063 requires V062 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 62) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_NOTIFY_WEBHOOK  (B9 capture-once - SMOKE TEST REQUIRED)
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
    fits_ids ARRAY;         -- V063: frozen fitting EVENT_IDs (VARCHAR EVENT_ID) shared by message + ledger + NOTIFIED_AT
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
        -- V063 capture-once: freeze the FITTING EVENT_IDs (eligible, ordered
        -- newest-first, whose cumulative JSON-escaped length -- each line plus
        -- 2 chars per '\n' separator -- stays <= 3000) into an immutable ARRAY.
        -- The message, the ledger, and NOTIFIED_AT are then ALL derived from
        -- THIS SAME array, so a concurrent ALERT_EVENTS insert or the 24h
        -- window sliding as CURRENT_TIMESTAMP advances cannot make the sent
        -- set and the recorded set diverge.
        SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT DESC, f.EVENT_ID)
          INTO :fits_ids
        FROM (
            SELECT e.EVENT_ID, e.RAISED_AT,
                   SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                       '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140),
                       CHR(92), CHR(92) || CHR(92)),
                       CHR(34), CHR(92) || CHR(34)),
                       CHR(10), CHR(92) || 'n'),
                       CHR(13), ''),
                       CHR(9),  CHR(92) || 't')) + 2)
                     OVER (ORDER BY e.RAISED_AT DESC, e.EVENT_ID
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
            WHERE e.STATUS = 'OPEN'
              AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
              AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')
              AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
                  >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
              AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                              WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id)
        ) f
        WHERE f.CUM_LEN <= 3000;

        -- Build the message from the frozen fits set ONLY, in the SAME order
        -- (RAISED_AT DESC, EVENT_ID) used to compute the fit. Its escaped
        -- length is <= 3000 by construction, so the LEFT(:message, 3000) at
        -- send time never truncates mid-event.
        SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\n')
               WITHIN GROUP (ORDER BY e.RAISED_AT DESC, e.EVENT_ID)
          INTO :message
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);

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

                -- Ledger rows for THIS route only (success path) -- the SAME
                -- frozen fits set that built the message, NOT a re-derivation.
                -- NOT EXISTS keeps it idempotent if this route is retried.
                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)
                SELECT e.EVENT_ID, :r_route_id
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);
                sent_total := sent_total + SQLROWCOUNT;

                -- Back-compat: NOTIFIED_AT still means "delivered somewhere at
                -- least once" (the drill, the delivery chip, and MTTA surfaces
                -- read it). Set it ONLY for the frozen fits set actually sent;
                -- a non-fitting event stays NULL and re-drains next run.
                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()
                 WHERE e.NOTIFIED_AT IS NULL
                   AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);
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

-- >>> derived:SP_LOAD_DAILY_FACTS  (B34 fail-guard: hold watermark on partial failure)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    wm TIMESTAMP_NTZ;           -- V041 R5: last successful daily load
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_short TIMESTAMP_NTZ;     -- watermark - 1d overlap (default -3d, clamp -30d)
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run
BEGIN
    SELECT MAX(WM_TS) INTO :wm
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_short := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY t
    USING (
        SELECT
            USAGE_DATE AS DAY,
            UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
            SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE,
            SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CREDITS_CLOUD_SVCS,
            SUM(COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)) AS CREDITS_ADJUSTMENT,
            SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_USED,
            SUM(COALESCE(CREDITS_BILLED,
                GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))) AS CREDITS_BILLED
        FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
        WHERE USAGE_DATE >= :lo_metering::DATE
        GROUP BY 1, 2
    ) s
    ON t.DAY = s.DAY AND t.SERVICE_TYPE = s.SERVICE_TYPE
    WHEN MATCHED THEN UPDATE SET
        CREDITS_COMPUTE = s.CREDITS_COMPUTE, CREDITS_CLOUD_SVCS = s.CREDITS_CLOUD_SVCS,
        CREDITS_ADJUSTMENT = s.CREDITS_ADJUSTMENT, CREDITS_USED = s.CREDITS_USED,
        CREDITS_BILLED = s.CREDITS_BILLED, LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, SERVICE_TYPE, CREDITS_COMPUTE, CREDITS_CLOUD_SVCS, CREDITS_ADJUSTMENT, CREDITS_USED, CREDITS_BILLED)
        VALUES (s.DAY, s.SERVICE_TYPE, s.CREDITS_COMPUTE, s.CREDITS_CLOUD_SVCS, s.CREDITS_ADJUSTMENT, s.CREDITS_USED, s.CREDITS_BILLED);

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_short::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
    SELECT
        DATE(QUERY_START_TIME),
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        COUNT(*),
        SUM(IFF(STATE = 'FAILED', 1, 0)),
        AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),
        MAX_BY(STATE, QUERY_START_TIME),
        MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE QUERY_START_TIME >= :lo_short::DATE
    GROUP BY 1, 2, 3, 4, 5;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_TASK_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_LOGIN_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_short::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        (DAY, USER_NAME, COMPANY, LOGINS, FAILED_LOGINS, PASSWORD_LOGINS)
    SELECT
        DATE(EVENT_TIMESTAMP),
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME),
        COUNT(*),
        SUM(IFF(IS_SUCCESS = 'NO', 1, 0)),
        SUM(IFF(FIRST_AUTHENTICATION_FACTOR = 'PASSWORD' AND IS_SUCCESS = 'YES', 1, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= :lo_short::DATE
    GROUP BY 1, 2, 3;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_LOGIN_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_STORAGE_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_short::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
        (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
    SELECT
        USAGE_DATE,
        DATABASE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= :lo_short::DATE
    GROUP BY 1, 2, 3;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- V041 R5+R6: advance the watermark; loader-owned freshness.
    -- V063 B34obs: HOLD the DAILY_FACTS watermark when any per-table wrap
    -- failed, so the next run re-reads from the held mark and re-covers the
    -- missed day (lo_short = wm - 1d; idempotent DELETE+INSERT). The
    -- SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed
    -- failure surfaces as a stale freshness row.
    IF (NOT failed_any) THEN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'DAILY_FACTS' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    END IF;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_METERING_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        UNION ALL
        SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        UNION ALL
        SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        UNION ALL
        SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    IF (failed_any) THEN
        RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, watermark held';
    END IF;
    RETURN 'daily facts loaded';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 63 AS VERSION,
       'Webhook capture-once (B9: message + ledger + NOTIFIED_AT share one frozen fitting-event ARRAY, no send-vs-ledger race - owner smoke test) + daily-facts fail-guard (B34: a per-table failure holds the DAILY_FACTS watermark and returns non-success instead of a false success). Deferred from V062; perf loader T3 in V064.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 63);
