#!/usr/bin/env python3
"""Forward-generate V103: warehouse-efficiency mart ACTIVE_HOURS must be SPAN-based.

[HIGH] MART_WAREHOUSE_EFFICIENCY_DAILY.IDLE_PCT counts busy multi-hour query time as idle.
The wh_eff arm of SP_LOAD_MARTS_V27 computes
    ACTIVE_HOURS = COUNT(DISTINCT DATE_TRUNC('hour', START_TIME))   -- the query's START hour only
while BILLED_HOURS = COUNT_IF(CREDITS_USED > 0) counts every metered hour, and
    IDLE_PCT = 100 * (BILLED_HOURS - ACTIVE_HOURS) / BILLED_HOURS.
So a query that starts at 10:59 and runs three hours marks hour 10 active and leaves hours 11
and 12 looking IDLE -> a nightly 3-hour MERGE reads IDLE_PCT = (3-1)/3 = 66.7%.

The LIVE sibling (insights_sql.warehouse_sizing_profile via _active_hours_cte) already fixed this
on 2026-07-31: it expands each query across the hours it SPANS. The comment there
(insights_sql.py:26-38) documents the start-hour form as the exact bug. But the mart LOADER was
never updated, and the Operations right-sizing panel reads the mart FIRST
(run_mart_first(mart27_sql.eff_sizing_profile, ...)), so the stored, wrong IDLE_PCT drives the
SUSPEND/DOWN recommendation (sizing.py idle gates >=50 / >=30), IDLE_MONTHLY_USD, and the
'Idle $ on suspend-first WHs' KPI -- flipping a busy batch/ELT warehouse from KEEP to SUSPEND and
inflating its idle-$ to ~2/3 of its bill. Only the p95 peak-day basis is disclosed in the caption;
the idle-basis divergence is silent.

Fix: re-derive SP_LOAD_MARTS_V27 (from V102, the latest def) so the wh_eff arm computes ACTIVE_HOURS
SPAN-based -- expand each query across the clock hours it spans (bounded to 25, matching
_active_hours_cte's _ACTIVE_HOUR_SPAN), attribute each spanned hour to its own DAY, count distinct
warehouse-day-hours, and source IDLE_PCT from that. Every other mart arm (incl. the V095
COMPANY_FOR_ROLE cost-alloc fix and the V102 task-mart retry-collapse) is byte-identical.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V102, then re-runs
SP_LOAD_MARTS_V27('HOURLY', <days>) to re-stamp trailing MART_WAREHOUSE_EFFICIENCY_DAILY rows with
the corrected idle basis (existing stored rows keep the old IDLE_PCT until reloaded). This file
never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V102__task_marts_retry_collapse.sql"

# ---- A: drop start-hour ACTIVE_HOURS from `q`; add span-based qh + q_active CTEs ----
OLD_Q = (
    "                q AS (\n"
    "                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,\n"
    "                           COUNT(*) AS QUERIES,\n"
    "                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,\n"
    "                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,\n"
    "                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,\n"
    "                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,\n"
    "                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS,\n"
    "                           COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) AS ACTIVE_HOURS\n"
    "                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
    "                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                      AND WAREHOUSE_NAME IS NOT NULL\n"
    "                    GROUP BY 1, 2\n"
    "                )"
)
NEW_Q = (
    "                q AS (\n"
    "                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,\n"
    "                           COUNT(*) AS QUERIES,\n"
    "                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,\n"
    "                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,\n"
    "                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,\n"
    "                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,\n"
    "                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS\n"
    "                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
    "                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                      AND WAREHOUSE_NAME IS NOT NULL\n"
    "                    GROUP BY 1, 2\n"
    "                ),\n"
    "                -- V103: ACTIVE_HOURS must count every clock hour a query was RUNNING, not just\n"
    "                -- its START hour. The old COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) marked\n"
    "                -- hours 11 and 12 of a 10:59->13:00 query IDLE, so IDLE_PCT (and every $ derived\n"
    "                -- from it: the SUSPEND/DOWN sizing verdict, IDLE_MONTHLY_USD, the idle-$ KPI)\n"
    "                -- overstated idle for any multi-hour query. Expand each query across the hours it\n"
    "                -- SPANS (bounded to 25, matching insights_sql._active_hours_cte), attribute each\n"
    "                -- spanned hour to its own DAY, and count distinct warehouse-day-hours.\n"
    "                qh AS (\n"
    "                    SELECT s.WAREHOUSE_NAME,\n"
    "                           DATE(DATEADD('hour', g.SEQ, s.H0)) AS DAY,\n"
    "                           DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS\n"
    "                    FROM (\n"
    "                        SELECT WAREHOUSE_NAME,\n"
    "                               DATE_TRUNC('hour', START_TIME) AS H0,\n"
    "                               DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1\n"
    "                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
    "                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                          AND WAREHOUSE_NAME IS NOT NULL\n"
    "                    ) s\n"
    "                    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 25))) g\n"
    "                      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1\n"
    "                ),\n"
    "                q_active AS (\n"
    "                    SELECT WAREHOUSE_NAME, DAY, COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS\n"
    "                    FROM qh\n"
    "                    GROUP BY 1, 2\n"
    "                )"
)

# ---- B: source ACTIVE_HOURS/IDLE_PCT from q_active; LEFT JOIN it on the effective day ----
OLD_SEL = (
    "                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,\n"
    "                       COALESCE(q.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,\n"
    "                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(q.ACTIVE_HOURS, 0), 0)\n"
    "                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,\n"
    "                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY\n"
    "                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME"
)
NEW_SEL = (
    "                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,\n"
    "                       COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,\n"
    "                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(qa.ACTIVE_HOURS, 0), 0)\n"
    "                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,\n"
    "                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY\n"
    "                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME\n"
    "                LEFT JOIN q_active qa ON qa.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)\n"
    "                                     AND qa.DAY = COALESCE(m.DAY, q.DAY)"
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
assert proc.count(OLD_Q) == 1, f"q CTE: got {proc.count(OLD_Q)}"
assert proc.count(OLD_SEL) == 1, f"final SELECT/join: got {proc.count(OLD_SEL)}"
proc = proc.replace(OLD_Q, NEW_Q).replace(OLD_SEL, NEW_SEL)

# fix landed
assert "qh AS (" in proc and "q_active AS (" in proc
assert "COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS" in proc
assert "COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) AS ACTIVE_HOURS" not in proc  # old form gone
assert "GENERATOR(ROWCOUNT => 25)" in proc
assert "LEFT JOIN q_active qa ON qa.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)" in proc
assert "COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS" in proc
# the V095 + V102 fixes upstream survive untouched
assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in proc
assert proc.count("QUALIFY ROW_NUMBER() OVER (") == 3   # V095 top-query + V102's two task collapses
assert "MART_WAREHOUSE_EFFICIENCY_DAILY" in proc

out = f"""-- V103__wh_efficiency_active_hours_span.sql
--
-- Warehouse-efficiency mart ACTIVE_HOURS must be SPAN-based, not START-hour-based. The wh_eff arm
-- of SP_LOAD_MARTS_V27 counted only a query's START hour as active (COUNT(DISTINCT
-- DATE_TRUNC('hour', START_TIME))), so every hour after the start of a multi-hour query was marked
-- IDLE: a nightly 3-hour MERGE read IDLE_PCT = (3-1)/3 = 66.7%. Because the Operations right-sizing
-- panel reads this mart FIRST, the stored wrong IDLE_PCT flipped busy batch/ELT warehouses from
-- KEEP to SUSPEND and inflated the idle-$ KPI. The live sibling (insights_sql._active_hours_cte)
-- fixed this on 2026-07-31 by expanding each query across the hours it SPANS; the mart loader was
-- never updated.
--
-- Re-derives SP_LOAD_MARTS_V27 from V102 so the wh_eff arm computes ACTIVE_HOURS span-based (expand
-- each query across its clock hours, bounded to 25 like _ACTIVE_HOUR_SPAN, attribute each spanned
-- hour to its own DAY, count distinct warehouse-day-hours) and sources IDLE_PCT from it. Every other
-- mart arm (incl. the V095 COMPANY_FOR_ROLE cost-alloc fix and the V102 task-mart retry-collapse) is
-- byte-identical. No schema change; owner applies in Snowsight after V102, then re-runs
-- SP_LOAD_MARTS_V27('HOURLY', d) to re-stamp trailing rows with the corrected idle basis. This file
-- never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20103, 'V103 requires V102 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 102) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 103 AS VERSION,
       'Warehouse-efficiency ACTIVE_HOURS span-based: SP_LOAD_MARTS_V27 re-derived from V102 so the wh_eff arm counts every clock hour a query was RUNNING (span expansion bounded to 25, matching insights_sql._active_hours_cte) instead of only its START hour. Fixes MART_WAREHOUSE_EFFICIENCY_DAILY.IDLE_PCT overstating idle for multi-hour queries (a 3-hour MERGE no longer reads 66.7% idle), which had flipped busy batch/ELT warehouses from KEEP to SUSPEND on the mart-first right-sizing panel and inflated the idle-$ KPI. Matches the live warehouse_sizing_profile. Other mart arms (V095 COMPANY_FOR_ROLE, V102 task retry-collapse) byte-identical. Proc only, no schema change; owner re-runs SP_LOAD_MARTS_V27(HOURLY) to re-stamp trailing rows.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 103);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "qh AS (" in out and "q_active AS (" in out
assert "EXCEPTION (-20103" in out and "IF (v < 102) THEN" in out
assert "SELECT 103 AS VERSION" in out and "WHERE VERSION = 103)" in out

target = Path(os.environ.get("V103_OUT") or (MIG / "V103__wh_efficiency_active_hours_span.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
