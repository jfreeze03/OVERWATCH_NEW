#!/usr/bin/env python3
"""Forward-generate V112: the morning digest never reaches a CRITICAL-only (paging) route.

[LOW] ALERT_ROUTES.DELIVER_DIGEST defaults TRUE (V070), and SP_DAILY_DIGEST walks every
`r.ENABLED AND r.DELIVER_DIGEST` route with no severity filter, sending the same executive prose.
A paging/tactical route added later (e.g. the documented CRITICAL -> PagerDuty recipe, which omits
DELIVER_DIGEST) inherits DELIVER_DIGEST=TRUE and receives the daily leadership digest -- paging
on-call for a non-incident. Snowflake cannot ALTER a column default to a literal, so the robust fix is
in the proc: the digest cursor now also excludes CRITICAL-only routes (MIN_SEVERITY = 'CRITICAL'),
which are paging targets by convention, regardless of the DELIVER_DIGEST default.

Re-derives SP_DAILY_DIGEST (latest def = V070) with the added cursor predicate. No schema change.
Owner applies in Snowsight after V111; the next SP_DAILY_DIGEST run skips CRITICAL-only routes. This
file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V070__delivery_routing_teams_only.sql"

OLD_CUR = ("        WHERE r.ENABLED AND r.DELIVER_DIGEST   -- V070 #11: only digest-eligible routes\n"
           "        ORDER BY r.ROUTE_ID;")
NEW_CUR = ("        WHERE r.ENABLED AND r.DELIVER_DIGEST   -- V070 #11: only digest-eligible routes\n"
           "          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'   -- alerting-hunt: never send the exec digest to a CRITICAL-only (paging) route (DELIVER_DIGEST defaults TRUE, and Snowflake cannot ALTER that default)\n"
           "        ORDER BY r.ROUTE_ID;")


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_DAILY_DIGEST\(")
assert proc.count(OLD_CUR) == 1, f"digest cursor: got {proc.count(OLD_CUR)}"
proc = proc.replace(OLD_CUR, NEW_CUR)
assert "UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'" in proc
assert "r.ENABLED AND r.DELIVER_DIGEST" in proc   # existing filter preserved

out = f"""-- V112__daily_digest_skips_paging_routes.sql
--
-- The morning digest never reaches a CRITICAL-only (paging) route. DELIVER_DIGEST defaults TRUE
-- (V070) and SP_DAILY_DIGEST walked every ENABLED digest-eligible route with no severity filter, so a
-- paging route added via the documented CRITICAL -> PagerDuty recipe (which omits DELIVER_DIGEST)
-- inherited TRUE and got the executive digest -- paging on-call for a non-incident. Snowflake cannot
-- ALTER a column default to a literal, so the digest cursor now also excludes CRITICAL-only routes
-- (MIN_SEVERITY = 'CRITICAL'), the paging targets by convention.
--
-- Re-derives SP_DAILY_DIGEST from V070; everything else byte-identical. No schema change; owner
-- applies after V111 and the next SP_DAILY_DIGEST run skips CRITICAL-only routes. Never runs from app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20112, 'V112 requires V111 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 111) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 112 AS VERSION,
       'Daily digest skips paging routes: SP_DAILY_DIGEST re-derived from V070 so its route cursor also excludes CRITICAL-only routes (UPPER(MIN_SEVERITY) <> CRITICAL). DELIVER_DIGEST defaults TRUE and cannot be ALTERed to a literal in Snowflake, so a paging route added via the CRITICAL -> PagerDuty recipe no longer receives the executive morning digest. Everything else byte-identical. Proc only, no schema change; forward-healing on the next SP_DAILY_DIGEST run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 112);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20112" in out and "IF (v < 111) THEN" in out
assert "SELECT 112 AS VERSION" in out and "WHERE VERSION = 112)" in out

target = Path(os.environ.get("V112_OUT") or (MIG / "V112__daily_digest_skips_paging_routes.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
