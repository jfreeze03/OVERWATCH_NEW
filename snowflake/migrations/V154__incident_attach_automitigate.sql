-- V154__incident_attach_automitigate.sql
--
-- Incident loop (Next-Fifty #12b). Two gaps in SP_INCIDENT_AUTODECLARE:
--   * A later unlinked OPEN/ACK CRITICAL whose family ALREADY has an OPEN/MITIGATED incident in
--     the same company is dropped by the family-already-open guard (V099) and was never linked
--     anywhere, so incident timelines, RCA and member counts silently missed it. The new [attach]
--     arm links it to that incident. Its candidate set is exactly the guard's witness set, so every
--     such critical is either declared or attached. It never creates an incident or moves a STATUS.
--   * Nothing ever wrote STATUS = 'MITIGATED'. The new [auto-mitigate] sweep moves an OPEN incident
--     forward-only to MITIGATED once EVERY ALERT member has been RESOLVED for >= 1h (a member whose
--     event row is gone counts as live; a SUPERSEDED / SNOOZE_SUPPRESSED member blocks while a
--     same-rule same-company successor is still OPEN/ACK/SNOOZED). MITIGATED_AT = the last member
--     resolve, not the sweep clock. Never RESOLVED: closing stays human so the root cause is
--     captured; the app lists these as 'ready to close'. OWNER / ACK_AT are never written here.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V099 (its current definition; V098 re-link guard + V099
-- company scope kept), byte-identical except three deltas: DECLARE +attached/mitigated/emsg, the two
-- exception-isolated blocks inserted after the member INSERT, and a RETURN that reports the attach +
-- mitigate counts. Adds INCIDENTS.MITIGATED_BY VARCHAR(200) (machine provenance: the sweep stamps
-- 'SP_INCIDENT_AUTODECLARE'; NULL = a human Mark mitigated). ADD COLUMN IF NOT EXISTS is idempotent.
--
-- TOGGLE: both new arms sit AFTER the INCIDENT_AUTO_DECLARE_CRITICAL check (V099), so when
-- auto-declare is switched off in Settings the proc returns 'auto-declare off' before them -- no
-- attach and no auto-mitigate either. The setting defaults to TRUE when absent (probe C3 reads it).
--
-- FIRST RUN: OPEN incidents whose member alerts have all long since resolved move to MITIGATED on
-- the first run (MITIGATED_AT = their historical last resolve), so the 90d time-to-mitigate median
-- includes them until they age out. TASK_INCIDENT_AUTODECLARE runs AFTER TASK_LOAD_HOURLY, beside the
-- alert-scan chain, so attach / mitigate see the previous hour's alert state (MITIGATED_AT uses the
-- true resolve time, so the metric is unaffected).
--
-- No task change, no backfill and no CALL in this file: the owner runs one CALL in the RUN_NEXT
-- PART B verify grid (the same work the hourly task does; it raises no alert and sends no email).
-- Apply AFTER V153. Rollback: re-run V099's SP_INCIDENT_AUTODECLARE definition (MITIGATED_BY may stay).
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20154, 'V154 requires V153 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 153) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Machine provenance for the [auto-mitigate] sweep (owner decision O-5). NULL = a human moved it.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.INCIDENTS ADD COLUMN IF NOT EXISTS MITIGATED_BY VARCHAR(200);

-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V099; + [attach] arm + [auto-mitigate] sweep, Next-Fifty #12b)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    enabled VARCHAR;
    made INT DEFAULT 0;
    attached INT DEFAULT 0;
    mitigated INT DEFAULT 0;
    emsg VARCHAR;
BEGIN
    SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL';
    IF (UPPER(:enabled) <> 'TRUE') THEN
        RETURN 'auto-declare off';
    END IF;

    CREATE OR REPLACE TEMPORARY TABLE _OW_AUTODECL AS
    WITH crit AS (
        SELECT e.EVENT_ID, e.COMPANY, e.SEVERITY, e.TITLE, e.RAISED_AT,
               SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE UPPER(e.SEVERITY) = 'CRITICAL'
          AND e.STATUS IN ('OPEN', 'ACK')
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
    )
    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,
           MAX_BY(TITLE, RAISED_AT) AS TITLE,
           MIN(RAISED_AT) AS FIRST_TS
    FROM crit c
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT'
          AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND i.COMPANY = c.COMPANY
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY
    )
    GROUP BY FAMILY, COMPANY;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, STARTED_AT,
         ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT INCIDENT_ID, LEFT('Auto: ' || TITLE, 300), 'CRITICAL', 'OPEN', COMPANY,
           CURRENT_TIMESTAMP(), FIRST_TS, 'UNKNOWN', 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT d.INCIDENT_ID, 'ALERT', e.EVENT_ID, e.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL d
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
      ON e.COMPANY = d.COMPANY
     AND UPPER(e.SEVERITY) = 'CRITICAL'
     AND e.STATUS IN ('OPEN', 'ACK')
     AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY
     AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                     WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);

    -- [attach] V154 (Next-Fifty #12b): a later unlinked OPEN/ACK CRITICAL whose family ALREADY has an
    -- OPEN/MITIGATED incident in the SAME company is skipped by the family-already-open guard above
    -- (V099) and, before V154, was never linked anywhere -- timelines, RCA and member counts silently
    -- missed it. Link it to that incident instead. The candidate set is EXACTLY the guard's witness
    -- set (same company, an ALERT member of the same family, OPEN/MITIGATED), so every such critical
    -- is either declared above or attached here. One target per event: an incident already holding
    -- a member of the SAME entity (DEDUPE_KEY field 2) wins, then the newest DETECTED_AT, then
    -- INCIDENT_ID (deterministic). Never creates an incident and never changes a STATUS -- a re-fire
    -- on a MITIGATED incident shows as a live member, so it simply stops being 'ready to close'.
    -- Isolated: a failure logs and leaves the declare above intact.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
            (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
        SELECT t.INCIDENT_ID, 'ALERT', t.EVENT_ID, t.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
        FROM (
            SELECT e.EVENT_ID, e.RAISED_AT, i.INCIDENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i
              ON i.COMPANY = e.COMPANY
             AND i.STATUS IN ('OPEN', 'MITIGATED')
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
              ON m.INCIDENT_ID = i.INCIDENT_ID
             AND m.MEMBER_KIND = 'ALERT'
            JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a
              ON a.EVENT_ID = m.REF_ID
             AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1)
                 = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)
            WHERE UPPER(e.SEVERITY) = 'CRITICAL'
              AND e.STATUS IN ('OPEN', 'ACK')
              AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                              WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID)
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.EVENT_ID
                ORDER BY IFF(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)
                             = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2), 0, 1),
                         i.DETECTED_AT DESC, i.INCIDENT_ID) = 1
        ) t;
        attached := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_attach_failed', :emsg,
                   'V154 attach arm - the declare above is unaffected', CURRENT_ROLE();
    END;

    -- [auto-mitigate] V154 (Next-Fifty #12b): an OPEN incident whose EVERY ALERT member is RESOLVED
    -- (a member whose event row is gone counts as NOT resolved) moves forward-only to MITIGATED --
    -- never to RESOLVED: closing stays human so the root cause is captured (the app surfaces these
    -- as 'ready to close'). A member closed as SUPERSEDED / SNOOZE_SUPPRESSED is a machine hand-off
    -- to a successor, so it blocks while a same-rule same-company event raised at/after it is still
    -- OPEN/ACK/SNOOZED. >=1h dwell on the LAST member resolve (anti-flap). MITIGATED_AT = when the
    -- last member resolved (not this sweep's clock), so time-to-mitigate is not inflated by the dwell
    -- or the hourly cadence; MITIGATED_BY names the machine (a human Mark mitigated leaves it NULL).
    -- OWNER / ACK_AT are never touched here (MTTA stays a human number). Isolated like [attach].
    BEGIN
        CREATE OR REPLACE TEMPORARY TABLE _OW_INC_MITIGATE AS
        WITH live AS (
            SELECT RULE_ID, COMPANY, MAX(RAISED_AT) AS LAST_LIVE_AT
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')
            GROUP BY RULE_ID, COMPANY
        )
        SELECT i.INCIDENT_ID,
               GREATEST(MAX(e.RESOLVED_AT), MAX(i.DETECTED_AT)) AS MITIGATED_TS
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          ON m.INCIDENT_ID = i.INCIDENT_ID
         AND m.MEMBER_KIND = 'ALERT'
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          ON e.EVENT_ID = m.REF_ID
        LEFT JOIN live l
          ON l.RULE_ID = e.RULE_ID
         AND l.COMPANY = e.COMPANY
        WHERE i.STATUS = 'OPEN'
        GROUP BY i.INCIDENT_ID
        HAVING COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED') = 0
           AND COUNT_IF(COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED')
                        AND l.LAST_LIVE_AT >= e.RAISED_AT) = 0
           AND MAX(e.RESOLVED_AT) <= DATEADD('hour', -1, CURRENT_TIMESTAMP());

        UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS i
           SET STATUS = 'MITIGATED',
               MITIGATED_AT = r.MITIGATED_TS,
               MITIGATED_BY = 'SP_INCIDENT_AUTODECLARE',
               UPDATED_AT = CURRENT_TIMESTAMP()
          FROM _OW_INC_MITIGATE r
         WHERE i.INCIDENT_ID = r.INCIDENT_ID
           AND i.STATUS = 'OPEN';
        mitigated := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_mitigate_failed', :emsg,
                   'V154 auto-mitigate sweep - declare/attach above unaffected', CURRENT_ROLE();
    END;

    SELECT COUNT(*) INTO :made FROM _OW_AUTODECL;
    RETURN 'auto-declared ' || :made || ' incident(s); attached ' || :attached || ' later critical(s); auto-mitigated ' || :mitigated || ' incident(s)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 154 AS VERSION,
       'Incident loop (Next-Fifty #12b): SP_INCIDENT_AUTODECLARE re-derived from V099 (V098 re-link guard + V099 company scope kept) with an [attach] arm linking a later unlinked OPEN/ACK CRITICAL to the OPEN/MITIGATED incident its family''s already-open guard matched, and an [auto-mitigate] sweep moving an OPEN incident to MITIGATED (never RESOLVED -- closing stays human) once every ALERT member has been RESOLVED for >= 1h (MITIGATED_AT = the last member resolve; a SUPERSEDED / SNOOZE_SUPPRESSED member blocks while its successor is live). Adds INCIDENTS.MITIGATED_BY (machine provenance; NULL = a human). Both arms are exception-isolated and sit after the INCIDENT_AUTO_DECLARE_CRITICAL toggle (off = neither runs); no task change, no backfill, no CALL at apply.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 154);
