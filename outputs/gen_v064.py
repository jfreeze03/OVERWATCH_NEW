#!/usr/bin/env python3
r"""Forward-generate V064__webhook_drain_watermarks_alert_burn_telemetry.sql.

The Codex-R2 NEXT-tier owner-migration bundle (rec 7 / 8 / 20-alert / 18).

NUMBERING: V063's header forward-references "perf loader (T3) in V064". That
T3.1-T3.4 restructure was DEFERRED by the owner and never built, so the number
V064 was free; the framework requires CONTIGUOUS versions (validate.sql checks
COUNT(DISTINCT VERSION) BETWEEN 1 AND N = N), so this bundle takes V064 and the
deferred T3-perf work, if ever built, becomes V065. V063 is left untouched
(byte-locked, about to be applied); its "T3 in V064" comment is now stale.

Fixes:
  rec8   SP_NOTIFY_WEBHOOK -- OLDEST-first bounded drain. V063 fit the NEWEST
         events into ONE 3000-char message per route per run, so under sustained
         volume the oldest events never fit and eventually crossed the 24h window
         undelivered (undelivered_expired) -- backwards from urgency. Now each
         route drains in batches OLDEST-first: every batch captures-once (the B9
         frozen ARRAY) the oldest eligible events that fit 3000 chars, sends +
         ledgers + stamps NOTIFIED_AT from that one set, and the ledger write
         makes the next batch exclude what was just sent -- so the loop drains
         strictly forward, terminates when nothing eligible remains, and is
         bounded by max_batches (backlog spills to the next run, never floods).
         A send failure stops THIS route this run; siblings keep draining. The
         expired-detection now shares the SEND eligibility (family+company+
         severity), so "flagged expired" == "was eligible to some route but
         undelivered", never "was never eligible".
         !! SYSTEM$SEND + ARRAY binding are runtime-only -> OWNER SMOKE TEST.
  rec7   SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE -- per-source daily
         watermarks. V063 held ONE 'DAILY_FACTS' mark; any sibling failure made
         all four sources re-read the held window next run (re-MERGE-ing the
         costliest read, METERING_DAILY_HISTORY, which can never fail). Now each
         source keeps its OWN mark (FACT_METERING/TASK/LOGIN/STORAGE_DAILY),
         advanced in its own success path (metering after its MERGE; the other
         three atomically inside their transaction before COMMIT so a rollback
         holds the mark). SP_NIGHTLY_RECONCILE's rewind is updated in lockstep to
         pull back the FOUR new keys (else its daily re-coverage silently breaks).
         !! OWNER SMOKE TEST (watermark self-heal is runtime-only).
  rec20a SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH -- DAILY_BURN was
         SUM(CREDITS_BILLED)/30 over a 31-date span INCLUDING today's partial,
         biasing burn low -> overstating days-left -> could SUPPRESS the breach
         alert. Now trailing-30-COMPLETE-days (DAY BETWEEN today-30 AND today-1,
         divide by the actual complete-day count) -- the SAME canonical burn the
         app mart (mart_sql.contract_exhaustion) and contract.py already use.
  rec18  APP_QUERY_TELEMETRY += SAMPLE_PROB, QUERY_ID (additive, idempotent).
         SAMPLE_PROB (1.0 for the must-persist exception stream, ~0.02 for the
         sampled healthy stream) makes fleet percentiles re-weightable; QUERY_ID
         makes rows joinable to ACCOUNT_USAGE.QUERY_HISTORY. The app persist path
         + weighted Admin>Performance view land in the app change, not here.

Derivation law (see gen_v061/2/3): SP_LOAD_DAILY_FACTS re-derived from its LATEST
def (V063) and SP_NIGHTLY_RECONCILE + SP_ALERT_SCAN_DAILY from theirs (V062) via
extract_proc + count-asserted needle edits. SP_NOTIFY_WEBHOOK is a RESTRUCTURE
(single-shot -> loop) so it is authored in full as a literal (as V062 did for the
SP_ALERT_SCAN_DAILY split), with invariant asserts. tests/test_v064_*.py
byte-compares. Idempotent; apply AFTER V063.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


# ============ SP_NOTIFY_WEBHOOK: full restructure (rec8, oldest-first drain) =====
# Authored in full (not derived) -- a single-shot -> bounded-loop restructure
# cannot be a clean needle edit. B9's capture-once (frozen fits_ids ARRAY shared
# by message + ledger + NOTIFIED_AT) is preserved WITHIN each batch.
WEBHOOK = r"""CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()
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
            -- Capture-once: the OLDEST eligible events (open, within the send window --
            -- 24h, or 7d for CRITICAL per #10-slice -- matching this route's
            -- family/company/severity, not yet delivered to THIS
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
                  -- V064 #10-slice: an undelivered CRITICAL must not starve past the 24h
                  -- send floor while it is still OPEN. Extend the send-eligibility window
                  -- to 7d for CRITICAL ONLY (still gated STATUS='OPEN' + the per-route
                  -- NOT EXISTS below, still drained oldest-first); every other severity
                  -- keeps the 24h floor. This is the CHEAP slice -- no dead-letter/replay
                  -- state machine. The expiry watchdog + #40 once-per-24h guard are intact
                  -- below (a CRITICAL delivered in this run's drain gets a ledger row, so
                  -- the watchdog's per-route NOT EXISTS excludes it that same run).
                  AND e.RAISED_AT >= CASE WHEN e.SEVERITY = 'CRITICAL'
                                          THEN DATEADD('day', -7, CURRENT_TIMESTAMP())
                                          ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END
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
EXCEPTION
    -- V064 #9 OUTER LEASE HANDLER. The lease is acquired at entry (HELD=TRUE) and
    -- released on the normal RETURN path above (fenced to CURRENT_SESSION). But any
    -- UNHANDLED error mid-proc -- a failed capture SELECT, the message LISTAGG, the
    -- NOTIFIED_AT stamp, the expired-tail INSERT -- would skip that release and leave
    -- HELD=TRUE, wedging ALL delivery until the 1h stale-reclaim. This handler performs
    -- the SAME fenced release (a no-op unless we still hold it, so it is safe even if the
    -- entry acquire itself was what failed), logs the error, and RE-RAISEs so the task
    -- run FAILs loudly rather than swallowing. The inner per-route send handler is
    -- unchanged: a route send failure is caught there (send_failed:=TRUE) and does NOT
    -- reach here, so one integration being down never trips the outer handler.
    WHEN OTHER THEN
        emsg := SQLERRM;
        UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
           SET HELD = FALSE, HOLDER = NULL
         WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
           AND HOLDER = CURRENT_SESSION();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'NotifyWebhook', 'webhook_run_failed', :emsg,
               'unhandled error in SP_NOTIFY_WEBHOOK - fenced sender lease released, run re-raised', CURRENT_ROLE();
        RAISE;
END;
$$;
"""


# ===================== validated needle edits (V064 authoring pass) ==============
EDITS = {
    # ---- rec7: SP_LOAD_DAILY_FACTS -> per-source watermarks (from V063) ----------
    'SP_LOAD_DAILY_FACTS': ('V063__webhook_capture_once_daily_facts_failguard.sql', [
        # D1: per-source watermark + lo declarations
        ("""    wm TIMESTAMP_NTZ;           -- V041 R5: last successful daily load
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_short TIMESTAMP_NTZ;     -- watermark - 1d overlap (default -3d, clamp -30d)
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run""",
         """    wm_metering TIMESTAMP_NTZ;  -- V064 rec7: per-source watermarks (was one shared DAILY_FACTS mark)
    wm_task TIMESTAMP_NTZ;
    wm_login TIMESTAMP_NTZ;
    wm_storage TIMESTAMP_NTZ;
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_task TIMESTAMP_NTZ;      -- watermark - 1d overlap (default -3d, clamp -30d)
    lo_login TIMESTAMP_NTZ;
    lo_storage TIMESTAMP_NTZ;
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run (return string)""", 1),
        # D2: read four per-source marks + compute four los
        ("""    SELECT MAX(WM_TS) INTO :wm
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_short := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);""",
         """    -- V064 rec7: each daily source keeps its OWN watermark so a per-table
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
                           DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);""", 1),
        # D3: metering advances its own mark right after its MERGE (no txn: a
        # metering failure aborts the proc before this line)
        ("""        VALUES (s.DAY, s.SERVICE_TYPE, s.CREDITS_COMPUTE, s.CREDITS_CLOUD_SVCS, s.CREDITS_ADJUSTMENT, s.CREDITS_USED, s.CREDITS_BILLED);

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed""",
         """        VALUES (s.DAY, s.SERVICE_TYPE, s.CREDITS_COMPUTE, s.CREDITS_CLOUD_SVCS, s.CREDITS_ADJUSTMENT, s.CREDITS_USED, s.CREDITS_BILLED);

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
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed""", 1),
        # D4-D9: per-source lo windows (each needle unique by table / source column)
        ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_short::DATE;",
         "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_task::DATE;", 1),
        ("    WHERE QUERY_START_TIME >= :lo_short::DATE",
         "    WHERE QUERY_START_TIME >= :lo_task::DATE", 1),
        ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_short::DATE;",
         "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_login::DATE;", 1),
        ("    WHERE EVENT_TIMESTAMP >= :lo_short::DATE",
         "    WHERE EVENT_TIMESTAMP >= :lo_login::DATE", 1),
        ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_short::DATE;",
         "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_storage::DATE;", 1),
        ("    WHERE USAGE_DATE >= :lo_short::DATE",
         "    WHERE USAGE_DATE >= :lo_storage::DATE", 1),
        # D10: task advances its mark INSIDE its transaction (atomic; a rollback
        # holds the mark). Anchored on the unique 5-col GROUP BY.
        ("""    GROUP BY 1, 2, 3, 4, 5;
    COMMIT;""",
         """    GROUP BY 1, 2, 3, 4, 5;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_TASK_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;""", 1),
        # D11: login advance (anchored on the post-D7 login source filter, unique)
        ("""    WHERE EVENT_TIMESTAMP >= :lo_login::DATE
    GROUP BY 1, 2, 3;
    COMMIT;""",
         """    WHERE EVENT_TIMESTAMP >= :lo_login::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_LOGIN_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;""", 1),
        # D12: storage advance (anchored on the post-D9 storage source filter)
        ("""    WHERE USAGE_DATE >= :lo_storage::DATE
    GROUP BY 1, 2, 3;
    COMMIT;""",
         """    WHERE USAGE_DATE >= :lo_storage::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_STORAGE_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;""", 1),
        # D13: the single 'DAILY_FACTS' gated advance is GONE (per-source above)
        ("""    -- V041 R5+R6: advance the watermark; loader-owned freshness.
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
""",
         """    -- V064 rec7: the single shared DAILY_FACTS watermark advance is GONE -- each
    -- source advances its OWN mark in its own success path above, so one
    -- table's failure holds only that table's mark (siblings stay current).
    -- The SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed
    -- failure still surfaces as a stale freshness row.
""", 1),
        # D14: return string names the per-source hold
        ("    RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, watermark held';",
         "    RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, that source''s watermark held';", 1),
    ]),
    # ---- rec7 lockstep: SP_NIGHTLY_RECONCILE rewinds the 4 new keys (from V062) ---
    # ---- + #9: capture child loader verdicts and report a machine-readable status.
    'SP_NIGHTLY_RECONCILE': ('V062__loader_robustness_alert_split_webhook.sql', [
        # #9-A: DECLARE block for the child verdict capture (the V062 proc had none).
        ("""AS
$$
BEGIN
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY""",
         """AS
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
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY""", 1),
        # #9-B: capture each CALL's return + aggregate a parent verdict. The old
        # proc CALLed the 5 child loaders and ignored their return strings, then
        # ALWAYS returned success -- so a child that reported 'WITH ERRORS' was
        # invisible to the caller/telemetry. We now RESULT_SCAN each CALL's return,
        # count non-success tokens, and prefix the parent RETURN with a
        # machine-readable verdict. Children's behavior is unchanged (observe only).
        ("""    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(0);
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);

    RETURN 'nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2)';""",
         """    -- V064 #4-drop ANALYSIS (KEEP -- verified NOT redundant): TASK_LOAD_DAILY runs
    -- SP_LOAD_DAILY_FACTS first (root, ~1 day back from its live watermark), then
    -- TASK_NIGHTLY_RECONCILE runs AFTER it, rewinds the four daily marks to -3d, and
    -- DELETEs FACT_METERING_DAILY D-3 (above). The re-CALL of SP_LOAD_DAILY_FACTS below
    -- is the RE-COVER step, NOT a duplicate of the root load: it repopulates the metering
    -- rows this proc just DELETEd AND re-reads the D-2/D-3 window (rewound watermark) for
    -- late-arriving ACCOUNT_USAGE data the 1-day-back root run cannot see. Dropping it
    -- would leave a permanent metering gap. Redundant? NO -> kept.
    --
    -- TODO(#7 staging-swap): the DELETE(D-2/D-3)-then-reload here is NOT atomic -- a child
    -- failure between the DELETEs (above) and these CALLs leaves a transient gap until the
    -- next nightly run re-covers. The robust fix (build-into-staging + atomic SWAP, or one
    -- transaction per fact family wrapping delete+reload) is DEFERRED: the reloaders are
    -- separate procs that contain their OWN BEGIN TRANSACTION/COMMIT (SP_LOAD_DAILY_FACTS,
    -- SP_LOAD_QH_EXTRACT) and DDL that autocommits (SP_LOAD_MARTS_V27 does CREATE OR
    -- REPLACE TEMPORARY TABLE), so they cannot run inside a single reconcile-owned
    -- transaction without committing it early or breaking the per-source watermark rewind.
    -- A correct deferral beats a broken reconcile; the #9 verdict below makes any
    -- mid-reconcile child failure LOUD so a gap never passes silently.
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
    -- V064 #6 RUN SCOPING: count ONLY this reconcile's OWN child-loader failures. The
    -- prior filter counted every '%_failed%' row account-wide in the run window, so a
    -- CONCURRENT AlertScan 'rule_block_failed', a NotifyWebhook 'route_send_failed', an
    -- ObjectCost load, or any app-page error logged in the same minutes was mis-attributed
    -- to the reconcile and made an OK run report WITH ERRORS. APP_ERROR_LOG has NO
    -- session/query-tag column to fence on (its columns are LOGGED_AT, PAGE, ERROR_TYPE,
    -- ERROR_MESSAGE, CONTEXT, ROLE_NAME -- see V001), and the children log without a run
    -- id, so the tightest CORRECT filter is PAGE. The five children that swallow+log write
    -- exactly three pages: ExtractLoader (SP_LOAD_QH_EXTRACT + its inner
    -- SP_LOAD_CLOUD_SVC_MART), DailyFacts (SP_LOAD_DAILY_FACTS), MartLoader
    -- (SP_LOAD_MARTS_V27). SP_LOAD_HOURLY_FACTS + SP_LOAD_OPS_DIAG do NOT swallow -- a
    -- failure there aborts THIS proc and FAILs the task run directly. (ExtractLoader can
    -- also be written by the concurrent hourly extract task; that is still a real loader
    -- failure in the window, not a cross-subsystem mis-attribution, so counting it is
    -- acceptable -- the secondary per-CALL verdict check above catches the reconcile's own
    -- extract call regardless.)
    SELECT COUNT(*) INTO :logged_fails
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE LOGGED_AT >= :recon_start
      AND PAGE IN ('ExtractLoader', 'DailyFacts', 'MartLoader')
      AND (ERROR_TYPE ILIKE '%_failed%' OR ERROR_TYPE ILIKE '%WITH ERRORS%');
    IF (fails > 0 OR logged_fails > 0) THEN
        RETURN 'RECONCILE WITH ERRORS: ' || :logged_fails || ' loader failure(s) logged'
            || IFF(:fails > 0, ' + ' || :fails || ' child verdict(s) non-success', '')
            || ' (metering + full-retention marts 3 days, extract-fed marts 2); inspect APP_ERROR_LOG';
    END IF;
    RETURN 'RECONCILE OK - nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2); no child loader failures logged';""", 1),
        ("""    -- Pull the watermarks back so the loaders re-cover the window.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'HOURLY_FACTS', 'DAILY_FACTS');""",
         """    -- Pull the watermarks back so the loaders re-cover the window.
    -- V064 rec7: the daily loader now keeps FOUR per-source marks, not one
    -- shared DAILY_FACTS mark -- rewind all four here or SP_LOAD_DAILY_FACTS
    -- reads a current mark and the nightly daily re-coverage silently no-ops.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'HOURLY_FACTS',
                      'FACT_METERING_DAILY', 'FACT_TASK_DAILY',
                      'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY');""", 1),
    ]),
    # ---- rec20-alert: SP_ALERT_SCAN_DAILY trailing-30-complete-day burn (V062) ----
    'SP_ALERT_SCAN_DAILY': ('V062__loader_robustness_alert_split_webhook.sql', [
        ("""                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / 30
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())) AS DAILY_BURN""",
         """                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN""", 1),
        ("-- threshold days at the trailing 30-day burn rate. Weekly-recurring",
         "-- threshold days at the trailing 30 complete-day burn rate. Weekly-recurring", 1),
        ("' contracted credits; trailing 30d burn ' || ROUND(p.DAILY_BURN, 1) ||",
         "' contracted credits; trailing 30 complete-day burn ' || ROUND(p.DAILY_BURN, 1) ||", 1),
    ]),
}

GUARDS = {
    'SP_LOAD_DAILY_FACTS': [
        "BEGIN TRANSACTION",          # the 3 per-table transaction wraps survive
        "SOURCE_FRESHNESS_STATE",     # freshness MERGE untouched
    ],
    'SP_NIGHTLY_RECONCILE': [
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();",
    ],
    'SP_ALERT_SCAN_DAILY': [
        "COST_CONTRACT_BREACH",
    ],
}

daily = derive("SP_LOAD_DAILY_FACTS")
reconcile = derive("SP_NIGHTLY_RECONCILE")
alertscan = derive("SP_ALERT_SCAN_DAILY")

# ---- correctness assertions on the generated procs ----
# rec8 webhook
assert WEBHOOK.count("fits_ids ARRAY;") == 1, "rec8 fits_ids declared once"
assert WEBHOOK.count("ARRAY_AGG(f.EVENT_ID)") == 1, "rec8 capture-once per batch"
assert WEBHOOK.count("ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)") == 3, "rec8 message + ledger + NOTIFIED_AT share the frozen set"
assert WEBHOOK.count("ORDER BY f.RAISED_AT ASC") == 1 and WEBHOOK.count("ORDER BY e.RAISED_AT ASC") == 2, "rec8 oldest-first everywhere"
assert "DESC" not in WEBHOOK, "rec8 no newest-first ordering left"
assert "LOOP" in WEBHOOK and "END LOOP;" in WEBHOOK, "rec8 drain loop"
assert "max_batches INT DEFAULT 6" in WEBHOOK, "rec8 bounded batches"
assert WEBHOOK.count("EXIT;") == 4, "rec8 four loop exits (empty, empty-msg, send-fail, max)"
assert "undelivered_expired" in WEBHOOK, "rec8 loud expired tail survives"
# #27 per-route expiry: keyed on event+route delivery, NOT on NOTIFIED_AT.
# (The send-path back-compat NOTIFIED_AT stamp still uses NOTIFIED_AT IS NULL; only
# the expired tail -- after "Loud, not silent" -- must no longer key on it.)
_wh_tail = WEBHOOK.split("Loud, not silent", 1)[1]
assert "NOTIFIED_AT IS NULL" not in _wh_tail, "#27 expiry tail no longer keys on the NOTIFIED_AT predicate"
assert ("NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n"
        "                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r2.ROUTE_ID)") in WEBHOOK, \
    "#27 expiry is per (event,route) via NOT EXISTS a delivery for THAT route"
assert "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2\n      ON r2.ENABLED" in WEBHOOK, "#27 expiry enumerates the eligible routes"
# #40 log-inflation guard: at most one 'undelivered_expired' row per pair per 24h
assert ("NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG a\n"
        "                      WHERE a.ERROR_TYPE = 'undelivered_expired'") in WEBHOOK, "#40 gate on a prior expired row"
assert "AND a.LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())" in WEBHOOK, "#40 gate window is 24h"
assert WEBHOOK.count("'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID") == 2, "#40 same (event,route) key written + matched"
# #26 single-flight lease: acquire at entry, bail if held, release at exit.
# V064 #9 adds a THIRD reference: the outer-handler fenced release (acquire + normal
# release + outer-handler release).
assert WEBHOOK.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3, "#26/#9 lease acquire + normal release + outer-handler release"
assert "HELD = FALSE OR ACQUIRED_AT < DATEADD('hour', -1, CURRENT_TIMESTAMP())" in WEBHOOK, "#26 stale-lease reclaim"
assert "another SP_NOTIFY_WEBHOOK run holds the sender lease" in WEBHOOK, "#26 bails when not acquired"
# #26 FENCE (review): the release must be scoped to the holding session, or a stalled
# run can clear a successor's reclaimed lease and permit concurrent senders.
assert ("WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'\n"
        "       AND HOLDER = CURRENT_SESSION();") in WEBHOOK, "#26 release fenced to CURRENT_SESSION()"
# #9 OUTER LEASE HANDLER: an unhandled mid-proc error must fenced-release + log + RE-RAISE
# (not leave HELD=TRUE until the 1h reclaim, and not silently swallow the failure).
assert WEBHOOK.count("HOLDER = CURRENT_SESSION();") == 2, "#9 both releases (normal + outer handler) are fenced"
assert WEBHOOK.rstrip().endswith("END;\n$$;") and "EXCEPTION\n    -- V064 #9 OUTER LEASE HANDLER" in WEBHOOK, "#9 outer EXCEPTION wraps the whole body"
assert "'NotifyWebhook', 'webhook_run_failed', :emsg," in WEBHOOK, "#9 logs the unhandled error"
assert WEBHOOK.count("RAISE;") == 1, "#9 exactly one re-raise (in the outer handler)"
assert "        RAISE;\nEND;\n$$;" in WEBHOOK, "#9 handler re-raises so the task run FAILs"
# #10-slice: undelivered CRITICALs get a 7d send window (24h floor kept for the rest);
# oldest-first drain + per-route NOT EXISTS + expiry watchdog all intact.
assert ("AND e.RAISED_AT >= CASE WHEN e.SEVERITY = 'CRITICAL'\n"
        "                                          THEN DATEADD('day', -7, CURRENT_TIMESTAMP())\n"
        "                                          ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END") in WEBHOOK, \
    "#10 CRITICAL 7d send window, 24h for the rest"
# #9 reconcile: the authoritative failure signal is the APP_ERROR_LOG '%_failed%' count
# during the run (children swallow arm failures + return benign strings), with the per-CALL
# return check as a secondary catch for the two machine-verdict children.
assert reconcile.count("SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));") == 5, "#9 one verdict capture per child CALL"
assert reconcile.count("fails := fails + 1;") == 5, "#9 count non-success per child (secondary)"
assert "SELECT COUNT(*) INTO :logged_fails" in reconcile, "#9 counts logged loader failures"
assert "WHERE LOGGED_AT >= :recon_start" in reconcile, "#9 scoped to this run"
assert "ERROR_TYPE ILIKE '%_failed%'" in reconcile, "#9 matches the children's failure tokens"
# #6 RUN SCOPING: the failure count is fenced to the reconcile's own child-loader pages,
# so a concurrent AlertScan/NotifyWebhook/ObjectCost/app failure is not mis-attributed.
assert "AND PAGE IN ('ExtractLoader', 'DailyFacts', 'MartLoader')" in reconcile, "#6 count scoped to the reconcile's own loader pages"
# #7 DEFERRAL + #4-drop KEEP: documented in-proc (delete+reload atomicity deferred; the
# daily re-call is the re-cover mechanism, not a redundant duplicate of the root load).
assert "TODO(#7 staging-swap)" in reconcile, "#7 atomicity deferral is flagged in-proc"
assert "V064 #4-drop ANALYSIS (KEEP -- verified NOT redundant)" in reconcile, "#4-drop kept + reasoned in-proc"
assert "IF (fails > 0 OR logged_fails > 0) THEN" in reconcile, "#9 either signal flags the run"
assert "RECONCILE WITH ERRORS: ' || :logged_fails ||" in reconcile, "#9 machine-readable failure verdict"
assert "RETURN 'RECONCILE OK" in reconcile, "#9 machine-readable success verdict"
assert reconcile.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_") == 5, "#9 the five child CALLs are unchanged in count"
# rec7 daily-facts
for src in ("FACT_METERING_DAILY", "FACT_TASK_DAILY", "FACT_LOGIN_DAILY", "FACT_STORAGE_DAILY"):
    adv = f"USING (SELECT '{src}' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s"
    assert daily.count(adv) == 1, f"rec7 one advance MERGE per source {src}"
assert "'DAILY_FACTS'" not in daily, "rec7 the single DAILY_FACTS mark is gone from the loader"
assert daily.count("BEGIN TRANSACTION") == 3 and daily.count("ROLLBACK") == 3, "rec7 per-table isolation preserved"
assert daily.count("failed_any := TRUE;") == 4, "rec7/B34 flag set in the 3 sibling handlers + the guarded metering mark"
assert daily.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS") == 4, "rec7 four per-source advances"
assert "IF (NOT failed_any) THEN" not in daily, "rec7 the single gated advance is gone"
assert ":lo_short::DATE" not in daily, "rec7 all lo_short references retargeted per-source"
# rec7 reconcile
assert "'FACT_METERING_DAILY', 'FACT_TASK_DAILY'" in reconcile, "rec7 reconcile rewinds the 4 new keys"
assert "'DAILY_FACTS'" not in reconcile, "rec7 reconcile no longer rewinds the retired key"
# rec20-alert
assert "NULLIF(COUNT(DISTINCT DAY), 0)" in alertscan, "rec20a complete-day divisor"
assert "AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN" in alertscan, "rec20a today excluded"
assert "SUM(CREDITS_BILLED), 0) / 30" not in alertscan, "rec20a the partial-day /30 form is gone"

out = f"""-- V064__webhook_drain_watermarks_alert_burn_telemetry.sql
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
{WEBHOOK}
-- >>> derived:SP_LOAD_DAILY_FACTS  (rec7 per-source watermarks - SMOKE TEST REQUIRED)
{daily}
-- >>> derived:SP_NIGHTLY_RECONCILE  (rec7 rewind the 4 per-source daily marks)
{reconcile}
-- >>> derived:SP_ALERT_SCAN_DAILY  (rec20-alert trailing-30-complete-day burn)
{alertscan}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 64 AS VERSION,
       'Webhook oldest-first bounded drain (rec8: batches drain the backlog forward so the oldest alerts stop starving past the 24h window; capture-once per batch, shared expired eligibility - owner smoke test) + per-source daily watermarks (rec7: SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE, one table failure holds only its own mark) + contract-breach trailing-30-complete-day burn (rec20-alert, was /30 over a partial day) + APP_QUERY_TELEMETRY SAMPLE_PROB/QUERY_ID (rec18).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 64);
"""
# seed + schema DDL land in the migration body (outside the procs)
assert out.count("WHERE SOURCE = 'DAILY_FACTS'") == 2, "rec7 cold-start seed reads the retained mark"
assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS (SOURCE, WM_TS)\nSELECT s.SOURCE," in out, "rec7 seed present"
assert out.count("ALTER TABLE DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY") == 2, "rec18 two ADD COLUMNs"

target = Path(os.environ.get("V064_OUT") or (MIG / "V064__webhook_drain_watermarks_alert_burn_telemetry.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
