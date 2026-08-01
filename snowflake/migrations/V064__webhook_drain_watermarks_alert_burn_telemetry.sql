-- V064__webhook_drain_watermarks_alert_burn_telemetry.sql
--
-- Codex-R2 NEXT-tier owner-migration bundle. See gen_v064.py header for detail.
--   rec8    SP_NOTIFY_WEBHOOK: OLDEST-first bounded drain (was newest-first single
--           shot -> oldest events starved past the 24h window). Batches drain the
--           backlog forward, capture-once per batch (B9 preserved). !! OWNER SMOKE
--           TEST -- SYSTEM$SEND + ARRAY binding are runtime-only.
--   rec7    SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE: per-source daily watermarks
--           so one table's failure holds only that source's mark (was: one shared
--           'DAILY_FACTS' mark re-read all four). Reconcile rewinds the 4 new keys
--           in lockstep. !! OWNER SMOKE TEST -- watermark self-heal is runtime-only.
--   rec20a  SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH: trailing-30-COMPLETE-day burn
--           (was SUM/30 over a partial-current-day span -> could suppress the
--           breach). Same canonical burn as the app mart + contract.py.
--   rec18   APP_QUERY_TELEMETRY += SAMPLE_PROB, QUERY_ID (re-weightable percentiles
--           + Query-History joinability). App persist path + view land app-side.
--
-- NUMBERING: V063 says "T3 in V064"; that T3-perf work was deferred and unbuilt,
-- and versions must be contiguous, so this bundle is V064. T3-perf becomes V065.
-- Idempotent; apply AFTER V063.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20064, 'V064 requires V063 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 63) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- rec18: telemetry re-weightability (SAMPLE_PROB) + Query-History joinability
-- (QUERY_ID). Additive + idempotent; existing rows read NULL (the app persist
-- path fills them going forward, and the weighted view treats NULL as 1.0).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY
    ADD COLUMN IF NOT EXISTS SAMPLE_PROB NUMBER(6,5);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY
    ADD COLUMN IF NOT EXISTS QUERY_ID VARCHAR(64);

-- rec7 cold-start seed (review fix): the daily loader below is repointed from the
-- single shared 'DAILY_FACTS' watermark to four per-source keys. Seed each NEW key
-- from the RETAINED 'DAILY_FACTS' position so a cutover DURING an outage (the
-- shared mark held behind by V063's B34 fail-guard) inherits that held value and
-- re-covers the same backlog V063 would have -- instead of cold-starting at
-- today-5/today-3 and silently, permanently dropping the gap. Idempotent: seeds
-- only an ABSENT key, and only when a 'DAILY_FACTS' mark exists (a fresh install
-- with no mark yet correctly lets the first loader run cold-start from default).
INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS (SOURCE, WM_TS)
SELECT s.SOURCE,
       (SELECT MAX(WM_TS) FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS')
FROM (SELECT 'FACT_METERING_DAILY' AS SOURCE UNION ALL SELECT 'FACT_TASK_DAILY'
      UNION ALL SELECT 'FACT_LOGIN_DAILY' UNION ALL SELECT 'FACT_STORAGE_DAILY') s
WHERE (SELECT MAX(WM_TS) FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS w WHERE w.SOURCE = s.SOURCE);

-- #26: single-flight lease for SP_NOTIFY_WEBHOOK. A minimal control table (the only
-- robust way to express a cross-run guard here) holding ONE sentinel row. The sender
-- UPDATEs it at entry and bails if another live run holds it (see the proc comment for
-- WHY the send-then-ledger ordering it protects is intentionally send-first). Idempotent:
-- CREATE IF NOT EXISTS + a NOT EXISTS seed, so re-applying the migration is a no-op.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE (
    LEASE_NAME  VARCHAR(80)   NOT NULL,
    HELD        BOOLEAN       NOT NULL DEFAULT FALSE,
    HOLDER      VARCHAR(200),
    ACQUIRED_AT TIMESTAMP_NTZ
);
INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE (LEASE_NAME, HELD)
SELECT 'SP_NOTIFY_WEBHOOK', FALSE
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK');

-- >>> SP_NOTIFY_WEBHOOK  (rec8 oldest-first bounded drain - SMOKE TEST REQUIRED)
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
    batches INT DEFAULT 0;              -- V064 rec8: total message batches sent this run
    r_batch INT DEFAULT 0;             -- V064 rec8: batches drained for the current route
    r_sent_any BOOLEAN DEFAULT FALSE;  -- V064 rec8: this route delivered >= 1 batch
    send_failed BOOLEAN DEFAULT FALSE; -- V064 rec8: this route's send raised -> stop draining it
    max_batches INT DEFAULT 6;         -- V064 rec8: bound batches/route/run (backlog spills to next run, never floods)
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
    -- V064 #26: single-flight concurrency guard. The send-then-ledger ordering
    -- inside the loop is INTENTIONAL for paging -- at-least-once bias: a
    -- claim-before-send outbox could LOSE a page if the claim commits but the send
    -- is dropped, which is worse for an alerting system than a rare duplicate page.
    -- That ordering does leave one race: two OVERLAPPING runs can both read the same
    -- eligible events before either writes its ledger rows, and re-send. This lease
    -- closes ONLY that window; it does NOT change the send-first ordering. Acquire a
    -- single sentinel row at entry; bail if another live run holds it; release at
    -- exit. A lease older than 1h is treated as abandoned (a crashed run that never
    -- released) and reclaimed, so the sender can never wedge itself out forever.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
       SET HELD = TRUE, HOLDER = CURRENT_SESSION(), ACQUIRED_AT = CURRENT_TIMESTAMP()
     WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
       AND (HELD = FALSE OR ACQUIRED_AT < DATEADD('hour', -1, CURRENT_TIMESTAMP()));
    IF (SQLROWCOUNT = 0) THEN
        RETURN 'skipped - another SP_NOTIFY_WEBHOOK run holds the sender lease';
    END IF;

    FOR rec IN c1 DO
        r_route_id := rec.ROUTE_ID;
        r_family := rec.FAMILY;
        r_minsev := rec.MIN_SEVERITY;
        r_integration := rec.INTEGRATION_NAME;
        r_compfilter := rec.COMPANY_FILTER;
        r_batch := 0;
        r_sent_any := FALSE;
        send_failed := FALSE;

        -- V064 rec8: OLDEST-FIRST BOUNDED DRAIN. Each iteration captures-once
        -- (frozen ARRAY, the B9 invariant) the OLDEST eligible-for-this-route
        -- events that fit 3000 escaped chars, sends them, and ledgers +
        -- NOTIFIED_AT-stamps THAT SAME set. The ledger write makes the next
        -- iteration's eligibility exclude what was just sent, so the loop drains
        -- strictly forward and terminates when no eligible event remains (or at
        -- max_batches). A send failure stops THIS route this run; siblings drain.
        LOOP
            -- Capture-once: the OLDEST eligible events (open, within 24h, matching
            -- this route's family/company/severity, not yet delivered to THIS
            -- route) whose cumulative JSON-escaped length (each line + 2 per '\n'
            -- separator) stays <= 3000, frozen into an ARRAY in send order.
            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT ASC, f.EVENT_ID)
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
                         OVER (ORDER BY e.RAISED_AT ASC, e.EVENT_ID
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

            -- Nothing eligible left for this route -> done draining it.
            IF (fits_ids IS NULL OR ARRAY_SIZE(:fits_ids) = 0) THEN
                EXIT;
            END IF;

            -- Build the message from the frozen fits set ONLY, in the SAME
            -- (RAISED_AT ASC, EVENT_ID) order used to compute the fit, so its
            -- escaped length is <= 3000 by construction and LEFT(:message, 3000)
            -- never truncates mid-event.
            SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\n')
                   WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
              INTO :message
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);

            -- Non-empty fits set -> non-empty message; guard is defence only.
            IF (:message IS NULL OR :message = '') THEN
                EXIT;
            END IF;

            -- v3: JSON-escape (backslash first, then quote, newline, CR, tab).
            message := REPLACE(:message, CHR(92), CHR(92) || CHR(92));
            message := REPLACE(:message, CHR(34), CHR(92) || CHR(34));
            message := REPLACE(:message, CHR(10), CHR(92) || 'n');
            message := REPLACE(:message, CHR(13), '');
            message := REPLACE(:message, CHR(9),  CHR(92) || 't');

            BEGIN
                CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                    SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                        'OVERWATCH alerts:' || CHR(92) || 'n' || LEFT(:message, 3000)),
                    SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));

                -- Ledger rows for THIS route from the SAME frozen fits set (NOT a
                -- re-derivation). NOT EXISTS keeps it idempotent on retry.
                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)
                SELECT e.EVENT_ID, :r_route_id
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);
                sent_total := sent_total + SQLROWCOUNT;

                -- Back-compat: NOTIFIED_AT means "delivered somewhere at least
                -- once" (drill / delivery chip / MTTA read it). Only for the
                -- frozen fits set actually sent; a non-fitting event stays NULL
                -- and re-drains a later run.
                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()
                 WHERE e.NOTIFIED_AT IS NULL
                   AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);

                r_sent_any := TRUE;
                batches := batches + 1;
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                           'route ' || :r_route_id || ' integration ' || :r_integration ||
                           ' - will retry next run; other routes unaffected',
                           CURRENT_ROLE();
                    send_failed := TRUE;
            END;

            -- Integration down: stop draining THIS route this run (more batches
            -- would just fail). Other routes keep going.
            IF (send_failed) THEN
                EXIT;
            END IF;

            r_batch := r_batch + 1;
            IF (r_batch >= max_batches) THEN
                EXIT;   -- bounded: leave the remaining backlog for the next run
            END IF;
        END LOOP;

        IF (r_sent_any) THEN
            routes_hit := routes_hit + 1;
        END IF;
    END FOR;

    -- Loud, not silent: an (event,route) pair aging past the 24h window with NO
    -- delivery FOR THAT ROUTE gets one error-log row per episode.
    -- V064 #27 (PER-ROUTE expiry): the old test keyed on ALERT_EVENTS.NOTIFIED_AT,
    -- which is stamped after the FIRST route delivers -- so an event that reached
    -- route A but never route B was NEVER flagged expired for B. Expiry is now per
    -- (event,route): a pair is expired when the event is past the 24h window and has
    -- NO row in ALERT_DELIVERIES for THAT route (NOT EXISTS on event+route), instead
    -- of keying on NOTIFIED_AT. The candidate route set is still the SEND eligibility
    -- (family + company + severity), so a flagged pair was genuinely eligible to that
    -- route but undelivered.
    -- (An OPEN event whose rule row was deleted from ALERT_CONFIG is undeliverable in
    -- BOTH the send and expired paths -- both INNER-join config -- so it is
    -- intentionally not flagged; it stays visibly OPEN in-app.)
    -- V064 #40 (log-inflation guard): a persistent backlog must NOT re-insert the
    -- same pair every sender run -- that would make a 30d failure KPI count RUNS, not
    -- events. Each (event,route) pair is logged at most once per 24h via NOT EXISTS
    -- on a prior 'undelivered_expired' row with the same event/route key (CONTEXT).
    -- The first signal is never lost; a still-stuck pair re-logs at most once a day.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
    SELECT 'NotifyWebhook', 'undelivered_expired',
           'open event ' || e.EVENT_ID || ' [' || e.SEVERITY ||
           '] aged past the 24h window undelivered to route ' || r2.ROUTE_ID ||
           ' (integration ' || r2.INTEGRATION_NAME || '); event remains OPEN in-app',
           'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID,
           CURRENT_ROLE()
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2
      ON r2.ENABLED
     AND (r2.FAMILY = 'ALL' OR c.FAMILY = r2.FAMILY)
     AND (COALESCE(r2.COMPANY_FILTER, 'ALL') = 'ALL'
          OR e.COMPANY = r2.COMPANY_FILTER
          OR UPPER(e.COMPANY) = 'ALL')
     AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
         >= CASE r2.MIN_SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
    WHERE e.STATUS = 'OPEN'
      AND e.RAISED_AT < DATEADD('hour', -24, CURRENT_TIMESTAMP())
      AND e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r2.ROUTE_ID)
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG a
                      WHERE a.ERROR_TYPE = 'undelivered_expired'
                        AND a.CONTEXT = 'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID
                        AND a.LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()));
    expired := SQLROWCOUNT;

    -- V064 #26: release the single-flight lease -- FENCED to this session. Without the
    -- HOLDER = CURRENT_SESSION() check a stalled run (>1h) whose lease was already
    -- reclaimed by a successor would, on finally finishing, clear the SUCCESSOR's lease
    -- and let a third run start concurrently -- defeating single-flight in exactly the
    -- crash/stall case the 1h window exists for. The fence makes release a no-op unless
    -- we still hold it. Best-effort otherwise: an unhandled failure above leaves
    -- HELD=TRUE, and the next run's acquire reclaims it after the 1h staleness window --
    -- the sender can never wedge itself out permanently.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
       SET HELD = FALSE, HOLDER = NULL
     WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
       AND HOLDER = CURRENT_SESSION();

    RETURN 'sent ' || :sent_total || ' event-route pair(s) across ' || :routes_hit ||
           ' route(s) in ' || :batches || ' batch(es); ' || :expired ||
           ' newly expired-undelivered (event,route) pair(s) flagged';
END;
$$;

-- >>> derived:SP_LOAD_DAILY_FACTS  (rec7 per-source watermarks - SMOKE TEST REQUIRED)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    wm_metering TIMESTAMP_NTZ;  -- V064 rec7: per-source watermarks (was one shared DAILY_FACTS mark)
    wm_task TIMESTAMP_NTZ;
    wm_login TIMESTAMP_NTZ;
    wm_storage TIMESTAMP_NTZ;
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_task TIMESTAMP_NTZ;      -- watermark - 1d overlap (default -3d, clamp -30d)
    lo_login TIMESTAMP_NTZ;
    lo_storage TIMESTAMP_NTZ;
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run (return string)
BEGIN
    -- V064 rec7: each daily source keeps its OWN watermark so a per-table
    -- failure holds only THAT source's mark; siblings advance independently
    -- (no whole-group re-read of the costliest source on any one failure).
    SELECT MAX(WM_TS) INTO :wm_metering
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_METERING_DAILY';
    SELECT MAX(WM_TS) INTO :wm_task
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_TASK_DAILY';
    SELECT MAX(WM_TS) INTO :wm_login
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_LOGIN_DAILY';
    SELECT MAX(WM_TS) INTO :wm_storage
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_STORAGE_DAILY';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm_metering),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_task := GREATEST(COALESCE(DATEADD('day', -1, :wm_task),
                                 DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                        DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_login := GREATEST(COALESCE(DATEADD('day', -1, :wm_login),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_storage := GREATEST(COALESCE(DATEADD('day', -1, :wm_storage),
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

    -- V064 rec7: metering has no txn wrap -- a fact-load failure aborts the proc
    -- before this line (V063's deliberate anchor-first design), so reaching here
    -- means metering loaded; advance its own mark. The mark MERGE is GUARDED
    -- (review fix): it is a NEW statement ahead of the isolated sibling blocks, so
    -- a transient OW_LOAD_WATERMARKS lock here must not abort the proc and starve
    -- task/login/storage -- log + hold the mark + fall through instead.
    BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_METERING_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_METERING_DAILY watermark advance - facts loaded, mark held, siblings unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V064 rec7: hold metering mark, keep loading siblings
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_task::DATE;
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
    WHERE QUERY_START_TIME >= :lo_task::DATE
    GROUP BY 1, 2, 3, 4, 5;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_TASK_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
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
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_login::DATE;
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
    WHERE EVENT_TIMESTAMP >= :lo_login::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_LOGIN_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
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
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_storage::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
        (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
    SELECT
        USAGE_DATE,
        DATABASE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= :lo_storage::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_STORAGE_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- V064 rec7: the single shared DAILY_FACTS watermark advance is GONE -- each
    -- source advances its OWN mark in its own success path above, so one
    -- table's failure holds only that table's mark (siblings stay current).
    -- The SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed
    -- failure still surfaces as a stale freshness row.

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
        RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, that source''s watermark held';
    END IF;
    RETURN 'daily facts loaded';
END;
$$;

-- >>> derived:SP_NIGHTLY_RECONCILE  (rec7 rewind the 4 per-source daily marks)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rv VARCHAR;             -- V064 #9: each child loader's RETURN verdict, captured per CALL
    fails INT DEFAULT 0;    -- children whose RETURN carried a WITH ERRORS/FAIL token
    recon_start TIMESTAMP_NTZ;  -- V064 #9: run start, to scope the failure-log count
    logged_fails INT DEFAULT 0; -- APP_ERROR_LOG failure rows written during this run
BEGIN
    -- V064 #9: most child loaders SWALLOW arm failures (log a '%_failed%' row to
    -- APP_ERROR_LOG, then return a benign 'loaded' string), so a per-CALL return-string
    -- check catches only the two machine-verdict children. The AUTHORITATIVE signal is
    -- the count of failure rows the children logged during this run -- captured below.
    recon_start := CURRENT_TIMESTAMP();
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());

    -- Pull the watermarks back so the loaders re-cover the window.
    -- V064 rec7: the daily loader now keeps FOUR per-source marks, not one
    -- shared DAILY_FACTS mark -- rewind all four here or SP_LOAD_DAILY_FACTS
    -- reads a current mark and the nightly daily re-coverage silently no-ops.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'HOURLY_FACTS',
                      'FACT_METERING_DAILY', 'FACT_TASK_DAILY',
                      'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY');

    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(0);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;

    -- V064 #9: the AUTHORITATIVE failure signal. Every child loader logs a '%_failed%'
    -- row to APP_ERROR_LOG when an arm fails (fact_load_failed, mart_load_failed,
    -- extract_load_failed, cloud_svc_mart_failed, object_cost_load_failed, ...), INCLUDING
    -- the ones that swallow the error and return a benign 'loaded' string -- so a
    -- return-string check alone would miss 4 of the 5 children. Count what was logged
    -- during THIS run; the per-CALL return check above is a secondary catch for the two
    -- machine-verdict children (SP_LOAD_DAILY_FACTS, SP_LOAD_MARTS_V27).
    SELECT COUNT(*) INTO :logged_fails
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE LOGGED_AT >= :recon_start
      AND (ERROR_TYPE ILIKE '%_failed%' OR ERROR_TYPE ILIKE '%WITH ERRORS%');
    IF (fails > 0 OR logged_fails > 0) THEN
        RETURN 'RECONCILE WITH ERRORS: ' || :logged_fails || ' loader failure(s) logged'
            || IFF(:fails > 0, ' + ' || :fails || ' child verdict(s) non-success', '')
            || ' (metering + full-retention marts 3 days, extract-fed marts 2); inspect APP_ERROR_LOG';
    END IF;
    RETURN 'RECONCILE OK - nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2); no child loader failures logged';
END;
$$;

-- >>> derived:SP_ALERT_SCAN_DAILY  (rec20-alert trailing-30-complete-day burn)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- C9: daily-cadence sibling of SP_ALERT_SCAN. The 6 rule blocks whose signal
-- is a DAILY-loaded fact (FACT_TASK_DAILY, FACT_LOGIN_DAILY, FACT_METERING_DAILY)
-- moved here and chained AFTER TASK_LOAD_DAILY, so they scan once the daily
-- facts are fresh instead of 24x/day over stale/partial rows. Same v7 per-block
-- isolation and the SAME SETTINGS read (budget + credit + AI price) as the
-- hourly scan. The self-alert uses a DISTINCT '|DAILY|' dedupe key so it never
-- collides with the hourly OPS_SCAN_DEGRADED event on the same date.
DECLARE
    budget_usd FLOAT;
    credit_price FLOAT;
    ai_credit_price FLOAT;
    emsg VARCHAR;
    fails INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :budget_usd, :credit_price, :ai_credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- [06] PIPE_TASK_FAILURES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, tk.COMPANY, c.SEVERITY,
               COALESCE(tk.DATABASE_NAME || '.', '') || COALESCE(tk.SCHEMA_NAME || '.', '')
                   || tk.TASK_NAME || ' failed ' || tk.FAILED || 'x on ' || tk.DAY,
               'Database: ' || COALESCE(tk.DATABASE_NAME, 'unknown') || '. '
                   || LEFT(COALESCE(tk.LAST_ERROR, 'No error text captured.'), 450),
               tk.FAILED,
               c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || COALESCE(tk.SCHEMA_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY tk
          ON c.RULE_ID = 'PIPE_TASK_FAILURES'
         AND tk.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND tk.FAILED >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PIPE_TASK_FAILURES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [07] SEC_FAILED_LOGINS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
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

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_FAILED_LOGINS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [08] COST_BUDGET_PACE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            (SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
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

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_BUDGET_PACE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [09] COST_FORECAST_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            (SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
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
        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_FORECAST_BREACH - other rules unaffected', CURRENT_ROLE();
    END;
    -- [13b] COST_AI_CREEP
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_AI_CREEP: the canonical AI/Cortex bucket (SERVICE_TYPE ILIKE
        -- '%CORTEX%'/'AI%'/'%INTELLIGENCE%') from FACT_METERING_DAILY growing
        -- week-over-week, dollarized at the AI credit rate (AI_CREDIT_PRICE_USD,
        -- NOT the compute rate). COST_SERVERLESS_CREEP carves AI out; this rule
        -- owns it. Re-alerts weekly while creeping.
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'AI/Cortex spend up ' || ROUND(a.GROWTH_PCT, 0) || '% week-over-week ($' ||
                   ROUND(a.THIS_WK_USD, 0) || ' vs $' || ROUND(a.PRIOR_WK_USD, 0) || ' prior 7d)',
               'Last 7d ' || ROUND(a.THIS_WK_CR, 2) || ' AI credits ($' || ROUND(a.THIS_WK_USD, 2) ||
                   ' @ $' || ROUND(:ai_credit_price, 2) || '/cr) vs ' || ROUND(a.PRIOR_WK_CR, 2) ||
                   ' credits prior. Cortex/AI usage grows silently - confirm the workload is ' ||
                   'intentional and priced in. Breakdown: Cost > Spend (by service).',
               a.GROWTH_PCT,
               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM cfg c
        JOIN (
            SELECT SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR,
                   SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS THIS_WK_USD,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS PRIOR_WK_USD,
                   -- Onset (prior week 0) is an infinite ratio: emit a finite 999%
                   -- sentinel so a brand-new AI workload FIRES (the case budget-pace
                   -- misses) instead of GROWTH_PCT going NULL and dropping the row.
                   CASE WHEN SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) = 0
                        THEN IFF(SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) > 0, 999, 0)
                        ELSE (SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              / SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              - 1) * 100 END AS GROWTH_PCT
            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())
              AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')
        ) a ON c.RULE_ID = 'COST_AI_CREEP'
           AND a.THIS_WK_CR >= 5 AND a.GROWTH_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_AI_CREEP - other rules unaffected', CURRENT_ROLE();
    END;
    -- [16] COST_CONTRACT_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_CONTRACT_BREACH: current contract projected to exhaust within
        -- threshold days at the trailing 30 complete-day burn rate. Weekly-recurring
        -- until the contract or the burn changes; CRITICAL inside 14 days.
        SELECT c.RULE_ID, 'ALL',
               IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY),
               'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||
                   TO_VARCHAR(p.EXHAUST_DATE) || ')',
               'Consumed ' || ROUND(p.CONSUMED, 0) || ' of ' || ROUND(p.TOTAL, 0) ||
                   ' contracted credits; trailing 30 complete-day burn ' || ROUND(p.DAILY_BURN, 1) ||
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
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
            )
        ) p ON c.RULE_ID = 'COST_CONTRACT_BREACH'
           AND p.TOTAL > 0 AND p.DAILY_BURN > 0
           AND p.DAYS_LEFT BETWEEN 0 AND c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_CONTRACT_BREACH - other rules unaffected', CURRENT_ROLE();
    END;
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 6 daily alert rule block(s) failed this run',
               'APP_ERROR_LOG has the SQL errors (rule_block_failed). The other rules '
                   || 'kept firing - that is the point of the v7 decomposition.',
               :fails,
               c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_SCAN_DEGRADED' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    RETURN 'alert scan daily v1 (task/login/budget-pace/forecast/AI-creep/contract): ' || (6 - :fails) || '/6 rule blocks ok (daily)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 64 AS VERSION,
       'Webhook oldest-first bounded drain (rec8: batches drain the backlog forward so the oldest alerts stop starving past the 24h window; capture-once per batch, shared expired eligibility - owner smoke test) + per-source daily watermarks (rec7: SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE, one table failure holds only its own mark) + contract-breach trailing-30-complete-day burn (rec20-alert, was /30 over a partial day) + APP_QUERY_TELEMETRY SAMPLE_PROB/QUERY_ID (rec18).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 64);
