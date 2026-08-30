#!/usr/bin/env python3
"""Forward-generate V096: alert-scan dedupe/clear keys that encode the state they need.

Three related alerting defects, all from a dedupe/clear key not carrying the band/state
token (or the sweep matching the wrong token). Re-derives TWO procs:

  * SP_ALERT_SCAN (from its LATEST def, V091) -- two edits:
    (1) [MED] auto-clear never fires for next-day-cleared conditions. The V091 auto-clear
        sweep restricts candidates to `ev.DEDUPE_KEY LIKE '%|' || CURRENT_DATE()` (today's
        bucket), but the three opted-in rules stamp the key with CURRENT_DATE() at raise and
        measure over a trailing-24h window -- so a condition only drops below the CLEAR floor
        ~24h after onset (the NEXT calendar day), by which time the key's date no longer
        matches the sweep's CURRENT_DATE() and the event strands OPEN. Fix: swap the
        date-in-key filter for a RAISED_AT recency lower bound (>= 48h ago), keeping the
        existing >=1h dwell upper bound and the below-CLEAR hysteresis NOT-IN subquery. The
        rule-scope filter (ENABLED + AUTO_CLEAR_ENABLED) already excludes fact-day rules, so
        historical day-stamped exceedances are never rewritten. (A prior-day event whose
        scope is still firing is superseded by today's freshly-raised event -- desirable
        de-dup, not a lost alert.)
    (3) [LOW] SEC_CRED_EXPIRY EXPIRING->EXPIRED double-count. The arm escalates HIGH->CRITICAL
        on expiry and swaps the |EXPIRING|/|EXPIRED| state token in the key, so the EXPIRED
        CRITICAL fires as a distinct event while the EXPIRING HIGH stays OPEN -- one credential
        counted as both. Fix: extend the V067 #40 supersede sweep OR-list to also resolve the
        lower event when its CRITICAL sibling shares the key with |EXPIRING| swapped to
        |EXPIRED|.
    (2b) the supersede OR-list also gains a |HIGH|->|CRIT| term for the SLO burn band below.

  * SP_SLO_BREACH_SCAN (from V085) -- one edit:
    (2) [MED] same-day HIGH->CRITICAL escalation swallowed. The arm escalates SEVERITY to
        CRITICAL at burn>=2x but the dedupe key is RULE_ID|SLO_ID|CURRENT_DATE() with NO burn
        band, and the INSERT is NOT EXISTS-guarded, so a later same-day CRITICAL collides with
        the earlier HIGH on the identical key and inserts nothing. Fix: append a burn band
        token (IFF(COALESCE(BURN_MULTIPLE,0)>=2,'CRIT','HIGH')) as its own pipe segment before
        the date, mirroring PIPE_COPY_FAILURES (V066:478) / COST_DEPT_BUDGET_PACE (V066:567),
        so HIGH and CRIT get distinct keys and the escalation survives the guard. The SP_ALERT_SCAN
        supersede sweep's new |HIGH|->|CRIT| term then resolves the superseded HIGH.

Two procedure re-derivations, no schema change, no new object, no backfill. Owner applies in
Snowsight after V095. Forward-healing: auto-clear/escalation start working on the next scan;
existing stranded-OPEN / double-counted events may need a one-time manual resolve or age out.
This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_ALERT = MIG / "V091__alert_auto_clear.sql"
BASE_SLO = MIG / "V085__slo_breach_alert.sql"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


# ---- SP_ALERT_SCAN edits (from V091) --------------------------------------------
# (1) auto-clear candidate set: date-in-key -> RAISED_AT recency. Two count==1 swaps
#     (the code predicate + its now-stale inline comment), leaving the >=1h dwell on
#     the following line and the hysteresis NOT-IN subquery intact.
AC_OLD_CODE = "AND ev.DEDUPE_KEY LIKE '%|' || CURRENT_DATE()"
AC_NEW_CODE = "AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())"
AC_OLD_NOTE = "-- today's live bucket only"
AC_NEW_NOTE = "-- V096: recent window (was date-in-key); catches next-day-cleared 24h conditions"

# (2b + 3) supersede sweep OR-list: add |HIGH|->|CRIT| (SLO burn) and
#     |EXPIRING|->|EXPIRED| (cred expiry) terms.
SUP_OLD = (
    "                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|'))"
)
SUP_NEW = (
    "                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|'))"
)

# (comment) keep the supersede-sweep design note accurate about the tokens now covered.
SUPC_OLD = (
    "    -- tokens '|WARN|'/'|MED|' occur only in the three banded keys, so this is a no-op for\n"
    "    -- every other rule. Wrapped so a sweep failure never breaks the scan."
)
SUPC_NEW = (
    "    -- tokens '|WARN|'/'|MED|'/'|HIGH|'/'|EXPIRING|' occur only in banded/state keys, so this\n"
    "    -- is a no-op for every other rule (V096 adds |HIGH|->|CRIT| for the SLO burn band and\n"
    "    -- |EXPIRING|->|EXPIRED| for cred expiry). Wrapped so a sweep failure never breaks the scan."
)

alert = extract_procedure(BASE_ALERT.read_text(encoding="utf-8"), "SP_ALERT_SCAN")
for old in (AC_OLD_CODE, AC_OLD_NOTE, SUP_OLD, SUPC_OLD):
    assert alert.count(old) == 1, f"SP_ALERT_SCAN: expected 1 of {old!r}, got {alert.count(old)}"
alert = (alert.replace(AC_OLD_CODE, AC_NEW_CODE)
              .replace(AC_OLD_NOTE, AC_NEW_NOTE)
              .replace(SUP_OLD, SUP_NEW)
              .replace(SUPC_OLD, SUPC_NEW))
# fix landed and the bug is gone
assert AC_NEW_CODE in alert and "ev.DEDUPE_KEY LIKE '%|'" not in alert
assert "REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')" in alert
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" in alert
# untouched anchors: dwell upper bound, hysteresis subquery, the SEC key state token,
# the existing two supersede terms
for anchor in (
    "AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())",   # dwell survives
    "COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)",         # hysteresis survives
    "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')",  # SEC key
    "REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')",
    "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')",
    "RESOLUTION_KIND = 'AUTO_CLEARED'",
):
    assert anchor in alert, anchor
assert alert.count("COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)") == 3

# ---- SP_SLO_BREACH_SCAN edit (from V085) ----------------------------------------
SLO_OLD = "           c.RULE_ID || '|' || e.SLO_ID || '|' || TO_VARCHAR(CURRENT_DATE())"
SLO_NEW = ("           c.RULE_ID || '|' || e.SLO_ID || '|' "
           "|| IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRIT', 'HIGH') "
           "|| '|' || TO_VARCHAR(CURRENT_DATE())")

slo = extract_procedure(BASE_SLO.read_text(encoding="utf-8"), "SP_SLO_BREACH_SCAN")
assert slo.count(SLO_OLD) == 1, f"SP_SLO_BREACH_SCAN: expected 1 SLO key, got {slo.count(SLO_OLD)}"
slo = slo.replace(SLO_OLD, SLO_NEW)
assert "IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRIT', 'HIGH')" in slo
# untouched: the severity escalation (same predicate) + the NOT EXISTS dedup guard
assert "IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRITICAL', c.SEVERITY)" in slo
assert "WHERE e.DEDUPE_KEY = b.DEDUPE_KEY" in slo

out = f"""-- V096__alert_scan_dedupe_keys.sql
--
-- Alert-scan dedupe/clear keys that encode the state they need. Three related alerting
-- defects, all from a dedupe/clear key not carrying the band/state token (or the sweep
-- matching the wrong token). Re-derives two procs from their latest defs:
--
--   SP_ALERT_SCAN (from V091):
--     (1) [MED] auto-clear never fires for next-day-cleared conditions -- the sweep matched
--         `DEDUPE_KEY LIKE '%|' || CURRENT_DATE()` (today only) but trailing-24h conditions
--         drop below CLEAR the NEXT day, stranding the event OPEN. Now matches a RAISED_AT
--         recency window (>= 48h ago) with the existing >=1h dwell + hysteresis NOT-IN kept;
--         the ENABLED+AUTO_CLEAR_ENABLED rule scope still excludes fact-day rules.
--     (3) [LOW] SEC_CRED_EXPIRY EXPIRING->EXPIRED double-count -- supersede sweep now resolves
--         the EXPIRING HIGH when its EXPIRED CRITICAL sibling exists (|EXPIRING|->|EXPIRED|).
--     (2b) supersede sweep also maps |HIGH|->|CRIT| for the SLO burn band below.
--
--   SP_SLO_BREACH_SCAN (from V085):
--     (2) [MED] same-day HIGH->CRITICAL escalation swallowed -- the dedupe key had no burn
--         band, so the escalated CRITICAL collided with the earlier HIGH on the identical key
--         and the NOT EXISTS guard suppressed it. Now appends a burn band token
--         (IFF(COALESCE(BURN_MULTIPLE,0)>=2,'CRIT','HIGH')) as its own pipe segment before the
--         date, so HIGH and CRIT get distinct keys (mirrors V066 PIPE/BUDGET), and the
--         supersede sweep resolves the superseded HIGH.
--
-- Two procedure re-derivations, no schema change, no new object, no backfill. Owner applies
-- in Snowsight after V095. Forward-healing (next scan); pre-existing stranded/double-counted
-- events may need a one-time manual resolve or age out. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20096, 'V096 requires V095 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 95) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{alert}
{slo}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 96 AS VERSION,
       'Alert-scan dedupe/clear keys: SP_ALERT_SCAN re-derived from V091 (auto-clear sweep now matches a RAISED_AT >= 48h recency window instead of DEDUPE_KEY LIKE today, so trailing-24h conditions that clear the next day auto-resolve instead of stranding OPEN -- dwell + below-CLEAR hysteresis kept, rule scope still excludes fact-day rules; supersede sweep OR-list extended with |HIGH|->|CRIT| and |EXPIRING|->|EXPIRED|) plus SP_SLO_BREACH_SCAN re-derived from V085 (dedupe key gains a burn band token IFF(BURN_MULTIPLE>=2, CRIT, HIGH) so a same-day HIGH->CRITICAL escalation gets a distinct key and is not swallowed by the NOT EXISTS guard). Two procs, no schema change, no backfill; forward-healing on the next scan.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 96);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "ev.DEDUPE_KEY LIKE '%|'" not in out          # the auto-clear date-in-key bug is gone
assert "REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')" in out
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" in out
assert "IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRIT', 'HIGH')" in out
assert "EXCEPTION (-20096" in out and "IF (v < 95) THEN" in out
assert "SELECT 96 AS VERSION" in out and "WHERE VERSION = 96)" in out

target = Path(os.environ.get("V096_OUT") or (MIG / "V096__alert_scan_dedupe_keys.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
