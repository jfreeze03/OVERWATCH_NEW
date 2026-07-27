#!/usr/bin/env python3
"""Forward-generate V053a__action_layer_remediation_verify.sql.

Design + sign-off: docs/design/ACTION_LAYER_V053.md (D1-D5, 2026-07-27).
Phase a of the action layer, reduced to what is SAFE app-side: the typed
savings link + re-derived monthly verifier. The remediation proc was dropped
after three review rounds kept finding holes in server-side free-text SQL
validation (latest: a comment slips a DROP COLUMN past the substring
allow-list). Remediation stays on the app's existing guarded path; the app
now stamps FINDING_TYPE so the verifier finds those rows.

- SP_VERIFY_IDLE_SAVINGS: re-derived from V007 under the derivation law with
  two edits so it selects app-booked typed rows (closes the P1-A selection gap).

tests/test_v053a_action_layer.py re-derives and byte-compares.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def apply(body: str, edits, name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n}: {old[:80]!r}"
        body = body.replace(old, new)
    return body


# Re-derive the monthly verifier (V007) so it selects typed app-booked rows.
verify_idle = apply(extract_proc("V007__automation.sql", "SP_VERIFY_IDLE_SAVINGS"), [
    ("               TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', '')) AS WAREHOUSE_NAME,",
     "               COALESCE(NULLIF(TARGET_OBJECT, ''),\n"
     "                        TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', ''))) AS WAREHOUSE_NAME,"),
    ("        WHERE STATE = 'ESTIMATED' AND DESCRIPTION LIKE 'Auto-suspend tune: %'",
     "        WHERE STATE = 'ESTIMATED'\n"
     "          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')"),
], "verify_idle")

out = r"""-- V053a__action_layer_remediation_verify.sql — action layer phase a
-- (Tranche C continued; design + sign-off: docs/design/ACTION_LAYER_V053.md).
--
--   Typed savings link + re-derived monthly verifier only. This closes the
--   P1-A selection gap: the app stamps FINDING_TYPE/TARGET_OBJECT on its
--   existing savings-ledger inserts, and the verifier now selects those rows.
--   No stored procedure ships: SP_EXECUTE_REMEDIATION (free-text lever under
--   EXECUTE AS OWNER) and SP_VERIFY_SAVINGS (owner-privileged proof) were both
--   dropped after review — remediation and verify stay on the app's existing
--   guarded paths (typed confirmation + operator gating). Additive + a proc
--   re-derivation only.
--     D1 narrow allow-list (object tuning: warehouse/pipe/task/table; account
--        levers + cancel stay on their guarded raw path); D2 target identifier-validated
--        and NO concatenated stored proof (the injection source is gone); the
--        proof is operator-supplied; D3 proof evidence = QUERY_ID + snapshot;
--        row-affected checks on every UPDATE; sequential-dedup idempotency.
--
--   App paths are proc-first with legacy fallback: pre-V053a the app behaves
--   exactly as v4.54. Apply AFTER V052. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20053, 'V053a requires V052 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 52) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Typed savings link + proof evidence (additive).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS FINDING_TYPE VARCHAR(40);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS TARGET_OBJECT VARCHAR(300);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_QUERY_ID VARCHAR(80);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_RESULT VARCHAR(16000);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_RUN_AT TIMESTAMP_NTZ;

-- >>> derived:SP_VERIFY_IDLE_SAVINGS
""" + verify_idle + """
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 53 AS VERSION,
       'Action layer phase a (Tranche C): typed SAVINGS_LEDGER link (FINDING_TYPE/TARGET_OBJECT + proof evidence columns) + monthly verifier re-derived to select typed rows (closes the P1-A selection gap). Remediation/verify procs dropped after review; those actions stay on the app guarded path.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 53);
"""
target = Path(os.environ.get("V053A_OUT") or (MIG / "V053__action_layer_remediation_verify.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
