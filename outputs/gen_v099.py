#!/usr/bin/env python3
"""Forward-generate V099: scope SP_INCIDENT_AUTODECLARE's family-already-open guard by company.

[HIGH] A CRITICAL for one company is silently NOT auto-declared when the OTHER company
has an open incident of the same rule family. SP_INCIDENT_AUTODECLARE's `crit` CTE selects
CRITICAL OPEN/ACK alerts for BOTH companies and the outer SELECT groups per
(FAMILY, COMPANY) -- one incident per family per company. But the family-already-open guard
(WHERE NOT EXISTS ... INCIDENT_MEMBERS m JOIN INCIDENTS i JOIN ALERT_EVENTS a) correlates
only on `SPLIT_PART(..., 1) = c.FAMILY`; it never checks `i.COMPANY = c.COMPANY`. Because
both ALFA and Trexis share rule families (DEDUPE_KEY = RULE_ID||'|ALL|'||DAY, so FAMILY
collides across companies), any crit row whose family is already represented in ANY
open/mitigated incident is dropped -- so a Trexis CRITICAL in a family ALFA already has open
is never auto-declared until ALFA's incident resolves: a silent cross-company incident
coverage gap. The sibling manual-declare path already scopes by company (control_room.py,
live round 8); the auto path lacked the symmetric scope.

Fix: add `AND i.COMPANY = c.COMPANY` to the family-already-open NOT EXISTS guard, so
suppression is per company, matching the per-(FAMILY, COMPANY) grouping.

Re-derives SP_INCIDENT_AUTODECLARE from its LATEST def (V098) with that one guard edit;
no schema change, no new object, no backfill. Owner applies in Snowsight after V098;
forward-healing (the next hourly TASK_INCIDENT_AUTODECLARE run auto-declares the
cross-company CRITICALs that were being suppressed). This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V098__incident_autodeclare_relink_guard.sql"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


# Add the company correlation to the family-already-open guard (2-line block is unique:
# `i.STATUS IN ('OPEN', 'MITIGATED')` and the `a.`-aliased `= c.FAMILY` predicate only
# appear together in this guard).
OLD = (
    "          AND i.STATUS IN ('OPEN', 'MITIGATED')\n"
    "          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY"
)
NEW = (
    "          AND i.STATUS IN ('OPEN', 'MITIGATED')\n"
    "          AND i.COMPANY = c.COMPANY\n"
    "          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY"
)

proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_INCIDENT_AUTODECLARE")
assert proc.count(OLD) == 1, f"expected 1 family-open guard, got {proc.count(OLD)}"
assert "i.COMPANY = c.COMPANY" not in proc, "company scope already present"
proc = proc.replace(OLD, NEW)
# fix landed
assert "AND i.COMPANY = c.COMPANY\n" in proc
# untouched anchors: the crit CTE anti-membership guard, the member-INSERT guard (V098),
# and the member-INSERT family match all survive
assert "m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID" in proc           # crit CTE guard
assert "m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID" in proc         # V098 member guard
assert "SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY" in proc
assert "RETURN 'auto-declared ' || :made || ' incident(s)'" in proc

out = f"""-- V099__incident_autodeclare_company_scope.sql
--
-- Scope SP_INCIDENT_AUTODECLARE's family-already-open guard by company. The proc groups
-- per (FAMILY, COMPANY) -- one incident per family per company -- but the family-already-
-- open NOT EXISTS guard correlated only on the family, never on i.COMPANY = c.COMPANY.
-- Because ALFA and Trexis share rule families (FAMILY = RULE_ID collides across companies),
-- a CRITICAL for one company was silently NOT auto-declared whenever the OTHER company had
-- an open/mitigated incident of the same family -- a cross-company incident coverage gap
-- until the other company's incident resolved. The manual-declare path already scopes by
-- company (live round 8); this brings the auto path to the symmetric scope.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V098 with `AND i.COMPANY = c.COMPANY` added to the
-- family-already-open guard. No schema change, no new object, no backfill. Owner applies in
-- Snowsight after V098; forward-healing (the next hourly TASK_INCIDENT_AUTODECLARE run
-- auto-declares the cross-company CRITICALs that were being suppressed). This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20099, 'V099 requires V098 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 98) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 99 AS VERSION,
       'SP_INCIDENT_AUTODECLARE family-open guard scoped by company: re-derived from V098 with AND i.COMPANY = c.COMPANY added to the family-already-open NOT EXISTS guard, so a CRITICAL for one company is auto-declared even when the other company has an open incident of the same (company-shared) rule family. Fixes a cross-company incident coverage gap; the manual-declare path already scopes by company. Proc only, no schema change, no backfill; forward-healing on the next hourly run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 99);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "AND i.COMPANY = c.COMPANY" in out
assert "EXCEPTION (-20099" in out and "IF (v < 98) THEN" in out
assert "SELECT 99 AS VERSION" in out and "WHERE VERSION = 99)" in out

target = Path(os.environ.get("V099_OUT")
              or (MIG / "V099__incident_autodeclare_company_scope.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
