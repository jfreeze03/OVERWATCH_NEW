#!/usr/bin/env python3
"""Forward-generate V092: give SP_ACTION_LIFECYCLE explicit CLEAR signals.

V074's SP_ACTION_LIFECYCLE uses COALESCE-keep semantics for OWNER and DEFER_UNTIL:
    OWNER       = COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER)
    DEFER_UNTIL = COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL)
so a BLANK owner or a NULL defer is a keep, never a clear. Consequence: the Action
Center "Update work item" UI cannot un-assign an owner (blank the box) or un-defer
an item (toggle "Defer this item" off) — both are silent no-ops. v4.318 made the UI
HONEST about that (it stopped offering "unassign" / "clear the defer" as savable
effects), but the operator capability was genuinely missing: an operator who wants
to resume a deferred item now, or reassign to nobody, had no in-app path.

This re-derives SP_ACTION_LIFECYCLE from the LATEST definition (V074 — no later
migration re-derives it; V081 only MODELS a new proc on it) byte-identically PLUS
two enumerated edits:
  1. two new BOOLEAN parameters, P_CLEAR_OWNER and P_CLEAR_DEFER, appended after
     P_REQUEST_KEY;
  2. the OWNER and DEFER_UNTIL assignments wrapped so an explicit clear wins:
       OWNER       = IFF(:P_CLEAR_OWNER, NULL, COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER))
       DEFER_UNTIL = IFF(:P_CLEAR_DEFER, NULL, COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL))
When both flags are FALSE (the default the app passes for an ordinary edit) the
UPDATE is byte-for-byte the old COALESCE-keep behaviour, so existing assign/defer/
comment/resolve paths are unchanged. The audit INSERT already records the passed
OWNER/DEFER_UNTIL (NULL on a clear), so a clear is logged correctly without change.

The signature changes (8 args -> 10), so the migration first DROPs the old 8-arg
overload — Snowflake keys a procedure by name+signature, so a bare CREATE OR REPLACE
would leave the stale 8-arg version alongside the new one. Nothing but the app calls
this proc (grep: only V074 defines it and V081 references it in a comment), and the
app rewire (action_transition_sql / _render_action_detail pass the clear flags and
restore the "unassign" / "clear the defer" effect lines) ships with this migration.

Procedure-only: no table changes, no data reload. Owner applies in Snowsight after
V091. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V074__operating_workbench_foundation.sql"

# The 8-arg overload the new definition replaces — dropped explicitly because a
# differing signature is a distinct object under CREATE OR REPLACE.
OLD_SIGNATURE = ("DBA_MAINT_DB.OVERWATCH.SP_ACTION_LIFECYCLE("
                 "VARCHAR, VARCHAR, VARCHAR, DATE, DATE, VARCHAR, VARCHAR, VARCHAR)")

# --- enumerated edit 1: two clear-signal parameters ------------------------------
OLD_PARAMS = "    P_REQUEST_KEY VARCHAR\n)"
NEW_PARAMS = ("    P_REQUEST_KEY VARCHAR,\n"
              "    P_CLEAR_OWNER BOOLEAN,\n"
              "    P_CLEAR_DEFER BOOLEAN\n)")

# --- enumerated edit 2: an explicit clear wins over COALESCE-keep -----------------
OLD_OWNER = "           OWNER = COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER),"
NEW_OWNER = "           OWNER = IFF(:P_CLEAR_OWNER, NULL, COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER)),"
OLD_DEFER = "           DEFER_UNTIL = COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL),"
NEW_DEFER = "           DEFER_UNTIL = IFF(:P_CLEAR_DEFER, NULL, COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL)),"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_ACTION_LIFECYCLE")

for old in (OLD_PARAMS, OLD_OWNER, OLD_DEFER):
    assert proc.count(old) == 1, f"expected exactly 1 occurrence of: {old!r}"
proc = proc.replace(OLD_PARAMS, NEW_PARAMS)
proc = proc.replace(OLD_OWNER, NEW_OWNER)
proc = proc.replace(OLD_DEFER, NEW_DEFER)
assert NEW_PARAMS in proc and NEW_OWNER in proc and NEW_DEFER in proc
# The old COALESCE-keep expressions survive INSIDE the IFF false-branch, nowhere else.
assert proc.count("COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER)") == 1
assert proc.count("COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL)") == 1
# Untouched anchors: everything else is byte-identical to V074.
for anchor in (
    "seen NUMBER DEFAULT 0;",
    "next_status := COALESCE(NULLIF(UPPER(TRIM(P_STATUS)), ''), old_status);",
    "BEGIN TRANSACTION;",
    "RESOLUTION_NOTE = IFF(:next_status IN ('DONE', 'DROPPED')",
    "COMPLETED_AT = IFF(:next_status IN ('DONE', 'DROPPED'),",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_ACTIVITY",
    "IFF(:next_status = :old_status, 'COMMENT', 'TRANSITION'),",
    "RETURN 'DUPLICATE: request already applied';",
    "WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;",
):
    assert anchor in proc, anchor

out = f"""-- V092__action_lifecycle_clear_signals.sql
--
-- Give SP_ACTION_LIFECYCLE explicit CLEAR signals so an operator can un-assign an
-- owner and un-defer (resume) a work item from the Action Center. V074's proc uses
-- COALESCE-keep for OWNER and DEFER_UNTIL, so a blank owner / NULL defer is a keep,
-- never a clear -- v4.318 had to make the UI honest by dropping the "unassign" /
-- "clear the defer" effects the write could not perform, leaving a genuine gap.
--
-- Re-derives SP_ACTION_LIFECYCLE from the LATEST definition (V074) byte-identically
-- plus two enumerated edits: two new BOOLEAN parameters (P_CLEAR_OWNER,
-- P_CLEAR_DEFER) appended after P_REQUEST_KEY, and the OWNER / DEFER_UNTIL
-- assignments wrapped IFF(:P_CLEAR_x, NULL, <old COALESCE-keep>). With both flags
-- FALSE (the app default for an ordinary edit) the UPDATE is the old behaviour
-- byte-for-byte. The audit INSERT already logs the passed OWNER/DEFER_UNTIL (NULL on
-- a clear), so a clear is recorded without change.
--
-- The signature changes (8 -> 10 args), so the old 8-arg overload is DROPped first
-- (a differing signature is a distinct object under CREATE OR REPLACE). Only the app
-- calls this proc; the rewire (clear flags + restored effect lines) ships with it.
--
-- Procedure-only: no table changes, no data reload. Owner applies in Snowsight after
-- V091. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20092, 'V092 requires V091 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 91) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Drop the superseded 8-arg overload before creating the 10-arg definition.
DROP PROCEDURE IF EXISTS {OLD_SIGNATURE};

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 92 AS VERSION,
       'Action lifecycle clear signals: SP_ACTION_LIFECYCLE re-derived from V074 with P_CLEAR_OWNER / P_CLEAR_DEFER BOOLEAN parameters so an operator can un-assign an owner and un-defer (resume) a work item -- the COALESCE-keep semantics could only keep, never clear (the v4.318 UI honesty gap). Both flags FALSE = the old behaviour byte-for-byte; the audit already logs the cleared value. Old 8-arg overload dropped. Procedure-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 92);
"""

# --- self-assertions: one guarded proc re-derivation + one version row -----------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE VIEW" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert out.count("DROP PROCEDURE IF EXISTS") == 1
assert "P_CLEAR_OWNER BOOLEAN" in out and "P_CLEAR_DEFER BOOLEAN" in out
assert "OWNER = IFF(:P_CLEAR_OWNER, NULL, COALESCE(NULLIF(TRIM(:P_OWNER), ''), OWNER))" in out
assert "DEFER_UNTIL = IFF(:P_CLEAR_DEFER, NULL, COALESCE(:P_DEFER_UNTIL, DEFER_UNTIL))" in out
# one atomic transaction with rollback preserved from V074
assert out.count("BEGIN TRANSACTION;") == 1 and out.count("COMMIT;") == 1
assert "EXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;" in out
assert "DUPLICATE: request already applied" in out
assert "EXCEPTION (-20092" in out and "IF (v < 91) THEN" in out
assert "SELECT 92 AS VERSION" in out and "WHERE VERSION = 92)" in out

target = Path(os.environ.get("V092_OUT")
              or (MIG / "V092__action_lifecycle_clear_signals.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
