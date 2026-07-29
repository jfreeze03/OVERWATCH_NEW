#!/usr/bin/env python3
"""Forward-generate V057__fail_status_token.sql — silent-0 failure-count fix.

CONFIRMED correctness bug (perf-round scope 2026-07-29, item "NEW (correctness)").

SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_STATUS takes the values
success / fail / incident, and OW_QH_EXTRACT copies EXECUTION_STATUS verbatim,
so a failed query is stored as 'FAIL' — never 'FAILED'. The primary query facts
follow that convention (FACT_QUERY_HOURLY / FACT_QUERY_DAILY / OPS_DIAG all use
`= 'FAIL'`, with the standing comment "'FAIL' matches the V002 hourly-fact
convention"). But four HOURLY arms of SP_LOAD_MARTS_V27 count failures with
`EXECUTION_STATUS = 'FAILED'`, which never matches — so their FAILS column is a
constant 0:

    * MART_WAREHOUSE_EFFICIENCY_DAILY  (efficiency q-CTE, V056:265)
    * MART_QUERY_FAMILY_DAILY          (qfam,             V056:323)
    * FACT_QUERY_ROLE_HOURLY           (role_hr,          V056:374)
    * FACT_QUERY_SCHEMA_HOURLY         (schema_hr,        V056:406)

Fix: change those four `EXECUTION_STATUS = 'FAILED'` -> `= 'FAIL'` so the marts
report real failures and reconcile with the primary facts. The four
`STATE = 'FAILED'` predicates in the task-graph arm are Snowflake TASK_HISTORY
states (genuinely 'FAILED') and are LEFT UNTOUCHED — the edit is scoped to the
exact `COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,` string (x4).

No schema change, no new object: one proc re-derived from its CURRENT (V056)
definition under the derivation law; tests/test_v057_fail_token.py byte-compares.
The paired app-side instance (app/data/change_impact_sql.py warehouse_daily_series,
which read live QUERY_HISTORY with the same 'FAILED') is fixed in the same release
to `<> 'SUCCESS'`, matching its sibling proc-impact builder in that file.
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
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


def apply_n(body: str, old: str, new: str, want: int, name: str) -> str:
    n = body.count(old)
    assert n == want, f"{name}: needle x{n} (want {want}): {old[:60]!r}"
    return body.replace(old, new)


# --- the fix: the FOUR EXECUTION_STATUS='FAILED' FAILS predicates -> 'FAIL'.
# Scoped to the exact aggregate string so the task-state STATE='FAILED'
# predicates (correct) are never touched.
marts = apply_n(
    extract_proc("V056__loader_reconcile_alert_fixes.sql", "SP_LOAD_MARTS_V27"),
    "COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,",
    "COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,",
    4, "SP_LOAD_MARTS_V27")

# guard: the four task-state predicates survive unchanged.
assert marts.count("STATE = 'FAILED'") + marts.count("STATE IN ('SUCCEEDED', 'FAILED')") == 4, \
    "task-state 'FAILED' predicates must be preserved"
assert "EXECUTION_STATUS = 'FAILED'" not in marts, "no EXECUTION_STATUS='FAILED' may remain"


out = f"""-- V057__fail_status_token.sql — silent-0 failure-count correctness fix.
--
--   SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_STATUS is success/fail/
--   incident and OW_QH_EXTRACT copies it verbatim, so a failed query is 'FAIL',
--   never 'FAILED'. Four HOURLY arms of SP_LOAD_MARTS_V27 counted failures with
--   EXECUTION_STATUS = 'FAILED', which never matched -> the FAILS column was a
--   constant 0 in MART_WAREHOUSE_EFFICIENCY_DAILY, MART_QUERY_FAMILY_DAILY,
--   FACT_QUERY_ROLE_HOURLY and FACT_QUERY_SCHEMA_HOURLY. The primary facts
--   (FACT_QUERY_HOURLY/DAILY, OPS_DIAG) already use the correct 'FAIL'.
--
--   Fix: the four `COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS` -> '= FAIL'.
--   The task-graph arm's STATE = 'FAILED' predicates are TASK_HISTORY states
--   (genuinely 'FAILED') and are deliberately untouched.
--
--   One proc re-derived from V056 (derivation law); no schema change, no new
--   object. Idempotent CREATE OR REPLACE; safe to re-run. Apply AFTER V056.
--
--   Forward data self-corrects on the next hourly load; the nightly reconcile
--   repairs the trailing days. To correct older history, re-run the marts for a
--   wider window (see docs/handoff/DEPLOY_V057_20260729.md).
--
-- Derivation law: SP_LOAD_MARTS_V27 re-derived from its CURRENT (V056)
-- definition + the single enumerated edit; the test byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20057, 'V057 requires V056 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 56) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27
{marts}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 57 AS VERSION,
       'FAILS token fix: four SP_LOAD_MARTS_V27 arms (efficiency/qfam/role-hr/schema-hr) counted EXECUTION_STATUS=FAILED (never matches) -> FAIL, so MART_WAREHOUSE_EFFICIENCY_DAILY/MART_QUERY_FAMILY_DAILY/FACT_QUERY_ROLE_HOURLY/FACT_QUERY_SCHEMA_HOURLY report real failures' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 57);
"""
target = Path(os.environ.get("V057_OUT") or (MIG / "V057__fail_status_token.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
