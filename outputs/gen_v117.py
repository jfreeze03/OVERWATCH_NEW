#!/usr/bin/env python3
"""Forward-generate V117: SP_ALERT_SCAN gains a snooze-suppress sweep.

Re-derives SP_ALERT_SCAN (latest def = V115) with ONE new post-raise sweep spliced before the
final RETURN (after the V091 auto-clear sweep). Everything else byte-identical to V115.

[MED] Multi-day snooze is silently defeated by the scan's date-banded dedupe. A per-event snooze
   sets STATUS='SNOOZED' on one event, keeping its DEDUPE_KEY. Nearly every rule bands its key by
   day/week (RULE|entity|<date>), and the re-raise guard matches the EXACT key, so when the band
   rolls the next day the raise arms mint a brand-new OPEN event for the same rule+entity even
   though it is snoozed -- a "1 week" snooze silences nothing past the first day (V086's own header
   promises the still-present row keeps the scanner from re-raising it, an invariant false for
   banded keys). Fix: after the raise arms, resolve any OPEN event whose BAND-INDEPENDENT identity
   (its DEDUPE_KEY minus a trailing |YYYY-MM-DD date/week token) matches an ACTIVE snooze
   (STATUS='SNOOZED', wake time in the future) for the same rule. The re-raise is minted then
   resolved WITHIN this scan, so the separate webhook sender never sees the transient OPEN.
   RESOLUTION_KIND='SNOOZE_SUPPRESSED' is excluded from per-rule precision exactly like
   SUPERSEDED/AUTO_CLEARED. The original SNOOZED row is untouched and still wakes on schedule.

   Band-independent identity uses TRY_TO_DATE (no regex, no string-escape ambiguity): strip a
   trailing |YYYY-MM-DD only when the last 10 chars parse as a date and the 11th-from-end is '|'.
   All date/week bands render as a bare ISO date (CURRENT_DATE / f.DAY / DATE_TRUNC('week',...));
   entity-only keys (RULE|USER|IP, RULE|PRIV|GRANTED_ON|<timestamp>) do NOT end in a bare date, so
   they are never stripped -- their identity is the full key and their snooze already worked
   (the SNOOZED row's exact key blocks the guard), so this sweep never touches them.

   RESIDUAL (documented, not fixed here): on the WAKE scan the original snooze reopens with its
   now-stale banded key while the raise arm also mints today's fresh key, so the woken event and a
   fresh re-raise can both be OPEN for that one scan until an operator resolves them. Fixing that
   cleanly needs a wake-step change (resolve-and-re-raise) with weekly-band tradeoffs; deferred.

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

SWEEP = """    -- [snooze-suppress sweep] V117: a per-event snooze keeps the event's date-banded
    -- DEDUPE_KEY, so when the day/week band rolls the raise arms above mint a NEW OPEN event
    -- for the SAME rule+entity even though it is snoozed -- silently defeating a multi-day
    -- snooze after the first day. Resolve any OPEN event whose band-independent identity (its
    -- DEDUPE_KEY minus a trailing |YYYY-MM-DD date/week token) matches an ACTIVE snooze
    -- (STATUS='SNOOZED', wake time still in the future) for the same rule. The re-raise is
    -- minted then resolved WITHIN this scan, so the separate webhook sender never sees the
    -- transient OPEN. RESOLUTION_KIND='SNOOZE_SUPPRESSED' is excluded from per-rule precision/
    -- MTTR like SUPERSEDED/AUTO_CLEARED; the original SNOOZED row is untouched and wakes on
    -- schedule. Entity-only keys (IP, grant timestamp) do not end in a bare ISO date, so their
    -- identity is the full key -- their snooze already worked and is unchanged. TRY_TO_DATE
    -- (not regex) strips the trailing date to avoid string-escape ambiguity. Wrapped so a sweep
    -- failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'
         WHERE ev.STATUS = 'OPEN'
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
               WHERE s.STATUS = 'SNOOZED'
                 AND s.SNOOZED_UNTIL > CURRENT_TIMESTAMP()
                 AND s.RULE_ID = ev.RULE_ID
                 AND s.EVENT_ID <> ev.EVENT_ID
                 AND IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'
                         AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL,
                         LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11), s.DEDUPE_KEY)
                     = IFF(SUBSTR(ev.DEDUPE_KEY, -11, 1) = '|'
                           AND TRY_TO_DATE(RIGHT(ev.DEDUPE_KEY, 10)) IS NOT NULL,
                           LEFT(ev.DEDUPE_KEY, LENGTH(ev.DEDUPE_KEY) - 11), ev.DEDUPE_KEY)
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_suppress_sweep_failed', :emsg, 'V117 snooze-suppress sweep - other rules unaffected', CURRENT_ROLE();
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
# prove we re-derived from V115 (the supersede-ACK fix is intact)
assert "WHERE lo.STATUS IN ('OPEN', 'ACK')" in proc
assert proc.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1   # auto-clear sweep still there

proc = proc.replace(ANCHOR, SWEEP + ANCHOR)

# post-conditions
assert proc.count("RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'") == 1
assert "s.STATUS = 'SNOOZED'\n                 AND s.SNOOZED_UNTIL > CURRENT_TIMESTAMP()" in proc
assert "TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10))" in proc and "TRY_TO_DATE(RIGHT(ev.DEDUPE_KEY, 10))" in proc
assert proc.count("snooze_suppress_sweep_failed") == 1
# untouched machinery survives
assert proc.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1
assert proc.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1

out = f"""-- V117__alert_snooze_suppress_sweep.sql
--
-- SP_ALERT_SCAN gains a snooze-suppress sweep (proc re-derived from V115):
--   [MED] A per-event snooze keeps the event's date-banded DEDUPE_KEY, so when the day/week band
--   rolls the raise arms mint a NEW OPEN event for the same rule+entity even though it is snoozed
--   -- a multi-day snooze silenced nothing past the first day. A new post-raise sweep resolves any
--   OPEN event whose band-independent identity (DEDUPE_KEY minus a trailing |YYYY-MM-DD token,
--   detected with TRY_TO_DATE, not regex) matches an ACTIVE snooze for the same rule. The re-raise
--   is minted then resolved within the scan (the webhook sender never sees it);
--   RESOLUTION_KIND='SNOOZE_SUPPRESSED' is excluded from precision. Entity-only keys (IP, grant
--   time) are never stripped -- their snooze already worked. The sibling supersede + auto-clear
--   sweeps and everything else are byte-identical to V115.
--
-- Residual (documented): on the WAKE scan the reopened stale original and a fresh re-raise can both
-- be OPEN for one scan until resolved; a clean fix needs a wake-step change and is deferred.
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
       'SP_ALERT_SCAN snooze-suppress sweep (re-derived from V115): a per-event snooze keeps the events date-banded DEDUPE_KEY, so when the day/week band rolled the raise arms minted a fresh OPEN for the same rule+entity and a multi-day snooze silenced nothing past the first day. A new post-raise sweep resolves any OPEN event whose band-independent identity (DEDUPE_KEY minus a trailing bare-date token, via TRY_TO_DATE) matches an active snooze for the same rule, with RESOLUTION_KIND=SNOOZE_SUPPRESSED (excluded from precision). Entity-only keys are never stripped. Supersede + auto-clear sweeps byte-identical. Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
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
