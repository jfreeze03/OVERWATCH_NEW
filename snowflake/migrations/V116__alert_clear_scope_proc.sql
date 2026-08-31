-- V116__alert_clear_scope_proc.sql
--
-- Atomic, idempotent set-based clear for the Alerts "clear the open queue" action.
--
-- The drawer/bulk lifecycle already route through SP_ALERT_LIFECYCLE (audit + status change
-- in one transaction, guarded by an OW_ACTION_INTENTS idempotency key). The clear-queue was
-- the exception: it ran the audit INSERT and the status UPDATE as two SEPARATE auto-committed
-- statements. If the UPDATE failed after the audit committed (a transient/lock error), the
-- events stayed OPEN with audit rows already written, and a retry re-inserted duplicate audit
-- rows (the audit has no dedupe). Alert-hunt #4.
--
-- SP_ALERT_CLEAR_SCOPE does the audit + status change atomically over a STATUS + company SCOPE
-- (not enumerated ids, so it still clears past the feed cap), recording the idempotency key so a
-- double-click / retry is a DUPLICATE no-op. Mirrors SP_ALERT_SNOOZE exactly (BEGIN TRANSACTION
-- ... COMMIT, EXCEPTION -> ROLLBACK). The app calls it via execute_action() with the existing
-- two-statement builder as the pre-V116 legacy fallback, so behaviour is unchanged until applied.
--
-- New proc only, no schema change. Owner applies in Snowsight after V115.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20116, 'V116 requires V115 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 115) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Scope-based bulk ack/resolve, atomic + audited + idempotent (mirrors SP_ALERT_LIFECYCLE /
-- SP_ALERT_SNOOZE). P_ACTION in {ACK, RESOLVE}. P_COMPANY = 'ALL' clears every company;
-- otherwise the company PLUS account-level (COMPANY='ALL'). P_KIND, when a real resolution
-- kind, tags a RESOLVE for the precision score; blank leaves RESOLUTION_KIND untouched (an
-- untagged validation clear stays excluded from precision).
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_CLEAR_SCOPE(
    P_COMPANY VARCHAR, P_ACTION VARCHAR, P_NOTE VARCHAR, P_KIND VARCHAR,
    P_ACTOR VARCHAR, P_IDEM_KEY VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    v_actor VARCHAR;
    v_action VARCHAR;
    v_kind VARCHAR;
    v_note VARCHAR;
    v_all BOOLEAN;
    v_n NUMBER;
BEGIN
    IF (COALESCE(TRIM(:P_IDEM_KEY), '') = '') THEN
        RETURN 'BLOCKED: missing idempotency key';
    END IF;
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS WHERE IDEM_KEY = :P_IDEM_KEY)) THEN
        RETURN 'DUPLICATE: ' || :P_IDEM_KEY;
    END IF;
    v_action := IFF(UPPER(COALESCE(:P_ACTION, '')) = 'RESOLVE', 'RESOLVE', 'ACK');
    v_actor := COALESCE(NULLIF(:P_ACTOR, ''), CURRENT_USER());
    v_kind := UPPER(COALESCE(:P_KIND, ''));
    IF (v_kind NOT IN ('ACTIONED', 'NOISE', 'EXPECTED')) THEN
        v_kind := '';
    END IF;
    v_note := IFF(v_kind <> '', '[' || :v_kind || '] ', '')
              || COALESCE(:P_NOTE, '') || ' — by ' || :v_actor;
    v_all := (UPPER(COALESCE(:P_COMPANY, 'ALL')) = 'ALL');

    BEGIN TRANSACTION;
    IF (v_action = 'ACK') THEN
        -- ACK only transitions OPEN rows (already-ACK stay ACK and keep counting as open).
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
        SELECT EVENT_ID, 'ACK', :v_note, :v_actor
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE STATUS = 'OPEN'
           AND (:v_all OR COMPANY = :P_COMPANY OR UPPER(COMPANY) = 'ALL');
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = 'ACK', ACK_BY = :v_actor, ACK_AT = CURRENT_TIMESTAMP()
         WHERE STATUS = 'OPEN'
           AND (:v_all OR COMPANY = :P_COMPANY OR UPPER(COMPANY) = 'ALL');
        v_n := SQLROWCOUNT;
    ELSE
        -- RESOLVE transitions OPEN and ACK; a real kind tags the row, blank leaves it untouched.
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
        SELECT EVENT_ID, 'RESOLVE', :v_note, :v_actor
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE STATUS IN ('OPEN', 'ACK')
           AND (:v_all OR COMPANY = :P_COMPANY OR UPPER(COMPANY) = 'ALL');
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(),
               RESOLUTION_KIND = IFF(:v_kind <> '', :v_kind, RESOLUTION_KIND)
         WHERE STATUS IN ('OPEN', 'ACK')
           AND (:v_all OR COMPANY = :P_COMPANY OR UPPER(COMPANY) = 'ALL');
        v_n := SQLROWCOUNT;
    END IF;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS (IDEM_KEY, KIND, ACTOR, RESULT)
    VALUES (:P_IDEM_KEY, 'ALERT_CLEAR_' || :v_action, :v_actor, :v_n || ' event(s)');
    COMMIT;
    RETURN 'OK: ' || :v_n || ' event(s) ' || :v_action;
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 116 AS VERSION,
       'Atomic scope-based alert clear: SP_ALERT_CLEAR_SCOPE does the ALERT_AUDIT insert and the ALERT_EVENTS status change in one transaction over a STATUS + company scope, guarded by an OW_ACTION_INTENTS idempotency key (mirrors SP_ALERT_LIFECYCLE / SP_ALERT_SNOOZE). Replaces the clear-queue two-autocommit path where a mid-op failure + retry could duplicate audit rows. New proc only, no schema change; the app calls it via execute_action() with the existing two-statement builder as the legacy fallback.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 116);
