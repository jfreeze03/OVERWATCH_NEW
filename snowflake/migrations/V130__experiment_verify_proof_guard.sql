-- V130__experiment_verify_proof_guard.sql
--
-- Proc-level proof enforcement in SP_VERIFY_EXPERIMENT (Codex R32). The Decision Studio UI gates a
-- VERIFIED settlement on a result note, positive verified USD, and a closed observation window, but the
-- settlement PROCEDURE validated only the status enum / existence / request-key idempotency (V081) -- so a
-- VERIFIED call with an empty note and $0 (the owner hand-calling in Snowsight) would book a $0,
-- no-evidence SAVINGS_LEDGER row. SP_VERIFY_EXPERIMENT now REJECTS, regardless of caller, a VERIFIED
-- verdict that lacks a result note or positive savings, or whose observation window has not closed --
-- returning 'BLOCKED: …' BEFORE the transaction opens. REJECTED / ROLLED_BACK verdicts are unaffected.
--
-- Re-derives SP_VERIFY_EXPERIMENT from V081 with only the OBSERVATION_END read + the two proof guards
-- added; everything else byte-identical. No schema change. Owner applies in Snowsight after V129; the
-- next settlement is proof-gated at the database. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20130, 'V130 requires V129 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 129) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_EXPERIMENT(
    P_EXPERIMENT_ID VARCHAR,
    P_STATUS VARCHAR,
    P_VERIFIED_USD NUMBER,
    P_RESULT_NOTE VARCHAR,
    P_ACTOR VARCHAR,
    P_REQUEST_KEY VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    matched NUMBER DEFAULT 0;
    seen NUMBER DEFAULT 0;
    had_booking NUMBER DEFAULT 0;
    aid VARCHAR;
    exp_title VARCHAR;
    actor VARCHAR;
    new_status VARCHAR;
    obs_end DATE;
BEGIN
    IF (P_EXPERIMENT_ID IS NULL OR TRIM(P_EXPERIMENT_ID) = '') THEN
        RETURN 'INVALID: experiment id is required';
    END IF;
    new_status := UPPER(TRIM(P_STATUS));
    -- IS NULL first: `NULL NOT IN (...)` is NULL (not TRUE) under three-valued logic,
    -- so a NULL status would otherwise slip the guard and write STATUS=NULL.
    IF (new_status IS NULL OR new_status NOT IN ('VERIFIED', 'REJECTED', 'ROLLED_BACK')) THEN
        RETURN 'INVALID: status must be VERIFIED, REJECTED or ROLLED_BACK';
    END IF;
    actor := COALESCE(NULLIF(TRIM(P_ACTOR), ''), CURRENT_USER());

    SELECT COUNT(*), MAX(ACTION_ID), MAX(TITLE), MAX(TO_DATE(OBSERVATION_END))
      INTO :matched, :aid, :exp_title, :obs_end
      FROM DBA_MAINT_DB.OVERWATCH.OPTIMIZATION_EXPERIMENTS
     WHERE EXPERIMENT_ID = :P_EXPERIMENT_ID;
    IF (matched = 0) THEN
        RETURN 'NOT_FOUND: experiment id';
    END IF;

    -- A repeated settle carrying the same request key is a no-op (double-submit safe).
    IF (P_REQUEST_KEY IS NOT NULL AND TRIM(P_REQUEST_KEY) <> '') THEN
        SELECT COUNT(*) INTO :seen
          FROM DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY
         WHERE REQUEST_KEY = :P_REQUEST_KEY;
        IF (seen > 0) THEN
            RETURN 'DUPLICATE: request already applied';
        END IF;
    END IF;

    -- R32: proc-level proof enforcement (defense-in-depth). The UI already gates these, but
    -- a VERIFIED settlement hand-called via Snowsight must not book a $0 / no-evidence saving.
    IF (:new_status = 'VERIFIED'
        AND (P_RESULT_NOTE IS NULL OR TRIM(P_RESULT_NOTE) = ''
             OR P_VERIFIED_USD IS NULL OR P_VERIFIED_USD <= 0)) THEN
        RETURN 'BLOCKED: VERIFIED needs a result note and positive verified savings';
    END IF;
    IF (:new_status = 'VERIFIED' AND :obs_end IS NOT NULL AND :obs_end > CURRENT_DATE()) THEN
        RETURN 'BLOCKED: the observation window has not closed yet';
    END IF;

    BEGIN TRANSACTION;

    -- 1) the experiment's own verdict. VERIFIED_USD is meaningful only for VERIFIED;
    --    a REJECTED/ROLLED_BACK verdict clears it.
    UPDATE DBA_MAINT_DB.OVERWATCH.OPTIMIZATION_EXPERIMENTS
       SET STATUS = :new_status,
           VERIFIED_USD = IFF(:new_status = 'VERIFIED', :P_VERIFIED_USD, NULL),
           RESULT_NOTE = COALESCE(NULLIF(TRIM(:P_RESULT_NOTE), ''), RESULT_NOTE),
           UPDATED_AT = CURRENT_TIMESTAMP(),
           UPDATED_BY = :actor
     WHERE EXPERIMENT_ID = :P_EXPERIMENT_ID;

    -- 2+3) reconcile the ledger and the action -- only when the experiment is tied to
    --      an action (standalone experiments have neither to touch).
    IF (aid IS NOT NULL AND TRIM(aid) <> '') THEN
        IF (:new_status = 'VERIFIED') THEN
            -- book the verified savings (upsert exactly the experiment's own row) ...
            MERGE INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
            USING (SELECT :aid AS ACTION_ID) s
               ON l.ACTION_ID = s.ACTION_ID AND l.FINDING_TYPE = 'EXPERIMENT'
            WHEN MATCHED THEN UPDATE SET
                STATE = 'VERIFIED',
                VERIFIED_USD = :P_VERIFIED_USD,
                VERIFIED_AT = CURRENT_TIMESTAMP(),
                VERIFIED_BY = :actor,
                DESCRIPTION = LEFT(COALESCE(:exp_title, l.DESCRIPTION), 500),
                NOTES = LEFT(COALESCE(NULLIF(TRIM(:P_RESULT_NOTE), ''), l.NOTES), 2000)
            WHEN NOT MATCHED THEN INSERT
                (ACTION_ID, DESCRIPTION, STATE, VERIFIED_USD, VERIFIED_AT, VERIFIED_BY,
                 FINDING_TYPE, NOTES)
            VALUES
                (:aid, LEFT(COALESCE(:exp_title, 'Verified experiment'), 500), 'VERIFIED',
                 :P_VERIFIED_USD, CURRENT_TIMESTAMP(), :actor, 'EXPERIMENT',
                 LEFT(NULLIF(TRIM(:P_RESULT_NOTE), ''), 2000));

            -- ... and close the source action.
            UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
               SET STATUS = 'DONE',
                   COMPLETED_AT = COALESCE(COMPLETED_AT, CURRENT_TIMESTAMP()),
                   RESOLUTION_NOTE = COALESCE(NULLIF(TRIM(:P_RESULT_NOTE), ''),
                                              'Verified via experiment ' || :P_EXPERIMENT_ID),
                   UPDATED_AT = CURRENT_TIMESTAMP(),
                   UPDATED_BY = :actor
             WHERE ACTION_ID = :aid AND STATUS <> 'DONE';

            INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY
                (ACTION_ID, ACTIVITY_TYPE, TO_STATUS, NOTE, REQUEST_KEY, CREATED_BY)
            SELECT :aid, 'TRANSITION', 'DONE',
                   'Verified via experiment ' || :P_EXPERIMENT_ID
                       || COALESCE(' ($' || TO_VARCHAR(:P_VERIFIED_USD) || ')', ''),
                   NULLIF(TRIM(:P_REQUEST_KEY), ''), :actor;
        ELSE
            -- REJECTED / ROLLED_BACK: reverse a PRIOR verified booking so the ledger
            -- and the experiment can't diverge, and reopen the action we had closed.
            -- Gated on a real prior booking so an action closed for another reason is
            -- never reopened.
            SELECT COUNT(*) INTO :had_booking
              FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
             WHERE ACTION_ID = :aid AND FINDING_TYPE = 'EXPERIMENT' AND STATE = 'VERIFIED';
            IF (had_booking > 0) THEN
                UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
                   SET STATE = 'REJECTED',
                       VERIFIED_USD = NULL,
                       VERIFIED_AT = CURRENT_TIMESTAMP(),
                       VERIFIED_BY = :actor,
                       NOTES = LEFT('Reversed: experiment ' || :new_status
                                    || COALESCE(' -- ' || NULLIF(TRIM(:P_RESULT_NOTE), ''), ''), 2000)
                 WHERE ACTION_ID = :aid AND FINDING_TYPE = 'EXPERIMENT';

                UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
                   SET STATUS = 'OPEN',
                       COMPLETED_AT = NULL,
                       RESOLUTION_NOTE = 'Reopened: verified experiment ' || :new_status,
                       UPDATED_AT = CURRENT_TIMESTAMP(),
                       UPDATED_BY = :actor
                 WHERE ACTION_ID = :aid AND STATUS = 'DONE';

                INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY
                    (ACTION_ID, ACTIVITY_TYPE, TO_STATUS, NOTE, REQUEST_KEY, CREATED_BY)
                SELECT :aid, 'TRANSITION', 'OPEN',
                       'Reopened: experiment ' || :P_EXPERIMENT_ID || ' ' || :new_status,
                       NULLIF(TRIM(:P_REQUEST_KEY), ''), :actor;
            END IF;
        END IF;
    END IF;

    COMMIT;
    RETURN 'OK: experiment ' || LOWER(:new_status);
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 130 AS VERSION,
       'Proc-level proof enforcement in SP_VERIFY_EXPERIMENT (R32): re-derived from V081 to REJECT a VERIFIED settlement that lacks a result note or positive verified savings, or whose observation window has not closed, regardless of caller (the UI already gated these; this closes the hand-called-Snowsight bypass that could book a $0 no-evidence SAVINGS_LEDGER row). Returns BLOCKED before the transaction; REJECTED/ROLLED_BACK unaffected. Everything else byte-identical, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 130);
