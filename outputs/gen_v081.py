#!/usr/bin/env python3
"""Forward-generate V081: SP_VERIFY_EXPERIMENT — one transaction that ends the
divergence between the experiment ledger and the savings ledger, BOTH WAYS.

Decision Studio review, finding #5. Settling an optimization experiment used to
write OPTIMIZATION_EXPERIMENTS.VERIFIED_USD only (app/logic/workbench.update_experiment_sql):
it never booked SAVINGS_LEDGER and never closed the ACTION_QUEUE row the experiment
came from, so the two "verified savings" totals drifted apart and a verified
experiment left its action sitting OPEN forever.

This adds ONE audited procedure that settles a terminal experiment outcome
(VERIFIED / REJECTED / ROLLED_BACK) atomically. It always records the verdict
(step 1); the ledger + action legs run only when the experiment carries an
ACTION_ID:
  VERIFIED:
    1. UPDATE OPTIMIZATION_EXPERIMENTS -> STATUS='VERIFIED', VERIFIED_USD, RESULT_NOTE.
    2. MERGE SAVINGS_LEDGER keyed on ACTION_ID (discriminated by FINDING_TYPE='EXPERIMENT'
       so it upserts exactly the experiment's own row, never an auto-booked / remediation
       row for the same action).
    3. Close the source ACTION_QUEUE row (STATUS='DONE', COMPLETED_AT) + ACTION_ACTIVITY audit.
  REJECTED / ROLLED_BACK (the compensating inverse — skeptic #2 caught that without it
  a rollback-after-verify re-opened the exact #5 divergence and stranded the action):
    1. UPDATE OPTIMIZATION_EXPERIMENTS -> STATUS, VERIFIED_USD=NULL, RESULT_NOTE.
    2+3. Only if this experiment had actually booked a VERIFIED ledger row for the action:
       flip that ledger row to STATE='REJECTED' / VERIFIED_USD=NULL and reopen the action
       (STATUS='OPEN', COMPLETED_AT=NULL) + ACTION_ACTIVITY audit. Gated on a prior booking
       so it never reopens an action closed for an unrelated reason.

Modelled on SP_ACTION_LIFECYCLE (V074): RETURNS VARCHAR, EXECUTE AS OWNER, a single
BEGIN TRANSACTION ... COMMIT with EXCEPTION WHEN OTHER THEN ROLLBACK; RAISE, and a
REQUEST_KEY idempotency guard. Schema confirmed against V074/V005: the experiment
description column is HYPOTHESIS (NOT NULL, 4000) with a short TITLE (NOT NULL, 500);
the ledger DESCRIPTION is NOT NULL 500, so the short TITLE (not the 4000-char
HYPOTHESIS) is used for the ledger label.

Known limits (disclosed, not fixed here): the ledger row keys on ACTION_ID, so N
experiments sharing one action collapse to one ledger row (experiments are ~1:1 with
actions); and ledger_totals sums all VERIFIED rows without cross-source dedup (a
pre-existing property of that headline).

Procedure-only: no table changes, no data reload. Owner applies in Snowsight after
V080; the app rewire (update_experiment_sql -> CALL for VERIFIED/REJECTED/ROLLED_BACK)
ships with it. This file never runs from the app.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

# The procedure body. No ';' appears in a top-level -- comment (only inside the $$
# body, which statement splitters skip), matching the house caution in gen_v080.
PROC = """CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_EXPERIMENT(
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

    SELECT COUNT(*), MAX(ACTION_ID), MAX(TITLE)
      INTO :matched, :aid, :exp_title
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
$$;"""

out = f"""-- V081__unified_experiment_verify.sql
--
-- SP_VERIFY_EXPERIMENT: one transaction that ends the divergence between the
-- experiment ledger and the savings ledger, BOTH WAYS (Decision Studio review, #5).
--
-- Settling an experiment used to write OPTIMIZATION_EXPERIMENTS.VERIFIED_USD only --
-- it never booked SAVINGS_LEDGER and never closed the ACTION_QUEUE row the experiment
-- came from, so the two "verified savings" totals drifted and a verified experiment
-- left its action OPEN. This procedure settles a terminal outcome atomically:
--   VERIFIED           -> record the verdict, MERGE the savings ledger keyed on
--                         ACTION_ID (FINDING_TYPE='EXPERIMENT' so it upserts exactly the
--                         experiment's own row), and close the action (DONE) + audit.
--   REJECTED/ROLLED_BACK-> record the verdict (VERIFIED_USD cleared) and, if this
--                         experiment had booked a verified ledger row, reverse it
--                         (STATE='REJECTED') and reopen the action (OPEN) + audit --
--                         gated on a prior booking so an unrelated DONE action is never
--                         reopened. Without this inverse, a rollback-after-verify
--                         re-created the exact #5 divergence.
-- The ledger + action legs run only when the experiment carries an ACTION_ID.
--
-- Modelled on SP_ACTION_LIFECYCLE (V074): EXECUTE AS OWNER, single BEGIN TRANSACTION
-- with EXCEPTION rollback, REQUEST_KEY idempotency. Known limits: the ledger keys on
-- ACTION_ID (N experiments per action share ONE ledger row -- experiments are ~1:1 with
-- actions -- so rejecting one of several experiments on the same action can reverse
-- another's still-valid booking), and ledger_totals sums all VERIFIED rows without
-- cross-source dedup (a pre-existing property of that headline). Procedure-only -- no
-- table changes, no data reload. Owner applies in Snowsight after V080; the app rewire (update_experiment_sql
-- -> CALL for VERIFIED/REJECTED/ROLLED_BACK) ships with it. Never run by the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20081, 'V081 requires V080 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 80) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{PROC}

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 81 AS VERSION,
       'Unified experiment settle: SP_VERIFY_EXPERIMENT atomically settles an optimization experiment''s terminal outcome -- VERIFIED books SAVINGS_LEDGER keyed on ACTION_ID (FINDING_TYPE=''EXPERIMENT'') and closes the source ACTION_QUEUE row; REJECTED/ROLLED_BACK reverse a prior booking and reopen the action -- with an audited ACTION_ACTIVITY entry, so the experiment and savings ledgers can no longer diverge in either direction. Procedure-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 81);
"""

# --- self-assertions: the file is exactly one guarded proc + one version row ------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE VIEW" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20081" in out and "IF (v < 80) THEN" in out
assert "SELECT 81 AS VERSION" in out and "WHERE VERSION = 81)" in out
# everything is in ONE transaction (both the verify and the reverse legs)
assert out.count("BEGIN TRANSACTION;") == 1 and out.count("COMMIT;") == 1
assert "UPDATE DBA_MAINT_DB.OVERWATCH.OPTIMIZATION_EXPERIMENTS" in out
# only terminal outcomes settle
assert "new_status NOT IN ('VERIFIED', 'REJECTED', 'ROLLED_BACK')" in out
# VERIFIED leg: book the ledger keyed on ACTION_ID + close the action
assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER" in out
assert "l.ACTION_ID = s.ACTION_ID AND l.FINDING_TYPE = 'EXPERIMENT'" in out
assert "STATUS = 'DONE'" in out
# REJECTED/ROLLED_BACK leg: reverse a prior booking + reopen -- gated on had_booking
assert "IF (had_booking > 0) THEN" in out
assert "STATE = 'REJECTED'" in out
assert "STATUS = 'OPEN'" in out and "COMPLETED_AT = NULL" in out
assert out.count("UPDATE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE") == 2   # close + reopen
assert out.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY") == 2
# ledger/action legs are gated on a present ACTION_ID (standalone experiments skip them)
assert "IF (aid IS NOT NULL AND TRIM(aid) <> '') THEN" in out
# rollback safety + idempotency
assert "EXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;" in out
assert "DUPLICATE: request already applied" in out

target = Path(os.environ.get("V081_OUT") or (MIG / "V081__unified_experiment_verify.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
