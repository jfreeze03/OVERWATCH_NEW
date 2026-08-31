#!/usr/bin/env python3
"""Forward-generate V110: four SP_ALERT_SCAN fixes from the alerting-layer hunt.

Re-derives SP_ALERT_SCAN (latest def = V107) with four corrections:

[MED] A -- SEC_NEW_ADMIN_NETWORK [18] watched-admin population omits the built-in ACCOUNTADMIN role
   (WHERE ROLE IN ('SNOW_ACCOUNTADMINS','SNOW_SYSADMINS')), so a user granted ACCOUNTADMIN directly
   (a pattern the BREAKGLASS_GRANTS_30D posture metric tracks) is absent from the join and their
   first login from a new IP fires no new-admin-network alert -- a false all-clear on the account's
   top-privilege credential. Every sibling path uses the canonical {ACCOUNTADMIN, SNOW_ACCOUNTADMINS}.
   Fix: add 'ACCOUNTADMIN' to the role set.

[MED] B -- the V067 #40 escalation-supersede sweep resolves a stale EXPIRING cred-expiry event via
   REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|'), but V104 made the band token TERMINAL (the key
   now ends '...|NAME|EXPIRING' with no trailing pipe), so '|EXPIRING|' never matches and the
   supersede is a permanent no-op: an expired credential shows BOTH a stale EXPIRING and a new EXPIRED
   event. Fix: match the terminal token with REPLACE(..., '|EXPIRING', '|EXPIRED').

[LOW] C -- V108 gave COST_CONTRACT_BREACH a third band token EXH (exhausted), but the supersede sweep
   has no |CRIT|->|EXH| or |WARN|->|EXH| arm, so a contract crossing CRIT->EXHAUSTED within a week
   leaves the stale CRIT 'projected to exhaust' event OPEN beside the EXH 'exhausted' event, double-
   counting one incident. Fix: add the two EXH supersede arms.

[LOW] F -- PERF_QUERY_FAIL_PCT [03] DETAIL says '... failed in last ' || c.WINDOW_HOURS || 'h.' but
   the FAILED/TOTAL counts are aggregated over a HARDCODED 24h window; WINDOW_HOURS is informational
   (never read for the window). If an operator edits WINDOW_HOURS the DETAIL misstates the window.
   Fix: hardcode the DETAIL to 'in last 24h.' to match the aggregation (like the sibling perf arms).

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V109; the next hourly
SP_ALERT_SCAN evaluates the corrected arms. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V107__cost_dept_budget_pace_dept_join_and_pace_window.sql"

OLD_A = "AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')"
NEW_A = "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')"

OLD_BC = (
    "                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|'))"
)
NEW_BC = (
    "                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|EXH|')\n"
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))"
)

OLD_F = "' queries failed in last ' || c.WINDOW_HOURS || 'h.'"
NEW_F = "' queries failed in last 24h.'"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")

assert proc.count(OLD_A) == 1, f"admin-role set: got {proc.count(OLD_A)}"
assert proc.count(OLD_BC) == 1, f"supersede sweep block: got {proc.count(OLD_BC)}"
assert proc.count(OLD_F) == 1, f"PERF DETAIL: got {proc.count(OLD_F)}"

proc = proc.replace(OLD_A, NEW_A)
proc = proc.replace(OLD_BC, NEW_BC)
proc = proc.replace(OLD_F, NEW_F)

# post-conditions
assert "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in proc
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))" in proc
assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" not in proc
assert proc.count("REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')") == 1
assert proc.count("REPLACE(lo.DEDUPE_KEY, '|WARN|', '|EXH|')") == 1
assert "' queries failed in last 24h.'" in proc
assert "c.WINDOW_HOURS || 'h.'" not in proc
# the arms we did NOT touch survive
assert "SEC_NEW_ADMIN_NETWORK" in proc and "COST_CONTRACT_BREACH" not in proc  # contract is a DAILY arm, not here
assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in proc  # V106/V107 dept-pace fixes intact

out = f"""-- V110__alerting_hunt_sp_alert_scan_fixes.sql
--
-- Four SP_ALERT_SCAN fixes from the alerting-layer hunt (proc re-derived from V107):
--   A [MED] SEC_NEW_ADMIN_NETWORK [18] adds the built-in ACCOUNTADMIN to the watched-admin role set
--     (was ('SNOW_ACCOUNTADMINS','SNOW_SYSADMINS') only), so a directly-granted-ACCOUNTADMIN user's
--     first login from a new IP is no longer a false all-clear.
--   B [MED] the escalation-supersede sweep matches the TERMINAL EXPIRING band token
--     (REPLACE(...,'|EXPIRING','|EXPIRED')); V104 made the token terminal so the old '|EXPIRING|'
--     pattern never matched and an expired credential kept a stale EXPIRING event open forever.
--   C [LOW] the supersede sweep gains |CRIT|->|EXH| and |WARN|->|EXH| arms so a COST_CONTRACT_BREACH
--     event escalating into V108's EXHAUSTED band supersedes the stale prior event.
--   F [LOW] PERF_QUERY_FAIL_PCT [03] DETAIL hardcodes 'in last 24h.' to match its hardcoded 24h
--     aggregation window (WINDOW_HOURS is informational and never read for the window).
--
-- Everything else byte-identical (incl. V106/V107 dept-pace fixes). No schema change; owner applies
-- after V109 and the next hourly SP_ALERT_SCAN evaluates the corrected arms. Never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20110, 'V110 requires V109 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 109) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 110 AS VERSION,
       'Alerting-hunt SP_ALERT_SCAN fixes (re-derived from V107): (A) SEC_NEW_ADMIN_NETWORK watched-admin role set adds the built-in ACCOUNTADMIN so a directly-granted-ACCOUNTADMIN user login from a new network is no longer a false all-clear; (B) escalation-supersede sweep matches the terminal EXPIRING band token (REPLACE ..|EXPIRING -> ..|EXPIRED) so an expired credential no longer strands a stale EXPIRING event; (C) supersede sweep gains |CRIT|->|EXH| and |WARN|->|EXH| arms so a COST_CONTRACT_BREACH event escalating to the V108 EXHAUSTED band supersedes the prior event; (F) PERF_QUERY_FAIL_PCT DETAIL hardcodes in-last-24h to match its 24h aggregation. Everything else byte-identical. Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 110);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20110" in out and "IF (v < 109) THEN" in out
assert "SELECT 110 AS VERSION" in out and "WHERE VERSION = 110)" in out

target = Path(os.environ.get("V110_OUT") or (MIG / "V110__alerting_hunt_sp_alert_scan_fixes.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
