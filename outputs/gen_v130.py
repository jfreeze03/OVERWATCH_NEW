#!/usr/bin/env python3
"""Forward-generate V130: proc-level proof enforcement in SP_VERIFY_EXPERIMENT (Codex R32).

The Decision Studio UI gates the Save-as-VERIFIED button on a result note, positive verified USD, and a
closed observation window (decision_studio.py:1007-1016), but the settlement procedure itself (V081)
validated only the status enum, existence and request-key idempotency — so a VERIFIED call with an empty
note and P_VERIFIED_USD=0 (e.g. the owner hand-calling in Snowsight) would book a $0, no-evidence
SAVINGS_LEDGER row. This adds defense-in-depth: SP_VERIFY_EXPERIMENT now REJECTS a VERIFIED settlement
that lacks a result note or positive savings, or whose observation window has not closed, regardless of
caller — returning a 'BLOCKED: …' string BEFORE the transaction opens.

Re-derives SP_VERIFY_EXPERIMENT from V081 with only the added OBSERVATION_END read + the two proof guards;
everything else byte-identical. Owner applies in Snowsight after V129. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V081__unified_experiment_verify.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_VERIFY_EXPERIMENT\(")

# T1: declare the observation-window holder alongside the existing locals.
DECL_OLD = "    new_status VARCHAR;\nBEGIN"
DECL_NEW = "    new_status VARCHAR;\n    obs_end DATE;\nBEGIN"
assert proc.count(DECL_OLD) == 1, f"declare anchor: got {proc.count(DECL_OLD)}"
proc = proc.replace(DECL_OLD, DECL_NEW)

# T2: also read the observation window in the existence probe.
SEL_OLD = ("SELECT COUNT(*), MAX(ACTION_ID), MAX(TITLE)\n"
           "      INTO :matched, :aid, :exp_title")
SEL_NEW = ("SELECT COUNT(*), MAX(ACTION_ID), MAX(TITLE), MAX(TO_DATE(OBSERVATION_END))\n"
           "      INTO :matched, :aid, :exp_title, :obs_end")
assert proc.count(SEL_OLD) == 1, f"select anchor: got {proc.count(SEL_OLD)}"
proc = proc.replace(SEL_OLD, SEL_NEW)

# T3: the proof guards, inserted immediately before the transaction opens (after the
# NOT_FOUND + DUPLICATE checks). A VERIFIED verdict must carry evidence; other verdicts pass.
GUARD = (
    "    -- R32: proc-level proof enforcement (defense-in-depth). The UI already gates these, but\n"
    "    -- a VERIFIED settlement hand-called via Snowsight must not book a $0 / no-evidence saving.\n"
    "    IF (:new_status = 'VERIFIED'\n"
    "        AND (P_RESULT_NOTE IS NULL OR TRIM(P_RESULT_NOTE) = ''\n"
    "             OR P_VERIFIED_USD IS NULL OR P_VERIFIED_USD <= 0)) THEN\n"
    "        RETURN 'BLOCKED: VERIFIED needs a result note and positive verified savings';\n"
    "    END IF;\n"
    "    IF (:new_status = 'VERIFIED' AND :obs_end IS NOT NULL AND :obs_end > CURRENT_DATE()) THEN\n"
    "        RETURN 'BLOCKED: the observation window has not closed yet';\n"
    "    END IF;\n\n"
)
TXN = "    BEGIN TRANSACTION;"
assert proc.count(TXN) == 1, f"transaction anchor: got {proc.count(TXN)}"
proc = proc.replace(TXN, GUARD + TXN, 1)

# post-conditions
assert "obs_end DATE;" in proc
assert "MAX(TO_DATE(OBSERVATION_END))" in proc and ":obs_end" in proc
assert "BLOCKED: VERIFIED needs a result note and positive verified savings" in proc
assert "BLOCKED: the observation window has not closed yet" in proc
assert proc.count("BEGIN TRANSACTION;") == 1  # the guard is BEFORE the txn, not inside it
assert "SP_VERIFY_EXPERIMENT" in proc and "SAVINGS_LEDGER" in proc  # nothing dropped

out = f"""-- V130__experiment_verify_proof_guard.sql
--
-- Proc-level proof enforcement in SP_VERIFY_EXPERIMENT (Codex R32). The Decision Studio UI gates a
-- VERIFIED settlement on a result note, positive verified USD, and a closed observation window, but the
-- settlement PROCEDURE validated only the status enum / existence / request-key idempotency (V081) -- so a
-- VERIFIED call with an empty note and $0 (the owner hand-calling in Snowsight) would book a $0,
-- no-evidence SAVINGS_LEDGER row. SP_VERIFY_EXPERIMENT now REJECTS, regardless of caller, a VERIFIED
-- verdict that lacks a result note or positive savings, or whose observation window has not closed --
-- returning 'BLOCKED: …' BEFORE the transaction opens. REJECTED / ROLLED_BACK verdicts are unaffected.
--
-- Re-derives SP_VERIFY_EXPERIMENT from V081 with only the OBSERVATION_END read + the two proof guards
-- added; everything else byte-identical. No schema change. Owner applies in Snowsight after V129; the
-- next settlement is proof-gated at the database. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20130, 'V130 requires V129 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 129) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 130 AS VERSION,
       'Proc-level proof enforcement in SP_VERIFY_EXPERIMENT (R32): re-derived from V081 to REJECT a VERIFIED settlement that lacks a result note or positive verified savings, or whose observation window has not closed, regardless of caller (the UI already gated these; this closes the hand-called-Snowsight bypass that could book a $0 no-evidence SAVINGS_LEDGER row). Returns BLOCKED before the transaction; REJECTED/ROLLED_BACK unaffected. Everything else byte-identical, no schema change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 130);
"""

# self-assertions
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_EXPERIMENT" in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "\\" not in out, "generated SQL must carry no backslash escapes"
assert "EXCEPTION (-20130" in out and "IF (v < 129) THEN" in out
assert "SELECT 130 AS VERSION" in out and "WHERE VERSION = 130)" in out

target = Path(os.environ.get("V130_OUT") or (MIG / "V130__experiment_verify_proof_guard.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
