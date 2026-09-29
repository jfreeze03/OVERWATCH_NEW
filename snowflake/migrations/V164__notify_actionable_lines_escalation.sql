-- V164__notify_actionable_lines_escalation.sql
--
-- Next-Fifty #40 (owner decisions 2026-09-29): Teams lines you can act on, and a one-time escalation of a
-- CRITICAL nobody acknowledged.
--
-- WHY: a Teams line said only '[SEV] <title>' -- no company, no detail, no event id to find it by -- and a
-- CRITICAL was posted once and then sat unanswered: nothing re-paged it and nothing reached anyone off Teams.
--
--   + ALERT_EVENTS.ESCALATED_AT TIMESTAMP_NTZ (ADD COLUMN IF NOT EXISTS): the once-per-event escalation marker.
--   + SETTINGS ESCALATE_AFTER_MIN '120' (0 or not a number = escalation off) and ESCALATE_EMAIL_INTEGRATION
--     'OVERWATCH_EMAIL' ('' = no email leg), WHEN NOT MATCHED only: a value set before the apply is kept.
--   ~ SP_NOTIFY_WEBHOOK re-derived from V064 (its current definer; V070, V112, V157 and V160 only mention it),
--     byte-identical except:
--       N1 13 escalation variables and cursor c2 (the enabled routes) in the DECLARE;
--       N2 every line reads '[SEV] <title, 140> | <company> | <detail, one line, 100> | event <EVENT_ID>' (ASCII),
--          identical in the 3000-char fit and in the LISTAGG, so the fit still equals what is sent (a worst-case
--          escaped line is under 900 chars, so a batch always holds at least one event);
--       N3 the escalation pass, inside the sender lease, after the drain and before the expired tail: a CRITICAL
--          still OPEN with no ACK_AT, not in an incident a human acknowledged, mitigated or closed AFTER the alert
--          joined it (INCIDENT_MEMBERS.LINKED_AT; V154's machine auto-mitigate, MITIGATED_BY =
--          SP_INCIDENT_AUTODECLARE, does not count), never snoozed (an ALERT_AUDIT SNOOZE row, or a V117
--          carry-forward of a snooze onto it: a SNOOZE_SUPPRESSED predecessor with the same band-stripped key),
--          raised inside the 7-day CRITICAL send window, whose rule still exists, and first notified
--          (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN+ minutes ago escalates ONCE: re-posted to every
--          enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION with the
--          notification-integration send, i.e. to that integration's DEFAULT_RECIPIENTS (no address is stored in
--          OVERWATCH). With the email leg off, only an event some enabled route delivered is eligible.
--          Capture-once (route-delivered events first, then oldest first, 3000 escaped chars -- an email-only
--          event never holds the batch while the email fails); right after each channel's send succeeds, the
--          ESCALATED_AT stamp for the ids it took (so a later error never re-posts them), then one ALERT_AUDIT
--          'ESCALATE' row per event the run stamped (every channel failing = nothing stamped, retried next run
--          inside the 7 days). Its own handler logs escalation_failed and never re-raises, so the expired tail,
--          the lease release and the RETURN still run. A re-post failure logs route_send_failed with the V064
--          CONTEXT prefix (the Native delivery card attributes it to its route); an email failure logs
--          escalation_email_failed;
--       N4 the RETURN adds '<n> CRITICAL(s) escalated' and a note (off / email failed / pass failed).
--
-- !! OWNER SMOKE TEST (like V064): the notification send, the ARRAY handling and the nested cursor loop are
--    runtime-only; RUN_NEXT PART B V164.3 / V164.4 prove them. The email leg needs DEFAULT_RECIPIENTS on the
--    integration and USAGE on it for the proc owner (PREFLIGHT P164.1).
-- COST: one SETTINGS read and one small capture SELECT on ALERT_EVENTS per hourly TASK_ALERT_NOTIFY run, plus sends
--    only when something is eligible: a few compile-seconds an hour. No new table, task, view or warehouse.
-- LATENCY: hourly (TASK_ALERT_NOTIFY runs in the chain off TASK_LOAD_HOURLY), so with 120 minutes an escalation
--    goes out about 120-185 minutes after the first notification.
-- THROUGHPUT: lines are about 3x longer, so a 3000-char batch holds about 8-13 events instead of about 40;
--    max_batches stays 6 (PREFLIGHT P164.3 counts the runs that would have spilled to the next hour).
-- FIRST RUN: every OPEN, never-acknowledged CRITICAL of the last 7 days first notified 120+ minutes ago escalates on
--    the next hourly run (one 3000-char batch per run; the rest follow). PREFLIGHT P164.2 lists them: acknowledge or
--    resolve stale ones first, or seed SETTINGS ('ESCALATE_AFTER_MIN', '0') before the apply (the MERGE keeps it).
-- ROLLBACK: soft -- set SETTINGS ESCALATE_AFTER_MIN to '0'; hard -- re-run ONLY V064's SP_NOTIFY_WEBHOOK CREATE
--    (V064 lines 74-351; never the whole V064 file, which would also roll back three other procs), which also
--    restores the old line format. The column and the settings can stay.
-- Nothing runs at apply time (no CALL: a hand CALL of the notifier can page and email).
-- Apply AFTER V163. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20164, 'V164 requires V163 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 163) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The once-per-event escalation marker (NULL = never escalated). Additive; existing rows read NULL.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS ESCALATED_AT TIMESTAMP_NTZ;

-- Escalation knobs (Admin > Settings). WHEN NOT MATCHED only: a value set before the apply (for example
-- ESCALATE_AFTER_MIN '0' to start with escalation off) is never overwritten. The integration is a NAME, not an
-- address: the email goes to that integration's DEFAULT_RECIPIENTS.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ESCALATE_AFTER_MIN', '120'),
        ('ESCALATE_EMAIL_INTEGRATION', 'OVERWATCH_EMAIL')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- >>> derived:SP_NOTIFY_WEBHOOK  (from V064; actionable lines + one-time CRITICAL escalation pass, Next-Fifty #40, V164)
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
    esc_after_s VARCHAR;    -- V164 #40: SETTINGS.ESCALATE_AFTER_MIN (absent = '120'; 0 / not a number = off)
    esc_after NUMBER DEFAULT 0;
    esc_email VARCHAR;      -- V164 #40: SETTINGS.ESCALATE_EMAIL_INTEGRATION (absent = 'OVERWATCH_EMAIL'; '' = no email)
    esc_now TIMESTAMP_NTZ;  -- V164 #40: one frozen clock for the whole escalation pass
    esc_ids ARRAY;          -- V164 #40: the frozen escalation set (capture-once, the B9 invariant)
    r_esc_ids ARRAY;        -- V164 #40: the part of esc_ids THIS route already delivered
    esc_ok ARRAY;           -- V164 #40: ids at least one channel accepted (audited + stamped)
    esc_msg VARCHAR;
    esc_sent BOOLEAN DEFAULT FALSE;  -- V164 #40: this channel's send succeeded (stamp right after it)
    esc_routes INT DEFAULT 0;
    esc_emailed BOOLEAN DEFAULT FALSE;
    escalated INT DEFAULT 0;
    esc_note VARCHAR DEFAULT '';
    c1 CURSOR FOR
        SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME,
               COALESCE(r.COMPANY_FILTER, 'ALL') AS COMPANY_FILTER
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
    c2 CURSOR FOR           -- V164 #40: enabled routes, for the escalation re-post
        SELECT r.ROUTE_ID, r.INTEGRATION_NAME
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
                           '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID,
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
            SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
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

    -- V164 #40: CRITICAL ESCALATION PASS (Next-Fifty #40, owner decision 2026-09-29). A CRITICAL still OPEN
    -- and never acknowledged (no ACK_AT; not in an incident a human acknowledged, mitigated or closed after
    -- the alert joined it -- the V154 machine auto-mitigate does not count; never snoozed -- an ALERT_AUDIT
    -- SNOOZE row, or a V117 carry-forward of a snooze onto it) whose first notification (NOTIFIED_AT, or
    -- RAISED_AT when no route ever took it) is ESCALATE_AFTER_MIN+ minutes old escalates ONCE: re-posted to
    -- every enabled route that already delivered it (the same Teams route), and emailed through
    -- ESCALATE_EMAIL_INTEGRATION, i.e. to that integration's DEFAULT_RECIPIENTS -- no address is written here.
    -- With the email leg off ('') only an event some enabled route delivered is eligible, and route-delivered
    -- events always fill the batch first, so an email-only one never holds it (even while the email fails).
    -- Capture-once: the ids are frozen (route-delivered first, then oldest first) into esc_ids within 3000
    -- escaped chars (the V063 B9 invariant), and every message, stamp and audit row derives from that one set.
    -- Send, then stamp at once: right after each channel's send succeeds, ESCALATED_AT (= the frozen pass clock)
    -- is set for the ids it took, so an error later in the pass can never re-post them next hour; an
    -- all-channel failure stamps nothing and retries next run inside the 7-day CRITICAL window (at-least-once,
    -- like the drain). Then one ALERT_AUDIT 'ESCALATE' row per event this run stamped. Inside the sender
    -- lease, so two runs never double-escalate. Isolated: an error here is logged (escalation_failed) and never
    -- re-raised -- the deliveries above, the expired tail, the lease release and the RETURN below still run.
    BEGIN
        SELECT COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL)), '120'),
               COALESCE(MAX(IFF(KEY = 'ESCALATE_EMAIL_INTEGRATION', VALUE, NULL)), 'OVERWATCH_EMAIL')
          INTO :esc_after_s, :esc_email
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
         WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION');
        esc_after := COALESCE(TRY_TO_NUMBER(TRIM(:esc_after_s)), 0);
        IF (esc_after <= 0) THEN
            esc_note := '; escalation off (ESCALATE_AFTER_MIN)';
        ELSE
            esc_now := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
            esc_ok := ARRAY_CONSTRUCT();
            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.EMAIL_ONLY, f.RAISED_AT ASC, f.EVENT_ID)
              INTO :esc_ids
            FROM (
                SELECT e.EVENT_ID, e.RAISED_AT, IFF(rd.EVENT_ID IS NULL, 1, 0) AS EMAIL_ONLY,
                       SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                           'ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID,
                           CHR(92), CHR(92) || CHR(92)),
                           CHR(34), CHR(92) || CHR(34)),
                           CHR(10), CHR(92) || 'n'),
                           CHR(13), ''),
                           CHR(9),  CHR(92) || 't')) + 2)
                         OVER (ORDER BY IFF(rd.EVENT_ID IS NULL, 1, 0), e.RAISED_AT ASC, e.EVENT_ID
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                LEFT JOIN (SELECT DISTINCT d.EVENT_ID
                           FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                           JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                             ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED) rd
                  ON rd.EVENT_ID = e.EVENT_ID
                WHERE e.SEVERITY = 'CRITICAL'
                  AND e.STATUS = 'OPEN'
                  AND e.ACK_AT IS NULL
                  AND e.ESCALATED_AT IS NULL
                  AND e.RAISED_AT >= DATEADD('day', -7, :esc_now)
                  AND COALESCE(e.NOTIFIED_AT, e.RAISED_AT) <= DATEADD('minute', -1 * :esc_after, :esc_now)
                  AND NOT EXISTS (SELECT 1
                                  FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                  JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
                                  WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID
                                    AND (i.ACK_AT >= m.LINKED_AT
                                         OR (i.STATUS = 'MITIGATED' AND i.MITIGATED_AT >= m.LINKED_AT
                                             AND COALESCE(i.MITIGATED_BY, '') <> 'SP_INCIDENT_AUTODECLARE')
                                         OR (i.STATUS = 'RESOLVED' AND i.RESOLVED_AT >= m.LINKED_AT)))
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a
                                  WHERE a.EVENT_ID = e.EVENT_ID AND a.ACTION = 'SNOOZE')
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
                                  WHERE s.RULE_ID = e.RULE_ID
                                    AND s.RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'
                                    AND s.RAISED_AT < e.RAISED_AT
                                    AND s.RESOLVED_AT >= e.RAISED_AT
                                    AND IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'
                                              AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL,
                                            LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11), s.DEDUPE_KEY)
                                        = IFF(SUBSTR(e.DEDUPE_KEY, -11, 1) = '|'
                                              AND TRY_TO_DATE(RIGHT(e.DEDUPE_KEY, 10)) IS NOT NULL,
                                            LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 11), e.DEDUPE_KEY))
                  AND (COALESCE(TRIM(:esc_email), '') <> '' OR rd.EVENT_ID IS NOT NULL)
            ) f
            WHERE f.CUM_LEN <= 3000;

            IF (esc_ids IS NOT NULL AND ARRAY_SIZE(:esc_ids) > 0) THEN
                -- Teams leg: re-post only what THIS route already delivered (the same route that paged).
                FOR erec IN c2 DO
                    r_route_id := erec.ROUTE_ID;
                    r_integration := erec.INTEGRATION_NAME;
                    SELECT ARRAY_AGG(DISTINCT d.EVENT_ID)
                      INTO :r_esc_ids
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                    WHERE d.ROUTE_ID = :r_route_id
                      AND ARRAY_CONTAINS(d.EVENT_ID::VARIANT, :esc_ids);
                    IF (r_esc_ids IS NOT NULL AND ARRAY_SIZE(:r_esc_ids) > 0) THEN
                        SELECT LISTAGG('ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
                               WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
                          INTO :esc_msg
                        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                        WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :r_esc_ids);
                        -- the V064 JSON escape (backslash first, then quote, newline, CR, tab)
                        esc_msg := REPLACE(:esc_msg, CHR(92), CHR(92) || CHR(92));
                        esc_msg := REPLACE(:esc_msg, CHR(34), CHR(92) || CHR(34));
                        esc_msg := REPLACE(:esc_msg, CHR(10), CHR(92) || 'n');
                        esc_msg := REPLACE(:esc_msg, CHR(13), '');
                        esc_msg := REPLACE(:esc_msg, CHR(9),  CHR(92) || 't');
                        esc_sent := FALSE;
                        BEGIN
                            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                                    'OVERWATCH ESCALATION - CRITICAL unacknowledged ' || :esc_after || '+ min:'
                                    || CHR(92) || 'n' || LEFT(:esc_msg, 3000)),
                                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
                            esc_sent := TRUE;
                        EXCEPTION
                            WHEN OTHER THEN
                                emsg := SQLERRM;
                                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                                SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                                       'route ' || :r_route_id || ' integration ' || :r_integration ||
                                       ' - escalation re-post; the email leg is unaffected',
                                       CURRENT_ROLE();
                        END;
                        IF (esc_sent) THEN
                            -- stamp at once: nothing later in this pass can make the next run re-post these
                            esc_ok := ARRAY_CAT(:esc_ok, :r_esc_ids);
                            esc_routes := esc_routes + 1;
                            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                               SET ESCALATED_AT = :esc_now
                             WHERE e.ESCALATED_AT IS NULL
                               AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :r_esc_ids);
                            escalated := escalated + SQLROWCOUNT;
                        END IF;
                    END IF;
                END FOR;

                -- Email leg: every escalated id, to the integration's DEFAULT_RECIPIENTS (never an address here).
                IF (COALESCE(TRIM(:esc_email), '') <> '') THEN
                    SELECT LISTAGG('ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
                           WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
                      INTO :esc_msg
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ids);
                    esc_sent := FALSE;
                    BEGIN
                        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                                'OVERWATCH ESCALATION - CRITICAL alert(s) nobody acknowledged within '
                                || :esc_after || ' minutes:' || CHR(10) || CHR(10) || :esc_msg || CHR(10) || CHR(10)
                                || 'Acknowledge in OVERWATCH > Alerts > Open events. Each alert escalates once.'),
                            SNOWFLAKE.NOTIFICATION.INTEGRATION(TRIM(:esc_email)));
                        esc_sent := TRUE;
                    EXCEPTION
                        WHEN OTHER THEN
                            emsg := SQLERRM;
                            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                            SELECT 'NotifyWebhook', 'escalation_email_failed', :emsg,
                                   'integration ' || :esc_email || ' - ' || ARRAY_SIZE(:esc_ids) ||
                                   ' escalation(s) not emailed; check DEFAULT_RECIPIENTS and USAGE on the integration',
                                   CURRENT_ROLE();
                            esc_note := :esc_note || '; escalation email failed (APP_ERROR_LOG escalation_email_failed)';
                    END;
                    IF (esc_sent) THEN
                        esc_ok := ARRAY_CAT(:esc_ok, :esc_ids);
                        esc_emailed := TRUE;
                        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                           SET ESCALATED_AT = :esc_now
                         WHERE e.ESCALATED_AT IS NULL
                           AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ids);
                        escalated := escalated + SQLROWCOUNT;
                    END IF;
                END IF;

                -- Then the audit: one ESCALATE row per event THIS run stamped (ESCALATED_AT = the frozen
                -- clock), written after every send so the note carries the whole run's outcome.
                IF (ARRAY_SIZE(:esc_ok) > 0) THEN
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
                    SELECT e.EVENT_ID, 'ESCALATE',
                           'unacknowledged ' || :esc_after || '+ min; this run re-posted to ' || :esc_routes ||
                           ' route(s), email ' || IFF(:esc_emailed, 'sent', 'not sent'),
                           'SP_NOTIFY_WEBHOOK'
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    WHERE e.ESCALATED_AT = :esc_now
                      AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ok);
                END IF;
            END IF;
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'NotifyWebhook', 'escalation_failed', :emsg,
                   'escalation pass - the deliveries above are unaffected; retries next run',
                   CURRENT_ROLE();
            esc_note := '; escalation pass FAILED (APP_ERROR_LOG escalation_failed)';
    END;

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
           ' newly expired-undelivered (event,route) pair(s) flagged; ' || :escalated ||
           ' CRITICAL(s) escalated' || :esc_note;
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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 164 AS VERSION,
       'Next-Fifty #40: actionable Teams lines and a one-time CRITICAL escalation. SP_NOTIFY_WEBHOOK re-derived from V064, byte-identical except: every line reads [SEV] title (140) | company | detail (one line, 100) | event id, identical in the 3000-char fit and the LISTAGG (max_batches stays 6); and an escalation pass inside the sender lease, after the drain and before the expired tail. A CRITICAL still OPEN with no ACK_AT, not in an incident a human acknowledged, mitigated or closed after the alert joined it (the machine auto-mitigate does not count), never snoozed (an audit SNOOZE row or a V117 carry-forward), raised in the 7-day CRITICAL window, whose rule exists, and first notified (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN (120) minutes ago escalates once: re-posted to every enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION (OVERWATCH_EMAIL; that integration DEFAULT_RECIPIENTS, no address stored). Capture-once, route-delivered events first, then oldest first; right after each channel send succeeds, the new ALERT_EVENTS.ESCALATED_AT stamp for the ids it took, then one ALERT_AUDIT ESCALATE row per event stamped; every channel failing retries next run. Isolated: escalation_failed is logged and never re-raised; a re-post failure logs route_send_failed, an email failure escalation_email_failed. The RETURN adds the escalated count. Seeds SETTINGS ESCALATE_AFTER_MIN 120 (0 = off) and ESCALATE_EMAIL_INTEGRATION OVERWATCH_EMAIL (blank = no email) WHEN NOT MATCHED only. No task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 164);
