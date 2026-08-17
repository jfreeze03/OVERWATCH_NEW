#!/usr/bin/env python3
"""Forward-generate V086: per-event alert snooze (CoCo Alerts29).

An operator can silence one specific alert until a wake time ("handle this
Monday") without acking or resolving it. Design (chosen: per-event grain):

  * A snoozed event moves to STATUS='SNOOZED' (a new lifecycle state) with a
    SNOOZED_UNTIL wake time. The triage feed reads STATUS IN ('OPEN','ACK'), so a
    snoozed event drops off EVERY feed (Alerts, Brief, Control Room) with ZERO
    read-path change -- and, crucially, no ordering hazard: the feed reads are
    byte-identical before V086 is applied. The still-present row keeps the
    scanner's dedupe from re-raising the same condition while it sleeps.
  * A wake step spliced into the hourly SP_ALERT_SCAN returns expired snoozes
    (SNOOZED_UNTIL <= now) to STATUS='OPEN', so they self-resurface within the
    hour. It is isolated in its own BEGIN..EXCEPTION and does NOT touch the
    rule-block `fails` counter (it is not a rule).
  * SP_ALERT_SNOOZE sets the snooze atomically with an ALERT_AUDIT row and
    OW_ACTION_INTENTS idempotency, mirroring SP_ALERT_LIFECYCLE (ack/resolve).

Additive + idempotent: ALTER ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE procs.
SP_ALERT_SCAN is re-derived from V084; the ONLY edit is the wake block spliced
before arm [01], so reversing it reproduces the V084 body byte-for-byte. The
scanner is NOT fired at apply time. Owner applies in Snowsight after V085.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_ALERTSCAN = MIG / "V084__security_new_exposure_alert.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


# The wake step. Runs before the rule arms, in its own isolated BEGIN..EXCEPTION
# so a wake failure logs and the scan proceeds; it never touches `fails` (not a rule).
WAKE_BLOCK = """    -- [wake] V086: return expired per-event snoozes to the triage feed. A snoozed
    -- event sits at STATUS='SNOOZED' (off the OPEN/ACK feed); once its wake time has
    -- passed it goes back to OPEN so it re-surfaces. Isolated + does NOT touch `fails`.
    BEGIN
        -- Restore the TRUE prior status: an ACK'd event that was snoozed wakes back
        -- to ACK (its ACK_BY/ACK_AT are intact), a never-acked one to OPEN. Waking an
        -- acked event to OPEN would strand a stale ACK_AT on an 'open' row and let a
        -- re-ack overwrite it (inflating MTTA). Clear the transient snooze metadata.
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN'),
               SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL
         WHERE STATUS = 'SNOOZED'
           AND SNOOZED_UNTIL IS NOT NULL
           AND SNOOZED_UNTIL <= CURRENT_TIMESTAMP();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_wake_failed', :emsg,
                   'V086 un-snooze - other rules unaffected', CURRENT_ROLE();
    END;

"""

_ANCHOR = "    -- [01] COST_DAILY_CREDITS\n"


def derive_scan(base: Path) -> str:
    """Extract SP_ALERT_SCAN from V084 and splice the wake step before arm [01]."""
    proc = extract_proc(base.read_text(encoding="utf-8"), "SP_ALERT_SCAN")
    assert "[wake]" not in proc and "STATUS = 'SNOOZED'" not in proc, "wake already present"
    assert proc.count(_ANCHOR) == 1, f"expected 1 arm-[01] anchor, got {proc.count(_ANCHOR)}"
    proc = proc.replace(_ANCHOR, WAKE_BLOCK + _ANCHOR, 1)
    assert proc.count("-- [wake] V086") == 1
    assert "SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN')," in proc
    return proc


alert_scan = derive_scan(BASE_ALERTSCAN)

# Sanity: the re-derived proc kept its V084 identity (arm [20] + the 15-count).
assert "-- [20] SEC_NEW_EXPOSURE" in alert_scan
assert "/15 rule blocks ok" in alert_scan
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()" in alert_scan

out = f"""-- V086__alert_snooze.sql
--
-- Per-event alert snooze (CoCo Alerts29). Silence one specific alert until a wake
-- time ("handle this Monday") without acking or resolving it.
--
-- Additive, idempotent:
--   * ALTER ALERT_EVENTS ADD SNOOZED_UNTIL / SNOOZE_BY / SNOOZE_REASON.
--   * SP_ALERT_SNOOZE: set STATUS='SNOOZED' + the wake time on OPEN/ACK events,
--     atomically with an ALERT_AUDIT row and OW_ACTION_INTENTS idempotency
--     (mirrors SP_ALERT_LIFECYCLE). A snoozed event leaves the OPEN/ACK triage
--     feed with NO read-path change on any page (no ordering hazard); the
--     still-present row keeps the scanner dedupe from re-raising it while asleep.
--   * Re-derive SP_ALERT_SCAN (from V084) with a wake step that returns expired
--     snoozes to OPEN so they self-resurface within the hour. The ONLY edit vs
--     V084 is the wake block spliced before arm [01]; reversing it reproduces the
--     V084 body byte-for-byte. Isolated + does not touch the rule-block count.
--
-- The scanner is NOT fired at apply time. Owner applies in Snowsight after V085.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20086, 'V086 requires V085 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 85) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Additive columns for the per-event snooze (the wake step + feed reads use them).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS SNOOZED_UNTIL TIMESTAMP_NTZ;
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS SNOOZE_BY VARCHAR(200);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS SNOOZE_REASON VARCHAR(1000);

-- Set (or clear) a per-event snooze atomically, audited + idempotent (mirrors
-- SP_ALERT_LIFECYCLE). The wake time is computed SERVER-SIDE from a duration, so the
-- caller never reasons about the account clock: P_SNOOZE_HOURS in [0, 8760], where 0
-- means un-snooze NOW (early), restoring the event's true prior status.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SNOOZE(
    P_EVENT_IDS VARCHAR, P_SNOOZE_HOURS FLOAT, P_REASON VARCHAR,
    P_ACTOR VARCHAR, P_IDEM_KEY VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    v_hours FLOAT;
    v_until TIMESTAMP_NTZ;
    v_actor VARCHAR;
    v_note VARCHAR;
    v_n NUMBER;
BEGIN
    IF (COALESCE(TRIM(:P_IDEM_KEY), '') = '') THEN
        RETURN 'BLOCKED: missing idempotency key';
    END IF;
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS WHERE IDEM_KEY = :P_IDEM_KEY)) THEN
        RETURN 'DUPLICATE: ' || :P_IDEM_KEY;
    END IF;
    v_hours := COALESCE(:P_SNOOZE_HOURS, -1);
    IF (:v_hours < 0 OR :v_hours > 8760) THEN
        RETURN 'BLOCKED: snooze hours must be in [0, 8760] (0 = un-snooze now)';
    END IF;
    v_actor := COALESCE(NULLIF(:P_ACTOR, ''), CURRENT_USER());
    BEGIN TRANSACTION;
    IF (:v_hours = 0) THEN
        -- Un-snooze NOW: restore the TRUE prior status (ACK if it was acked, else
        -- OPEN) -- the same transition the hourly wake step performs. Only SNOOZED
        -- rows are touched, so an already-woken/resolved event is a no-op.
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
        SELECT EVENT_ID, 'UNSNOOZE', 'un-snooze (early) — by ' || :v_actor, :v_actor
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS = 'SNOOZED';
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN'),
               SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL
         WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
           AND STATUS = 'SNOOZED';
        v_n := SQLROWCOUNT;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS (IDEM_KEY, KIND, ACTOR, RESULT)
        VALUES (:P_IDEM_KEY, 'ALERT_UNSNOOZE', :P_ACTOR, :v_n || ' event(s)');
        COMMIT;
        RETURN 'OK: ' || :v_n || ' event(s) un-snoozed';
    END IF;
    -- Snooze (hours > 0): server-computed wake time.
    v_until := DATEADD('minute', ROUND(:v_hours * 60), CURRENT_TIMESTAMP())::TIMESTAMP_NTZ;
    v_note := 'until ' || TO_VARCHAR(:v_until) || ' (' || :v_hours || 'h)'
              || IFF(COALESCE(:P_REASON, '') <> '', ' — ' || :P_REASON, '')
              || ' — by ' || :v_actor;
    -- Audit BEFORE the update, on the same OPEN/ACK pre-state, so audit rows match
    -- exactly the events this call snoozes (already-snoozed/resolved events skipped).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
    SELECT EVENT_ID, 'SNOOZE', :v_note, :v_actor
      FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
     WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
       AND STATUS IN ('OPEN', 'ACK');
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET STATUS = 'SNOOZED', SNOOZED_UNTIL = :v_until, SNOOZE_BY = :v_actor,
           SNOOZE_REASON = LEFT(COALESCE(:P_REASON, ''), 1000)
     WHERE EVENT_ID IN (SELECT TRIM(VALUE)::VARCHAR FROM TABLE(SPLIT_TO_TABLE(:P_EVENT_IDS, ',')))
       AND STATUS IN ('OPEN', 'ACK');
    v_n := SQLROWCOUNT;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_ACTION_INTENTS (IDEM_KEY, KIND, ACTOR, RESULT)
    VALUES (:P_IDEM_KEY, 'ALERT_SNOOZE', :P_ACTOR, :v_n || ' event(s)');
    COMMIT;
    RETURN 'OK: ' || :v_n || ' event(s) snoozed';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (+ [wake] un-snooze step before arm [01], from V084)
{alert_scan}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 86 AS VERSION,
       'Per-event alert snooze (CoCo Alerts29): ALTER ALERT_EVENTS ADD SNOOZED_UNTIL/SNOOZE_BY/SNOOZE_REASON; SP_ALERT_SNOOZE sets STATUS=SNOOZED + wake time atomically (audited, idempotent; P_SNOOZE_HOURS=0 un-snoozes early, restoring the true prior status). A snoozed event leaves the OPEN/ACK triage feed with no read-path change (no ordering hazard); a wake step spliced into the hourly SP_ALERT_SCAN (re-derived from V084) returns expired snoozes to their prior status (ACK if acked, else OPEN). The only SP_ALERT_SCAN edit vs V084 is the wake block; the scanner is not fired at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 86);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 2      # SP_ALERT_SNOOZE + SP_ALERT_SCAN
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20086" in out and "IF (v < 85) THEN" in out
assert "SELECT 86 AS VERSION" in out and "WHERE VERSION = 86)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in out   # never fire at apply
assert out.count("ADD COLUMN IF NOT EXISTS") == 3
assert out.count("-- [wake] V086") == 1
assert "STATUS = 'SNOOZED'" in out and "SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN')," in out

target = Path(os.environ.get("V086_OUT") or (MIG / "V086__alert_snooze.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
