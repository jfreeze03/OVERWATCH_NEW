#!/usr/bin/env python3
"""Forward-generate V104: SEC_CRED_EXPIRY dedupe key drops the ISO-week token.

[MED] A single expiring credential double-counts across ISO weeks, and V096's EXPIRING->EXPIRED
supersede silently no-ops on the common cross-week path.

The SEC_CRED_EXPIRY DEDUPE_KEY (SP_ALERT_SCAN [10] arm, V096:292) trails DATE_TRUNC('week',
CURRENT_DATE()):
    RULE_ID | USER | NAME | EXPIRING/EXPIRED | <ISO-week>
The credential-expiry horizon is 10 days (V028), which routinely spans two ISO weeks, so one
expiring credential raises a NEW OPEN EXPIRING event each week it sits in the horizon -- two or
three concurrent OPEN alerts for one credential that nothing collapses. Worse, when it finally
expires the EXPIRED event carries the CURRENT week's token, and V096's supersede sweep
(V096:833-845) resolves the lower state via REPLACE(key,'|EXPIRING|','|EXPIRED|') -- which
PRESERVES the week token -- so it only matches an EXPIRING event raised in that SAME week; a
prior-week EXPIRING (the normal case) is never superseded and stays OPEN alongside the EXPIRED
CRITICAL. Both inflate the open-alert / severity tallies (mart_sql.open_alert_events /
open_alert_severity_counts) and strand a phantom "still expiring" alert for an already-expired
credential. (The live Security > Expiring-credentials panel reads CREDENTIALS directly and is
unaffected; the defect is confined to the alert-queue/scorecard.)

Fix: drop the week token from the SEC_CRED_EXPIRY DEDUPE_KEY, keying per credential
identity+state (RULE_ID|USER|NAME|EXPIRING/EXPIRED). A credential in the horizon then raises ONE
deduped OPEN EXPIRING event (not one per week), and the existing REPLACE('|EXPIRING|','|EXPIRED|')
supersede matches regardless of when the EXPIRING event was raised -- no change to the sweep. The
per-week reminder re-fire is dropped, but an OPEN alert stays visible without re-firing, so nothing
is lost. The other weekly-keyed rule (V096:435, SERVICE_TYPE) is untouched.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V103; the next
hourly SP_ALERT_SCAN run raises the single-keyed event and the supersede collapses any surviving
cross-week pair. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V096__alert_scan_dedupe_keys.sql"

OLD_KEY = (
    "               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
    "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING') || '|' || "
    "DATE_TRUNC('week', CURRENT_DATE())"
)
NEW_KEY = (
    "               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
    "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')"
)


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")
assert proc.count(OLD_KEY) == 1, f"SEC_CRED_EXPIRY key: got {proc.count(OLD_KEY)}"
proc = proc.replace(OLD_KEY, NEW_KEY)

# fix landed: the cred-expiry key no longer carries a week token...
assert NEW_KEY in proc
assert OLD_KEY not in proc
# ...but the OTHER weekly-keyed rule (SERVICE_TYPE, V096:435) is untouched
assert ("c.RULE_ID || '|' || s.SERVICE_TYPE || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))") in proc
# the supersede sweep that now matches unchanged
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" in proc
# exactly one week-token reference remains in the proc (the SERVICE_TYPE rule)
assert proc.count("DATE_TRUNC('week', CURRENT_DATE())") == 1

out = f"""-- V104__sec_cred_expiry_dedupe_key.sql
--
-- SEC_CRED_EXPIRY double-counts one credential across ISO weeks and defeats V096's EXPIRING->EXPIRED
-- supersede. The dedupe key (SP_ALERT_SCAN [10], V096:292) trailed DATE_TRUNC('week', CURRENT_DATE()),
-- so a credential in the 10-day expiry horizon (V028) -- which routinely spans two ISO weeks -- raised
-- a new OPEN EXPIRING event each week, and the supersede sweep (REPLACE '|EXPIRING|'->'|EXPIRED|',
-- which preserves the week token) only resolved a same-week sibling, leaving prior-week EXPIRING events
-- OPEN alongside the EXPIRED CRITICAL. Both inflated the open-alert / severity tallies and stranded a
-- phantom "still expiring" alert for an already-expired credential (the live Expiring-credentials panel
-- reads CREDENTIALS directly and was unaffected).
--
-- Re-derives SP_ALERT_SCAN from V096 dropping the week token so the key is per credential
-- identity+state (RULE_ID|USER|NAME|EXPIRING/EXPIRED): one deduped OPEN EXPIRING event per credential,
-- and the existing supersede now matches regardless of when the EXPIRING event was raised. The other
-- weekly-keyed rule (SERVICE_TYPE) is byte-identical. No schema change; owner applies after V103 and the
-- next hourly SP_ALERT_SCAN collapses any surviving cross-week pair. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20104, 'V104 requires V103 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 103) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 104 AS VERSION,
       'SEC_CRED_EXPIRY dedupe key drops the ISO-week token: SP_ALERT_SCAN re-derived from V096 so the credential-expiry event keys per RULE_ID|USER|NAME|EXPIRING/EXPIRED instead of appending DATE_TRUNC(week). A credential in the 10-day horizon now raises ONE deduped OPEN EXPIRING event (not one per ISO week the horizon spans), and V096''s EXPIRING->EXPIRED supersede (REPLACE on the key) matches regardless of raise week -- fixing the cross-week double-count that inflated open-alert/severity tallies and stranded a phantom expiring alert after expiry. Other weekly-keyed rule (SERVICE_TYPE) byte-identical. Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 104);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20104" in out and "IF (v < 103) THEN" in out
assert "SELECT 104 AS VERSION" in out and "WHERE VERSION = 104)" in out

target = Path(os.environ.get("V104_OUT") or (MIG / "V104__sec_cred_expiry_dedupe_key.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
