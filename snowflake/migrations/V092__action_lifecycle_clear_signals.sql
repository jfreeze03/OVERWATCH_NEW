-- V092__action_lifecycle_clear_signals.sql
--
-- Give SP_ACTION_LIFECYCLE explicit CLEAR signals so an operator can un-assign an
-- owner and un-defer (resume) a work item from the Action Center. V074's proc uses
-- COALESCE-keep for OWNER and DEFER_UNTIL, so a blank owner / NULL defer is a keep,
-- never a clear -- v4.318 had to make the UI honest by dropping the "unassign" /
-- "clear the defer" effects the write could not perform, leaving a genuine gap.
--
-- Re-derives SP_ACTION_LIFECYCLE from the LATEST definition (V074) byte-identically
-- plus two enumerated edits: two new BOOLEAN parameters (P_CLEAR_OWNER,
-- P_CLEAR_DEFER) appended after P_REQUEST_KEY, and the OWNER / DEFER_UNTIL
-- assignments wrapped IFF(:P_CLEAR_x, NULL, <old COALESCE-keep>). With both flags
-- FALSE (the app default for an ordinary edit) the UPDATE is the old behaviour
-- byte-for-byte. The audit INSERT already logs the passed OWNER/DEFER_UNTIL (NULL on
-- a clear), so a clear is recorded without change.
--
-- The signature changes (8 -> 10 args), so the old 8-arg overload is DROPped first
-- (a differing signature is a distinct object under CREATE OR REPLACE). Only the app
-- calls this proc; the rewire (clear flags + restored effect lines) ships with it.
--
-- Procedure-only: no table changes, no data reload. Owner applies in Snowsight after
-- V091. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20092, 'V092 requires V091 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 91) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Drop the superseded 8-arg overload before creating the 10-arg definition.
DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_ACTION_LIFECYCLE(VARCHAR, VARCHAR, VARCHAR, DATE, DATE, VARCHAR, VARCHAR, VARCHAR);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ACTION_LIFECYCLE(
    P_ACTION_ID VARCHAR,
    P_STATUS VARCHAR,
    P_OWNER VARCHAR,
    P_DUE_DATE DATE,
    P_DEFER_UNTIL DATE,
    P_NOTE VARCHAR,
    P_ACTOR VARCHAR,
    P_REQUEST_KEY VARCHAR,
    P_CLEAR_OWNER BOOLEAN,
    P_CLEAR_DEFER BOOLEAN
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    seen NUMBER DEFAULT 0;
    matched NUMBER DEFAULT 0;
    old_status VARCHAR;
    next_status VARCHAR;
BEGIN
    IF (P_ACTION_ID IS NULL OR TRIM(P_ACTION_ID) = '') THEN
        RETURN 'INVALID: action id is required';
    END IF;
    IF (P_STATUS IS NOT NULL AND TRIM(P_STATUS) <> ''
        AND UPPER(TRIM(P_STATUS)) NOT IN ('OPEN', 'IN_PROGRESS', 'DONE', 'DROPPED')) THEN
        RETURN 'INVALID: unsupported status';
    END IF;
    IF (P_REQUEST_KEY IS NOT NULL AND TRIM(P_REQUEST_KEY) <> '') THEN
        SELECT COUNT(*) INTO :seen
        FROM DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY
        WHERE REQUEST_KEY = :P_REQUEST_KEY;
        IF (seen > 0) THEN
            RETURN 'DUPLICATE: request already applied';
        END IF;
    END IF;

    SELECT COUNT(*), MAX(STATUS) INTO :matched, :old_status
    FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
    WHERE ACTION_ID = :P_ACTION_ID;
    IF (matched = 0) THEN
        RETURN 'NOT_FOUND: action id';
    END IF;
    next_status := COALESCE(NULLIF(UPPER(TRIM(P_STATUS)), ''), old_status);

    BEGIN TRANSACTION;
    UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
       SET STATUS = :next_status,
           OWNER = IFF(:P_CLEAR_OWNER, NULL, COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER)),
           DUE_DATE = COALESCE(:P_DUE_DATE, DUE_DATE),
           DEFER_UNTIL = IFF(:P_CLEAR_DEFER, NULL, COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL)),
           RESOLUTION_NOTE = IFF(:next_status IN ('DONE', 'DROPPED')
                                 AND NULLIF(TRIM(:P_NOTE), '') IS NOT NULL,
                                 :P_NOTE, RESOLUTION_NOTE),
           COMPLETED_AT = IFF(:next_status IN ('DONE', 'DROPPED'),
                              COALESCE(COMPLETED_AT, CURRENT_TIMESTAMP()), NULL),
           UPDATED_AT = CURRENT_TIMESTAMP(),
           UPDATED_BY = COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER())
     WHERE ACTION_ID = :P_ACTION_ID;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY
        (ACTION_ID, ACTIVITY_TYPE, FROM_STATUS, TO_STATUS, OWNER_NAME,
         DUE_DATE, DEFER_UNTIL, NOTE, REQUEST_KEY, CREATED_BY)
    SELECT :P_ACTION_ID,
           IFF(:next_status = :old_status, 'COMMENT', 'TRANSITION'),
           :old_status, :next_status, NULLIF(TRIM(:P_OWNER), ''),
           :P_DUE_DATE, :P_DEFER_UNTIL, NULLIF(TRIM(:P_NOTE), ''),
           NULLIF(TRIM(:P_REQUEST_KEY), ''),
           COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER());
    COMMIT;
    RETURN 'OK: action updated';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 92 AS VERSION,
       'Action lifecycle clear signals: SP_ACTION_LIFECYCLE re-derived from V074 with P_CLEAR_OWNER / P_CLEAR_DEFER BOOLEAN parameters so an operator can un-assign an owner and un-defer (resume) a work item -- the COALESCE-keep semantics could only keep, never clear (the v4.318 UI honesty gap). Both flags FALSE = the old behaviour byte-for-byte; the audit already logs the cleared value. Old 8-arg overload dropped. Procedure-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 92);
