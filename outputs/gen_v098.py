#!/usr/bin/env python3
"""Forward-generate V098: SP_INCIDENT_AUTODECLARE must not re-link an already-membered alert.

[LOW] SP_INCIDENT_AUTODECLARE (V032, the only def) can attach an alert that is already a
member of one incident to a second incident (double-count). The `crit` CTE carries a guard
`AND NOT EXISTS (... INCIDENT_MEMBERS m WHERE m.MEMBER_KIND='ALERT' AND m.REF_ID = e.EVENT_ID)`
so an already-membered event doesn't seed a NEW incident, but the member INSERT independently
re-scans ALERT_EVENTS by company/severity/status/24h/family with NO such guard. Reachable:
run1 creates incident I1 for family F/company C and links e1; the operator resolves I1
(INCIDENTS.STATUS='RESOLVED' via control_room.py _incident_close_sql, which never resolves the
member alert events, so e1 stays OPEN); run2 sees a new CRITICAL e2 in the same F/C within 24h
-- the family-already-open guard does NOT block (I1 is RESOLVED), so I2 is created and the
unguarded member INSERT joins {e1, e2} and attaches BOTH to I2. INCIDENT_MEMBERS has no unique
constraint and the INSERT is plain, so e1 is now a member of both I1 and I2 -- double-counted.

Fix: add the same anti-membership guard the `crit` CTE uses to the member INSERT, so an event
already linked to ANY incident is never re-attached (alias m2 to keep the two guards distinct).

Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight after
V097; forward-healing (any pre-existing double-membership is historical). This file never runs
from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V032__incident_object.sql"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


# The append anchor is the member INSERT's final join predicate (unique in the proc: the crit
# CTE's near-twin uses `= c.FAMILY` with no trailing ';'). Add the NOT EXISTS guard as a new
# join predicate on the same event `e`.
OLD = "     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY;"
NEW = (
    "     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY\n"
    "     AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2\n"
    "                     WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);"
)

proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_INCIDENT_AUTODECLARE")
assert proc.count(OLD) == 1, f"expected 1 member-INSERT anchor, got {proc.count(OLD)}"
proc = proc.replace(OLD, NEW)
# fix landed: the member INSERT now guards against re-linking
assert "WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);" in proc
# untouched anchors: the crit CTE's own guard + the family-already-open guard survive
assert ("AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m\n"
        "                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)") in proc
assert "AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY" in proc
assert "RETURN 'auto-declared ' || :made || ' incident(s)'" in proc
# both anti-membership guards now present (crit CTE's m + member INSERT's m2)
assert proc.count("MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID") == 1
assert proc.count("m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID") == 1

out = f"""-- V098__incident_autodeclare_relink_guard.sql
--
-- SP_INCIDENT_AUTODECLARE must not re-link an already-membered alert. The proc (V032) can
-- attach an alert already a member of one incident to a second incident (double-count): the
-- `crit` CTE guards against an already-membered event seeding a NEW incident, but the member
-- INSERT independently re-scans ALERT_EVENTS with NO such guard. After an incident is resolved
-- while its CRITICAL alert stays OPEN (the closer never resolves member events), a new same-
-- family CRITICAL creates a second incident whose unguarded member INSERT re-attaches the old
-- still-OPEN alert -- INCIDENT_MEMBERS has no unique constraint, so it is counted twice.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V032 with the same anti-membership guard the crit CTE
-- uses added to the member INSERT (NOT EXISTS INCIDENT_MEMBERS m2 for this ALERT event), so an
-- event already linked to ANY incident is never re-attached.
--
-- Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight after
-- V097; forward-healing (pre-existing double-membership is historical). This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20098, 'V098 requires V097 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 97) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 98 AS VERSION,
       'SP_INCIDENT_AUTODECLARE re-link guard: re-derived from V032 so the member INSERT carries the same NOT EXISTS INCIDENT_MEMBERS anti-membership guard the crit CTE already has (alias m2), preventing an alert that is already a member of one incident (e.g. a still-OPEN CRITICAL whose incident was resolved without resolving the alert) from being re-attached to a second incident and double-counted in incident membership/metrics. Proc only, no schema change, no backfill; forward-healing (pre-existing double-membership is historical).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 98);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID" in out
assert "EXCEPTION (-20098" in out and "IF (v < 97) THEN" in out
assert "SELECT 98 AS VERSION" in out and "WHERE VERSION = 98)" in out

target = Path(os.environ.get("V098_OUT") or (MIG / "V098__incident_autodeclare_relink_guard.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
