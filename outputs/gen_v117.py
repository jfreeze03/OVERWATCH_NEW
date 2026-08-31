#!/usr/bin/env python3
"""Forward-generate V117: SP_ALERT_SCAN gains a snooze CARRY-FORWARD sweep.

Re-derives SP_ALERT_SCAN (latest def = V115) with ONE new post-raise sweep spliced before the
final RETURN (after the V091 auto-clear sweep). Everything else byte-identical to V115.

[MED] Multi-day snooze is silently defeated by the scan's date-banded dedupe. A per-event snooze
   sets STATUS='SNOOZED' on one event, keeping its DEDUPE_KEY. Nearly every rule bands its key by
   day/week (RULE|entity|<date>), and the re-raise guard matches the EXACT key, so when the band
   rolls the next day the raise arms mint a brand-new OPEN event for the same rule+entity even
   though it is snoozed -- a "1 week" snooze silences nothing past the first day.

   FIX = carry the snooze forward (NOT resolve the re-raise). An earlier resolve-the-re-raise
   design left a RESOLVED row occupying each day's key; after a mid-day wake the current band
   could not re-mint (key occupied) so only a STALE-numbers original showed (self-review finding).
   Instead: (1) snooze the fresh same-identity re-raise, inheriting the active snooze's wake time,
   so it holds the CURRENT band's data and wakes on schedule; (2) resolve the now-superseded older
   snoozed row so exactly ONE snoozed row (the latest band) survives. On wake it reopens once with
   current numbers. RESOLUTION_KIND='SNOOZE_SUPPRESSED' (machine close, excluded from precision).

   Band-independent identity uses TRY_TO_DATE (no regex, no string-escape ambiguity): strip a
   trailing |YYYY-MM-DD only when the last 10 chars parse as a date and the 11th-from-end is '|'.
   All date/week bands render as a bare ISO date; entity-only keys (RULE|USER|IP,
   RULE|PRIV|GRANTED_ON|<timestamp>) do NOT end in a bare date so they are never stripped -- their
   identity is the full key and their snooze already worked (the SNOOZED row's exact key blocks the
   guard). ev.RAISED_AT > s.RAISED_AT restricts carry-forward to GENUINE future re-raises, so a
   pre-existing untriaged OPEN sibling from an earlier band is left in the queue for a human
   (self-review finding), never machine-closed.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V116; the next
hourly SP_ALERT_SCAN evaluates the new sweep. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V115__alert_supersede_includes_ack.sql"

ANCHOR = (
    "    RETURN 'alert scan v11 (V091: + auto-clear sweep): ' || (16 - :fails) "
    "|| '/16 rule blocks ok';"
)

# The band-independent identity: strip a trailing |YYYY-MM-DD from KEY only when the last 10
# chars parse as a date and the char before them is '|'. Same for both aliases in each compare.
def _ident(alias: str) -> str:
    k = f"{alias}.DEDUPE_KEY"
    return (f"IFF(SUBSTR({k}, -11, 1) = '|'\n"
            f"                     AND TRY_TO_DATE(RIGHT({k}, 10)) IS NOT NULL,\n"
            f"                     LEFT({k}, LENGTH({k}) - 11), {k})")


SWEEP = f"""    -- [snooze carry-forward sweep] V117: a per-event snooze keeps the event's date-banded
    -- DEDUPE_KEY, so when the day/week band rolls the raise arms above mint a NEW OPEN event for
    -- the SAME rule+entity even though it is snoozed -- silently defeating a multi-day snooze.
    -- Carry the snooze FORWARD onto the re-raise (do NOT resolve it: a resolved row would occupy
    -- the day's key and, after a mid-day wake, block the current band from re-minting so only a
    -- STALE-numbers original showed). (1) snooze the fresh same-identity re-raise, inheriting the
    -- active snooze's wake time, so it carries the CURRENT band's data and wakes on schedule;
    -- (2) resolve the now-superseded older snoozed row so exactly ONE snoozed row (the latest
    -- band, current data) survives and reopens once on wake. Band-independent identity strips a
    -- trailing |YYYY-MM-DD via TRY_TO_DATE (no regex). ev.RAISED_AT > s.RAISED_AT restricts to
    -- GENUINE future re-raises, leaving a pre-existing untriaged OPEN sibling for a human. Entity-
    -- only keys (IP, grant time) never end in a bare date so they are never stripped -- untouched.
    -- RESOLUTION_KIND='SNOOZE_SUPPRESSED' is a machine close excluded from precision. Wrapped so a
    -- sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'SNOOZED',
               SNOOZED_UNTIL = s.SNOOZED_UNTIL,
               SNOOZE_BY = s.SNOOZE_BY,
               SNOOZE_REASON = s.SNOOZE_REASON
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
         WHERE ev.STATUS = 'OPEN'
           AND s.STATUS = 'SNOOZED'
           AND s.SNOOZED_UNTIL > CURRENT_TIMESTAMP()
           AND s.RULE_ID = ev.RULE_ID
           AND s.EVENT_ID <> ev.EVENT_ID
           AND ev.RAISED_AT > s.RAISED_AT
           AND {_ident('s')}
               = {_ident('ev')};
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'
         WHERE s.STATUS = 'SNOOZED'
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s2
               WHERE s2.STATUS = 'SNOOZED'
                 AND s2.EVENT_ID <> s.EVENT_ID
                 AND s2.RULE_ID = s.RULE_ID
                 AND s2.RAISED_AT > s.RAISED_AT
                 AND {_ident('s2')}
                     = {_ident('s')}
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_carry_forward_failed', :emsg, 'V117 snooze carry-forward sweep - other rules unaffected', CURRENT_ROLE();
    END;

"""


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")

assert proc.count(ANCHOR) == 1, f"return anchor: got {proc.count(ANCHOR)}"
assert "SNOOZE_SUPPRESSED" not in proc, "sweep already present"
assert "WHERE lo.STATUS IN ('OPEN', 'ACK')" in proc          # re-derived from V115
assert proc.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1

proc = proc.replace(ANCHOR, SWEEP + ANCHOR)

# post-conditions
assert proc.count("RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'") == 1
assert "SET STATUS = 'SNOOZED',\n               SNOOZED_UNTIL = s.SNOOZED_UNTIL" in proc  # carry-forward
assert "ev.RAISED_AT > s.RAISED_AT" in proc                  # only genuine re-raises (finding 5)
assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s" in proc  # UPDATE...FROM inherit
assert "s2.RAISED_AT > s.RAISED_AT" in proc                  # resolve superseded older
assert proc.count("TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10))") == 2   # stmt1 + stmt2 use s
assert proc.count("snooze_carry_forward_failed") == 1
assert proc.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1     # untouched
assert proc.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1

out = f"""-- V117__alert_snooze_suppress_sweep.sql
--
-- SP_ALERT_SCAN gains a snooze CARRY-FORWARD sweep (proc re-derived from V115):
--   [MED] A per-event snooze keeps the event's date-banded DEDUPE_KEY, so when the day/week band
--   rolls the raise arms mint a NEW OPEN event for the same rule+entity even though it is snoozed
--   -- a multi-day snooze silenced nothing past the first day. A new post-raise sweep carries the
--   snooze forward: (1) snoozes the fresh same-identity re-raise inheriting the active snooze's
--   wake time (so it holds the CURRENT band's data and wakes on schedule), (2) resolves the now-
--   superseded older snoozed row (RESOLUTION_KIND='SNOOZE_SUPPRESSED', excluded from precision) so
--   exactly ONE snoozed row survives and reopens once on wake with current numbers. Band-
--   independent identity strips a trailing |YYYY-MM-DD via TRY_TO_DATE (not regex); entity-only
--   keys are never stripped. ev.RAISED_AT > s.RAISED_AT restricts to genuine re-raises, leaving a
--   pre-existing untriaged OPEN sibling for a human. Supersede + auto-clear sweeps byte-identical
--   to V115.
--
-- No schema change; owner applies after V116 and the next hourly SP_ALERT_SCAN runs the sweep.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20117, 'V117 requires V116 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 116) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 117 AS VERSION,
       'SP_ALERT_SCAN snooze carry-forward sweep (re-derived from V115): a per-event snooze keeps the events date-banded DEDUPE_KEY, so when the day/week band rolled the raise arms minted a fresh OPEN for the same rule+entity and a multi-day snooze silenced nothing past the first day. A new post-raise sweep carries the snooze forward onto the fresh re-raise (inheriting the wake time) and resolves the superseded older snoozed row (RESOLUTION_KIND=SNOOZE_SUPPRESSED, excluded from precision), so one snoozed row with current-band data survives and reopens once on wake. Band-independent identity strips a trailing bare-date token via TRY_TO_DATE; entity-only keys are never stripped; ev.RAISED_AT > s.RAISED_AT leaves a pre-existing untriaged OPEN sibling for a human. Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 117);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20117" in out and "IF (v < 116) THEN" in out
assert "SELECT 117 AS VERSION" in out and "WHERE VERSION = 117)" in out

target = Path(os.environ.get("V117_OUT") or (MIG / "V117__alert_snooze_suppress_sweep.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
