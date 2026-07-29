#!/usr/bin/env python3
"""Forward-generate V059__task_graph_root_credits.sql — metrics-triage HIGH #2.

MART_TASK_GRAPH_DAILY.WH_CREDITS (pipeline spend / $-per-run) read ~$0 for every
proc-driven task. SP_LOAD_MARTS_V27 arm [6] rolled QUERY_ATTRIBUTION_HISTORY up by
the BARE QUERY_ID and joined a.QUERY_ID = h.QUERY_ID, but a task whose body is a
stored procedure attributes its compute to CHILD statements that carry the task's
CALL query id only as ROOT_QUERY_ID — so the bare-id join matched only the
~0-credit CALL row and collapsed proc-driven pipeline cost to ~0.

The live twin app/data/graph_sql.graph_daily_costs was fixed for exactly this in
v4.60 (audit #10) but the mart arm — served by default via run_mart_first — never
got it, so the mart under-reported while the live fallback was correct. V059
re-derives arm [6] to mirror graph_sql: roll up by COALESCE(ROOT_QUERY_ID,
QUERY_ID), prune on the same coalesced id, and join a.ROOT_ID = h.QUERY_ID.

One proc re-derived from its CURRENT (V058) definition + four enumerated token
edits (no other statement touched); V057's four EXECUTION_STATUS='FAIL' fixes and
V058's per-node arm [6b] are preserved. No schema change, no new object.
Apply AFTER V058. tests/test_v059_task_graph_root_credits.py byte-compares.
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


def apply(body: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n}: {old[:70]!r}"
        body = body.replace(old, new)
    return body


# arm [6] QAH rollup -> ROOT_QUERY_ID basis, matching graph_sql.graph_daily_costs.
marts = apply(extract_proc("V058__task_node_timing.sql", "SP_LOAD_MARTS_V27"), [
    ("SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS",
     "SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS"),
    ("AND QUERY_ID IN (",
     "AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN ("),
    ("GROUP BY QUERY_ID",
     "GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)"),
    ("a ON a.QUERY_ID = h.QUERY_ID",
     "a ON a.ROOT_ID = h.QUERY_ID"),
], "SP_LOAD_MARTS_V27")

# guardrails: the ROOT rollup is in, the bare-id join is gone, and the prior
# releases' fixes survive.
assert marts.count("COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID") == 1
assert marts.count("a ON a.ROOT_ID = h.QUERY_ID") == 1
assert "a ON a.QUERY_ID = h.QUERY_ID" not in marts
assert "SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE)" not in marts
assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4, "V057 FAIL fix must survive"
assert "EXECUTION_STATUS = 'FAILED'" not in marts
assert marts.count("loaded := loaded || 'task_node ';") == 1, "V058 arm [6b] must survive"
assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts


out = f"""-- V059__task_graph_root_credits.sql — metrics-triage HIGH #2 (correctness).
--
--   MART_TASK_GRAPH_DAILY.WH_CREDITS (pipeline spend / $-per-run) read ~$0 for
--   proc-driven tasks: SP_LOAD_MARTS_V27 arm [6] rolled QUERY_ATTRIBUTION_HISTORY
--   up by the bare QUERY_ID and joined a.QUERY_ID = h.QUERY_ID, so a task whose
--   body is a stored procedure (its compute lands on CHILD statements that carry
--   the CALL id only as ROOT_QUERY_ID) matched only the ~0-credit CALL row. The
--   live twin (app/data/graph_sql.graph_daily_costs) got this fix in v4.60 (audit
--   #10); the default-served mart never did.
--
--   V059 re-derives arm [6] to mirror graph_sql: roll up by
--   COALESCE(ROOT_QUERY_ID, QUERY_ID), prune on the same coalesced id, and join
--   a.ROOT_ID = h.QUERY_ID. Four token edits; every other statement byte-identical
--   (V057's FAIL fixes and V058's per-node arm [6b] preserved). No schema change,
--   no new object. Idempotent CREATE OR REPLACE; safe to re-run. Apply AFTER V058.
--
--   Forward loads self-correct on the next hourly cycle; to correct history re-run
--   the marts for a wider window (see docs/handoff/DEPLOY_V059_20260729.md).
--
-- Derivation law: SP_LOAD_MARTS_V27 re-derived from its CURRENT (V058) definition
-- + the four enumerated edits; the test byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20059, 'V059 requires V058 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 58) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27
{marts}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 59 AS VERSION,
       'Task-graph pipeline credits fix: SP_LOAD_MARTS_V27 arm [6] rolls QUERY_ATTRIBUTION_HISTORY up by COALESCE(ROOT_QUERY_ID,QUERY_ID) (was bare QUERY_ID), so MART_TASK_GRAPH_DAILY.WH_CREDITS captures proc-body child compute instead of reading ~0 for CALL tasks (matches graph_sql / audit #10)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 59);
"""
target = Path(os.environ.get("V059_OUT") or (MIG / "V059__task_graph_root_credits.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
