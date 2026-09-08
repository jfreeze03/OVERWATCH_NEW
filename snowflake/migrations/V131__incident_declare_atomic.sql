-- V131__incident_declare_atomic.sql
--
-- Atomic manual incident declare (Codex R34). The manual "Declare incident + link alerts" path ran two
-- separate INSERTs (INCIDENTS, then INCIDENT_MEMBERS) as two execute_statement round-trips, so if the
-- members INSERT failed (or the session dropped) after the INCIDENTS INSERT committed, a titled,
-- member-less incident was left behind. SP_INCIDENT_DECLARE does both writes in ONE transaction — the
-- incident and its member links commit together or not at all.
--
-- The proc reproduces the app's declare logic exactly (control_room.py _incident_declare_sql /
-- _incident_open_family_inner), server-side and injection-safe via bound parameters: a UUID shared
-- across both inserts; the family-already-open guard (do not open a second OPEN/MITIGATED incident for a
-- (family, company [, entity]) already covered); the conditional entity filter (applied only for a
-- non-ACCOUNT proposal that names an entity, expressed statically as `NOT :apply_entity OR <match>` so
-- there is NO dynamic SQL); the 48h open/ack member window; per-alert dedup; and the "only link members
-- if the incident row was actually created" guard. The app keeps its family-open PRE-check (for the
-- honest "already open" message, since execute_statement cannot see the proc's return), and now runs
-- this single CALL instead of the two INSERTs.
--
-- No schema change (INCIDENTS / INCIDENT_MEMBERS already exist). Owner applies in Snowsight after V130.
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20131, 'V131 requires V130 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 130) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(
    P_TITLE VARCHAR,
    P_SEVERITY VARCHAR,
    P_COMPANY VARCHAR,
    P_PROPOSAL_KEY VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    inc_id STRING;
    family STRING;
    entity_kind STRING;
    entity_name STRING;
    apply_entity BOOLEAN;
    created INT DEFAULT 0;
    members INT DEFAULT 0;
BEGIN
    IF (P_PROPOSAL_KEY IS NULL OR TRIM(P_PROPOSAL_KEY) = '') THEN
        RETURN 'INVALID: proposal key is required';
    END IF;
    inc_id := UUID_STRING();
    family := SPLIT_PART(:P_PROPOSAL_KEY, '|', 1);
    entity_kind := UPPER(SPLIT_PART(:P_PROPOSAL_KEY, '|', 3));
    -- entity_name is the 4th pipe-field. Realistic entity names (warehouse / db / task) contain no
    -- '|' -- a pipe would itself corrupt the pipe-delimited DEDUPE_KEY this is matched against
    -- (SPLIT_PART(...,2)) -- so SPLIT_PART(,4) equals the app's split('|',3) remainder for every
    -- real key, and matching a single field against the single DEDUPE_KEY entity position is correct.
    entity_name := SPLIT_PART(:P_PROPOSAL_KEY, '|', 4);
    -- Entity filter applies only for a non-ACCOUNT proposal that names an entity (matches the app's
    -- `len(parts)==4 and parts[2] != 'ACCOUNT'`); ACCOUNT proposals use the family-only scope.
    apply_entity := (ARRAY_SIZE(SPLIT(:P_PROPOSAL_KEY, '|')) >= 4 AND entity_kind <> 'ACCOUNT');

    BEGIN TRANSACTION;

    -- 1) open the incident, UNLESS an OPEN/MITIGATED incident already covers this (family, company
    --    [, entity]) — the family-already-open guard (identical predicate to the app's pre-check).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND)
    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,
           CURRENT_TIMESTAMP(), 'UNKNOWN'
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT' AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND (i.COMPANY = :P_COMPANY OR UPPER(i.COMPANY) = 'ALL')
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = :family
          AND (NOT :apply_entity
               OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = UPPER(:entity_name))
    );

    -- 2) link every open/ack alert of this family (+entity) in the 48h window as members, per-alert
    --    de-duplicated, but only if the incident row was actually created above.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED)
    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND (e.COMPANY = :P_COMPANY OR UPPER(e.COMPANY) = 'ALL')
      AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = :family
      AND (NOT :apply_entity
           OR UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = UPPER(:entity_name))
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                      WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
                  WHERE i2.INCIDENT_ID = :inc_id);

    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS WHERE INCIDENT_ID = :inc_id;
    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;

    COMMIT;

    IF (:created = 0) THEN
        RETURN 'NOOP: this family already has an open incident';
    END IF;
    RETURN 'DECLARED: ' || :members || ' member(s) linked';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 131 AS VERSION,
       'Atomic manual incident declare (R34): SP_INCIDENT_DECLARE does the INCIDENTS + INCIDENT_MEMBERS inserts in ONE transaction, replacing the app''s two separate execute_statement INSERTs that could half-apply a titled member-less incident on a mid-failure. Reproduces the app''s family-already-open guard + conditional entity filter (static NOT :apply_entity OR match, no dynamic SQL) + 48h member window + per-alert dedup + only-if-created guard, server-side and injection-safe via bound params. No schema change; the app now runs one CALL instead of two INSERTs (keeping its family-open pre-check for the honest already-open message).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 131);
