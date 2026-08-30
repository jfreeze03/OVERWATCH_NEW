#!/usr/bin/env python3
"""Forward-generate V102: task marts count scheduled runs, not task auto-retry attempts.

[MED] Same auto-retry double-count as V101, but in the two SP_LOAD_MARTS_V27 task arms:

  [6]  MART_TASK_GRAPH_DAILY  -- runs CTE: COUNT(*) AS TASK_RUNS,
                                 COUNT_IF(h.STATE = 'FAILED') AS FAILED_TASKS over raw
                                 TASK_HISTORY h -> RUNS_WITH_FAILURES over-counts graph runs
                                 as failed when a task failed then SUCCEEDED on retry.
  [6b] MART_TASK_NODE_DAILY   -- COUNT(*) AS RUNS, COUNT_IF(STATE = 'FAILED') AS FAILED plus
                                 the queue/exec percentiles over raw TASK_HISTORY -> a retried
                                 node is counted twice and its failed attempt inflates FAILED.

Both diverge from every LIVE reader (ops_sql.task_runs / task_recent_states /
task_graph_recent_runs), which collapse auto-retries to the terminal attempt via
  QUALIFY ROW_NUMBER() OVER (PARTITION BY [GRAPH_RUN_GROUP_ID,] DATABASE_NAME, SCHEMA_NAME,
                             NAME, SCHEDULED_TIME ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
so the Pipeline Health graph board (mart-first) reports failed graph runs the live drill shows
clean, and the per-node timing board double-counts retried nodes.

Fix: re-derive SP_LOAD_MARTS_V27 (from V095) so each task arm aggregates over a terminal-attempt
derived table with the matching QUALIFY -- [6] partitions INCLUDING GRAPH_RUN_GROUP_ID (mirrors
task_graph_recent_runs), [6b] per-node WITHOUT it (mirrors task_runs). WH_CREDITS still LEFT JOINs
attribution on the terminal attempt's QUERY_ID, so a rare failed-retry attempt's compute drops
from the graph-run credit rollup -- accepted: that rollup is contextual pipeline cost, not the
authoritative ledger (FACT_METERING / attribution marts), and it now matches the live run list.
Every other mart arm is byte-identical to V095.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V101, then
re-runs SP_LOAD_MARTS_V27('HOURLY', <days>) to re-stamp the trailing task-mart history with the
collapsed counts. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V095__cost_alloc_role_company.sql"

# ---- arm [6] MART_TASK_GRAPH_DAILY: collapse h before the runs-CTE aggregation ----
OLD_GRAPH = (
    "                           SUM(COALESCE(a.CREDITS, 0)) AS CREDITS\n"
    "                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h"
)
NEW_GRAPH = (
    "                           SUM(COALESCE(a.CREDITS, 0)) AS CREDITS\n"
    "                    FROM (\n"
    "                        -- V102: collapse task auto-retries to the terminal attempt so\n"
    "                        -- TASK_RUNS / FAILED_TASKS (-> RUNS_WITH_FAILURES) count scheduled\n"
    "                        -- tasks, not attempts, mirroring the live ops_sql.task_graph_recent_runs\n"
    "                        -- (a task auto-retried to success is no longer a graph-run failure).\n"
    "                        -- Credits still LEFT JOIN on the terminal attempt's QUERY_ID; a rare\n"
    "                        -- failed-retry attempt's compute drops from WH_CREDITS (accepted: this\n"
    "                        -- rollup is contextual pipeline cost, not the authoritative ledger).\n"
    "                        SELECT GRAPH_RUN_GROUP_ID, QUERY_ID, NAME, DATABASE_NAME, SCHEMA_NAME,\n"
    "                               SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, STATE\n"
    "                        FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "                        WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                          AND STATE IN ('SUCCEEDED', 'FAILED')\n"
    "                        QUALIFY ROW_NUMBER() OVER (\n"
    "                            PARTITION BY GRAPH_RUN_GROUP_ID, DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
    "                            ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1\n"
    "                    ) h"
)

# ---- arm [6b] MART_TASK_NODE_DAILY: collapse per-node before aggregation ----------
OLD_NODE = (
    "                FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "                WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                  AND STATE IN ('SUCCEEDED', 'FAILED')\n"
    "                GROUP BY 1, 2, 3, 4"
)
NEW_NODE = (
    "                FROM (\n"
    "                    -- V102: collapse task auto-retries to the terminal attempt so RUNS /\n"
    "                    -- FAILED and the queue/exec percentiles count scheduled runs, not\n"
    "                    -- attempts, mirroring the live ops_sql.task_runs / task_recent_states.\n"
    "                    SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,\n"
    "                           QUERY_START_TIME, COMPLETED_TIME, STATE\n"
    "                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "                    WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                      AND STATE IN ('SUCCEEDED', 'FAILED')\n"
    "                    QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
    "                                               ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1\n"
    "                ) th\n"
    "                GROUP BY 1, 2, 3, 4"
)


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_LOAD_MARTS_V27\(")
assert proc.count(OLD_GRAPH) == 1, f"graph arm: got {proc.count(OLD_GRAPH)}"
assert proc.count(OLD_NODE) == 1, f"node arm: got {proc.count(OLD_NODE)}"
proc = proc.replace(OLD_GRAPH, NEW_GRAPH).replace(OLD_NODE, NEW_NODE)

# both collapses landed (1 pre-existing top-query QUALIFY in V095 + 2 new task-arm collapses)
assert proc.count("QUALIFY ROW_NUMBER() OVER (") == 3
assert ("PARTITION BY GRAPH_RUN_GROUP_ID, DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME") in proc
assert ("QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
        "                                               ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1") in proc
# the failure/run counts survive (grain unchanged, only the source rowset collapsed)
assert "COUNT_IF(h.STATE = 'FAILED') AS FAILED_TASKS" in proc
assert "COUNT_IF(STATE = 'FAILED') AS FAILED" in proc
assert "COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES" in proc
# untouched sibling arms survive
assert "MART_COST_ALLOCATION_DAILY" in proc
assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in proc  # the V095 fix stays
assert "'SP_LOAD_MARTS_V27: SCOPE must be HOURLY or DAILY" in proc

out = f"""-- V102__task_marts_retry_collapse.sql
--
-- Task marts count scheduled runs, not task auto-retry attempts. The two SP_LOAD_MARTS_V27 task
-- arms aggregated raw TASK_HISTORY, so a task that failed then SUCCEEDED on retry was double-counted:
--   [6]  MART_TASK_GRAPH_DAILY -- COUNT_IF(h.STATE='FAILED') inflated RUNS_WITH_FAILURES, marking
--        clean graph runs as failed on the mart-first Pipeline Health board.
--   [6b] MART_TASK_NODE_DAILY  -- COUNT(*)/COUNT_IF(FAILED) and the queue/exec percentiles counted
--        the retried node twice.
-- Both diverged from the live readers (ops_sql.task_runs / task_recent_states /
-- task_graph_recent_runs), which collapse auto-retries to the terminal attempt.
--
-- Re-derives SP_LOAD_MARTS_V27 from V095 so each task arm aggregates over a terminal-attempt derived
-- table with the matching QUALIFY ROW_NUMBER() (graph arm partitions INCLUDING GRAPH_RUN_GROUP_ID,
-- node arm per-node without it). WH_CREDITS still LEFT JOINs attribution on the terminal QUERY_ID, so
-- a rare failed-retry attempt's compute drops from the graph-run credit rollup -- accepted: that
-- rollup is contextual pipeline cost, not the authoritative ledger, and it now matches the live run
-- list. Every other mart arm (incl. the V095 COMPANY_FOR_ROLE cost-alloc fix) is byte-identical.
-- No schema change; owner applies in Snowsight after V101, then re-runs SP_LOAD_MARTS_V27('HOURLY', d)
-- to re-stamp trailing task-mart history. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20102, 'V102 requires V101 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 101) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 102 AS VERSION,
       'Task-mart retry-collapse: SP_LOAD_MARTS_V27 re-derived from V095 so MART_TASK_GRAPH_DAILY [6] and MART_TASK_NODE_DAILY [6b] aggregate over terminal-attempt derived tables (QUALIFY ROW_NUMBER() PARTITION BY [GRAPH_RUN_GROUP_ID,] DATABASE_NAME/SCHEMA_NAME/NAME/SCHEDULED_TIME ORDER BY COMPLETED_TIME DESC), matching the live ops_sql readers -- a task auto-retried to success no longer inflates RUNS_WITH_FAILURES / FAILED, so the Pipeline Health graph board and per-node timing board stop over-reporting failures/runs the live drill collapses. WH_CREDITS joins the terminal attempt (rare failed-retry compute drops from the contextual graph-run rollup). Sibling arms (incl. V095 COMPANY_FOR_ROLE) byte-identical. Proc only, no schema change; owner re-runs SP_LOAD_MARTS_V27(HOURLY) to re-stamp.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 102);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert out.count("QUALIFY ROW_NUMBER() OVER (") == 3
assert "EXCEPTION (-20102" in out and "IF (v < 101) THEN" in out
assert "SELECT 102 AS VERSION" in out and "WHERE VERSION = 102)" in out

target = Path(os.environ.get("V102_OUT") or (MIG / "V102__task_marts_retry_collapse.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
