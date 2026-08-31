#!/usr/bin/env python3
"""Forward-generate V109: SP_WAREHOUSE_CHANGE_SCAN failure axis uses the real EXECUTION_STATUS domain.

[MED] SP_WAREHOUSE_CHANGE_SCAN (defined only in V024, never re-derived) computes both BASELINE_FAIL_PCT
and AFTER_FAIL_PCT from COUNT_IF(q.EXECUTION_STATUS = 'FAILED'). In SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
the value is 'SUCCESS' / 'FAIL' / 'INCIDENT' -- never 'FAILED' -- so both counters are a constant 0.
The post-change regression verdict tests AFTER_FAIL_PCT >= BASELINE_FAIL_PCT + 5, which is permanently
0 >= 5 = false, so the failure-rate regression axis can NEVER fire: a warehouse setting change that
breaks a large share of the warehouse's queries is bucketed NEUTRAL (no WH_CHANGE_REGRESSION alert) and
VERDICT_DETAIL always prints 'fail 0->0%'. This is the same dead token V057 fixed in SP_LOAD_MARTS_V27,
left un-fixed in this separate proc.

Fix: re-derive SP_WAREHOUSE_CHANGE_SCAN from V024 with COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') on
both the baseline and after arms, matching V010's object-change scan and the codebase-wide convention.
Procedure re-derivation only, no schema change and NO task re-creation. Owner applies in Snowsight after
V108; the next daily TASK_WAREHOUSE_CHANGE_SCAN re-derives FAIL_PCT on open tracking rows. Never runs
from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V024__warehouse_change_scorecard.sql"

OLD_FAILS = "COUNT_IF(q.EXECUTION_STATUS = 'FAILED') AS FAILS"
NEW_FAILS = "COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_WAREHOUSE_CHANGE_SCAN\(")

assert proc.count(OLD_FAILS) == 2, f"FAILS token: got {proc.count(OLD_FAILS)}"
proc = proc.replace(OLD_FAILS, NEW_FAILS)

# post-conditions: exactly the two failure-axis swaps, nothing else disturbed
assert proc.count(NEW_FAILS) == 2 and OLD_FAILS not in proc
assert "SP_WAREHOUSE_CHANGE_SCAN" in proc
assert "WAREHOUSE_CHANGE_REGISTRY" in proc          # the registry write survives
# the proc contains exactly its own CREATE and no re-created table/task
assert proc.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TASK" not in proc and "CREATE TABLE " not in proc

out = f"""-- V109__warehouse_change_scan_fail_token.sql
--
-- SP_WAREHOUSE_CHANGE_SCAN failure axis. The proc (defined only in V024, never re-derived) counted
-- query failures with COUNT_IF(q.EXECUTION_STATUS = 'FAILED'), but QUERY_HISTORY.EXECUTION_STATUS is
-- 'SUCCESS' / 'FAIL' / 'INCIDENT' -- never 'FAILED' -- so BASELINE_FAIL_PCT and AFTER_FAIL_PCT were a
-- constant 0, the post-change regression fail axis (AFTER >= BASELINE + 5) was permanently 0 >= 5 =
-- false, and a setting change that broke a warehouse's queries read a false all-clear ('fail 0->0%').
--
-- Re-derives SP_WAREHOUSE_CHANGE_SCAN from V024 with COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') on both
-- the baseline and after arms (matching V010 and the codebase convention; the same dead token V057
-- fixed in SP_LOAD_MARTS_V27). Everything else byte-identical. No schema change, no task re-creation;
-- owner applies in Snowsight after V108 and the next daily TASK_WAREHOUSE_CHANGE_SCAN re-derives
-- FAIL_PCT on open tracking rows. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20109, 'V109 requires V108 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 108) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 109 AS VERSION,
       'SP_WAREHOUSE_CHANGE_SCAN failure axis: proc re-derived from V024 so the baseline and after query-failure counters use COUNT_IF(EXECUTION_STATUS <> ''SUCCESS'') instead of the dead = ''FAILED'' token (QUERY_HISTORY domain is SUCCESS/FAIL/INCIDENT). BASELINE_FAIL_PCT and AFTER_FAIL_PCT are no longer a constant 0, so the post-change regression fail axis can fire and a warehouse setting change that breaks queries no longer reads a false all-clear. Everything else byte-identical; no schema change, no task re-creation. Proc only; forward-healing on the next daily TASK_WAREHOUSE_CHANGE_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 109);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert out.count("COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS") == 2
assert OLD_FAILS not in proc   # the dead token is gone from the PROC (the header comment may cite it)
assert "EXCEPTION (-20109" in out and "IF (v < 108) THEN" in out
assert "SELECT 109 AS VERSION" in out and "WHERE VERSION = 109)" in out

target = Path(os.environ.get("V109_OUT") or (MIG / "V109__warehouse_change_scan_fail_token.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
