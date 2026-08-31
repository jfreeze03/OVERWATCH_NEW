#!/usr/bin/env python3
"""Forward-generate V115: SP_ALERT_SCAN escalation-supersede includes ACK'd events.

Re-derives SP_ALERT_SCAN (latest def = V110) with one correction from the alert-layer hunt:

[MED] The V067 #40 escalation-supersede sweep resolves the lower-band event only when BOTH the
   lower (lo, alias) and the higher (hi) sibling are STATUS='OPEN'. But the house open-count
   convention counts STATUS IN ('OPEN','ACK') -- an ACK'd event still counts as open and still
   shows in the feed + score. So when a banded rule escalates after an ACK (e.g. a WARN is
   acknowledged, then burn worsens and a CRIT raises), the ACK'd lower event is never superseded
   and the SAME incident double-counts as two open alerts with a doubled platform-score penalty --
   exactly the outcome the sweep exists to prevent. Fix: broaden both sides of the sweep to
   STATUS IN ('OPEN','ACK'). Manual RESOLVED / active SNOOZED stay excluded as before.

Only the supersede sweep changes. The sibling auto-clear sweep (ev alias) stays OPEN-only BY
DESIGN -- an ACK there means "a human is actively working it", so a below-threshold condition must
not auto-close it. That carve-out is asserted untouched.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V114; the next
hourly SP_ALERT_SCAN evaluates the corrected sweep. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V110__alerting_hunt_sp_alert_scan_fixes.sql"

# The two predicates are alias-qualified, so each is unique to the supersede sweep
# (the auto-clear sweep uses the ev alias; raise arms use e).
OLD_LO = "WHERE lo.STATUS = 'OPEN'"
NEW_LO = "WHERE lo.STATUS IN ('OPEN', 'ACK')"
OLD_HI = "WHERE hi.STATUS = 'OPEN'"
NEW_HI = "WHERE hi.STATUS IN ('OPEN', 'ACK')"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")

assert proc.count(OLD_LO) == 1, f"lo predicate: got {proc.count(OLD_LO)}"
assert proc.count(OLD_HI) == 1, f"hi predicate: got {proc.count(OLD_HI)}"
# the auto-clear sweep is OPEN-only by design and must be left alone
assert proc.count("WHERE ev.STATUS = 'OPEN'") == 1, "auto-clear ev predicate not found"

proc = proc.replace(OLD_LO, NEW_LO)
proc = proc.replace(OLD_HI, NEW_HI)

# post-conditions
assert "WHERE lo.STATUS IN ('OPEN', 'ACK')" in proc
assert "WHERE hi.STATUS IN ('OPEN', 'ACK')" in proc
assert "lo.STATUS = 'OPEN'" not in proc and "hi.STATUS = 'OPEN'" not in proc
# the auto-clear OPEN-only carve-out survives untouched
assert proc.count("WHERE ev.STATUS = 'OPEN'") == 1
# supersede machinery we did NOT touch survives (V110's EXH + terminal-EXPIRING arms)
assert "REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')" in proc
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))" in proc
assert proc.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1
# V110's other fixes intact (proves we re-derived from V110, not an older def)
assert "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in proc

out = f"""-- V115__alert_supersede_includes_ack.sql
--
-- SP_ALERT_SCAN escalation-supersede includes ACK'd events (proc re-derived from V110):
--   [MED] The V067 #40 supersede sweep resolved the lower-band event only when BOTH sides were
--   STATUS='OPEN'. The open-count convention is STATUS IN ('OPEN','ACK') -- an ACK'd event still
--   counts as open -- so an incident acknowledged and THEN escalated (a WARN ACK'd, then a CRIT
--   raises) was never collapsed: the same incident double-counted as two open alerts and penalized
--   the platform score twice. Both sides of the sweep now match STATUS IN ('OPEN','ACK').
--
-- The sibling auto-clear sweep (ev alias) stays OPEN-only by design (an ACK means a human is
-- working it, so a below-threshold condition must not auto-close it). Everything else byte-identical
-- to V110. No schema change; owner applies after V114 and the next hourly SP_ALERT_SCAN evaluates
-- the corrected sweep. Never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20115, 'V115 requires V114 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 114) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 115 AS VERSION,
       'SP_ALERT_SCAN escalation-supersede includes ACK (re-derived from V110): the V067 #40 sweep now collapses the lower-band event when its higher-band sibling exists and either side is OPEN or ACK, not OPEN-only. An acknowledged-then-escalated incident (e.g. a WARN ACKed then a CRIT raised) no longer double-counts as two open alerts with a doubled score penalty, since an ACK still counts as open. The sibling auto-clear sweep stays OPEN-only by design. Everything else byte-identical. Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 115);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20115" in out and "IF (v < 114) THEN" in out
assert "SELECT 115 AS VERSION" in out and "WHERE VERSION = 115)" in out

target = Path(os.environ.get("V115_OUT") or (MIG / "V115__alert_supersede_includes_ack.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
