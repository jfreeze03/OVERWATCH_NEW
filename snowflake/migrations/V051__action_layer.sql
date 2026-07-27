-- V051__action_layer.sql — atomic alert lifecycle + idempotency
-- (Tranche C / r28; design: docs/design/ACTION_LAYER_V051.md).
--
--   Scoped slice (2026-07-27): the alert ack/resolve lifecycle becomes ONE
--   transactional, set-based, idempotent proc — replacing the app's split
--   UPDATE+audit statements that could partially succeed. OW_ACTION_INTENTS
--   dedups a re-submitted action by key. The app calls it proc-first with a
--   legacy fallback, so pre-V051 the app behaves exactly as v4.52.
--
--   The full action layer (remediation / verify-savings / action-queue procs +
--   the typed savings link) was DEFERRED: an adversarial review of the first
--   draft found an owner-privileged SQL injection and an over-broad allow-list
--   in the unwired remediation proc. Those need real design + wiring + security
--   hardening and are their own round (see the design doc). This migration
--   ships only the wired, verified path.
--
--   Idempotency, honestly: OW_ACTION_INTENTS.IDEM_KEY dedups SEQUENTIAL
--   retries (a double-click returns DUPLICATE without re-running). Snowflake
--   PRIMARY KEY is informational — it does not lock — so two truly concurrent
--   calls sharing a key are not serialized; the alert transitions are
--   idempotent anyway (re-ACK of an already-ACK event is a no-op via the
--   STATUS filter), and the writer population is ~2 DBAs. Apply AFTER V050.
--   Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20051, 'V051 requires V050 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 50) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS (
    IDEM_KEY   VARCHAR(120)  NOT NULL PRIMARY KEY,
    KIND       VARCHAR(40)   NOT NULL,
    ACTOR      VARCHAR(200),
    CREATED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    RESULT     VARCHAR(400)
);

-- Atomic alert lifecycle: audit + update in ONE transaction, set-based.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE(
    P_EVENT_IDS VARCHAR, P_ACTION VARCHAR, P_NOTE VARCHAR,
    P_KIND VARCHAR, P_ACTOR VARCHAR, P_IDEM_KEY VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    v_action VARCHAR;
    v_kind VARCHAR;
    v_note VARCHAR;
    v_n NUMBER;
BEGIN
    IF (COALESCE(TRIM(:P_IDEM_KEY), '') = '') THEN
        RETURN 'BLOCKED: missing idempotency key';
    END IF;
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS WHERE IDEM_KEY = :P_IDEM_KEY)) THEN
        RETURN 'DUPLICATE: ' || :P_IDEM_KEY;
    END IF;
    v_action := IFF(UPPER(:P_ACTION) = 'RESOLVE', 'RESOLVE', 'ACK');
    v_kind := IFF(UPPER(COALESCE(:P_KIND, '')) IN ('ACTIONED', 'NOISE', 'EXPECTED'), UPPER(:P_KIND), '');
    v_note := IFF(:v_kind <> '', '[' || :v_kind || '] ' || COALESCE(:P_NOTE, ''), COALESCE(:P_NOTE, ''))
              || IFF(COALESCE(:P_ACTOR, '') <> '', ' — by ' || :P_ACTOR, '');
    BEGIN TRANSACTION;
    IF (:v_action = 'ACK') THEN
        -- Audit BEFORE the update and on the SAME pre-state filter, so audit
        -- rows match exactly the events this call transitions — an event
        -- already ACK from a prior call is neither re-audited nor re-counted.
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
        SELECT EVENT_ID, 'ACK', :v_note, COALESCE(NULLIF(:P_ACTOR, ''), CURRENT_USER())
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS = 'OPEN';
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = 'ACK', ACK_BY = COALESCE(NULLIF(:P_ACTOR, ''), CURRENT_USER()),
               ACK_AT = CURRENT_TIMESTAMP()
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS = 'OPEN';
        v_n := SQLROWCOUNT;
    ELSE
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
        SELECT EVENT_ID, 'RESOLVE', :v_note, COALESCE(NULLIF(:P_ACTOR, ''), CURRENT_USER())
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS IN ('OPEN', 'ACK');
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(),
               RESOLUTION_KIND = IFF(:v_kind <> '', :v_kind, RESOLUTION_KIND)
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS IN ('OPEN', 'ACK');
        v_n := SQLROWCOUNT;
    END IF;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS (IDEM_KEY, KIND, ACTOR, RESULT)
    VALUES (:P_IDEM_KEY, 'ALERT_' || :v_action, :P_ACTOR, :v_n || ' event(s)');
    COMMIT;
    RETURN 'OK: ' || :v_n || ' event(s) ' || :v_action;
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 51 AS VERSION,
       'Action layer (r28/Tranche C, scoped): SP_ALERT_LIFECYCLE atomic set-based alert ack/resolve + audit in one transaction, OW_ACTION_INTENTS idempotency (sequential-retry dedup). Remediation/verify/action-queue procs deferred after adversarial review found an owner-privileged injection in the unwired draft.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 51);
