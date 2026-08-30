#!/usr/bin/env python3
"""Forward-generate V101: FACT_TASK_DAILY loader counts scheduled runs, not retry attempts.

[MED] Snowflake task auto-retries emit MULTIPLE TASK_HISTORY rows for one scheduled run (a
FAILED attempt then a SUCCEEDED retry, sharing SCHEDULED_TIME). Every LIVE task reader
deliberately collapses these to the terminal attempt via
  QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                             ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
(ops_sql.task_runs / task_recent_states / task_graph_recent_runs, commented "a FAILED attempt
that later SUCCEEDED on retry is not counted as a failure"). But the FACT_TASK_DAILY loader
(SP_LOAD_DAILY_FACTS, latest def V064) aggregates raw TASK_HISTORY with COUNT(*) AS RUNS and
SUM(IFF(STATE='FAILED',1,0)) AS FAILED and NO such collapse — so FACT_TASK_DAILY over-counts
runs and failures. The Task Health panel reads the mart FIRST (mart_sql.fact_task_daily), so a
task that failed-then-recovered shows FAILED>=1 on the default (mart) path while the live
task_runs fallback shows FAILED=0 for the same task/day; day_task_failures (the incident-replay
drill) and the PIPE_TASK_FAILURES alert (>= threshold) inherit the same phantom.

Fix: re-derive SP_LOAD_DAILY_FACTS (from V064) so the FACT_TASK_DAILY rollup aggregates over a
terminal-attempt CTE (the same QUALIFY the live readers use), matching the live counts. The
FACT_METERING/STORAGE/LOGIN_DAILY arms and everything else are byte-identical to V064.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V100, then the
next hourly SP_LOAD_DAILY_FACTS(3) run re-stamps the trailing FACT_TASK_DAILY days with the
collapsed counts (the DELETE+INSERT already rebuilds the trailing 3 days each run, so it
self-heals with no explicit backfill). This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V064__webhook_drain_watermarks_alert_burn_telemetry.sql"

OLD = (
    "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY\n"
    "        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)\n"
    "    SELECT\n"
    "        DATE(QUERY_START_TIME),\n"
    "        DATABASE_NAME,\n"
    "        SCHEMA_NAME,\n"
    "        NAME,\n"
    "        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),\n"
    "        COUNT(*),\n"
    "        SUM(IFF(STATE = 'FAILED', 1, 0)),\n"
    "        AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),\n"
    "        MAX_BY(STATE, QUERY_START_TIME),\n"
    "        MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)\n"
    "    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "    WHERE QUERY_START_TIME >= :lo_task::DATE\n"
    "    GROUP BY 1, 2, 3, 4, 5;"
)

NEW = (
    "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY\n"
    "        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)\n"
    "    WITH task_attempts AS (\n"
    "        -- V101: collapse task auto-retries to the terminal attempt so RUNS/FAILED count\n"
    "        -- scheduled runs, not attempts (a FAILED attempt that SUCCEEDED on retry is NOT a\n"
    "        -- failure) — matching ops_sql.task_runs / task_recent_states, so the mart Task\n"
    "        -- Health panel stops over-reporting failures the live tab collapses.\n"
    "        SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME,\n"
    "               COMPLETED_TIME, STATE, ERROR_MESSAGE\n"
    "        FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "        WHERE QUERY_START_TIME >= :lo_task::DATE\n"
    "        QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
    "                                   ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1\n"
    "    )\n"
    "    SELECT\n"
    "        DATE(QUERY_START_TIME),\n"
    "        DATABASE_NAME,\n"
    "        SCHEMA_NAME,\n"
    "        NAME,\n"
    "        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),\n"
    "        COUNT(*),\n"
    "        SUM(IFF(STATE = 'FAILED', 1, 0)),\n"
    "        AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),\n"
    "        MAX_BY(STATE, QUERY_START_TIME),\n"
    "        MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)\n"
    "    FROM task_attempts\n"
    "    GROUP BY 1, 2, 3, 4, 5;"
)


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_LOAD_DAILY_FACTS")
assert proc.count(OLD) == 1, f"expected 1 FACT_TASK_DAILY rollup, got {proc.count(OLD)}"
proc = proc.replace(OLD, NEW)
# fix landed
assert "WITH task_attempts AS (" in proc
assert ("QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
        "                                   ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1") in proc
assert "FROM task_attempts\n    GROUP BY 1, 2, 3, 4, 5;" in proc
# untouched anchors: the FACT_LOGIN / METERING / STORAGE arms + the task-fact DELETE survive
assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_task::DATE;" in proc
assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_login::DATE;" in proc
assert "FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY" in proc
assert "FACT_METERING_DAILY" in proc and "FACT_STORAGE_DAILY" in proc
# only ONE TASK_HISTORY scan remains in the proc (the collapsed one)
assert proc.count("FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY") == 1

out = f"""-- V101__fact_task_daily_retry_collapse.sql
--
-- FACT_TASK_DAILY loader counts scheduled runs, not task auto-retry attempts. Snowflake task
-- auto-retries emit multiple TASK_HISTORY rows for one scheduled run (a FAILED attempt then a
-- SUCCEEDED retry, sharing SCHEDULED_TIME). The live readers (ops_sql.task_runs /
-- task_recent_states / task_graph_recent_runs) all collapse these to the terminal attempt, but
-- the FACT_TASK_DAILY loader (SP_LOAD_DAILY_FACTS, V064) aggregated raw TASK_HISTORY with
-- COUNT(*)/SUM(FAILED), so the mart over-counted runs and failures — a task that failed then
-- recovered shows FAILED>=1 on the default (mart) Task Health panel while the live task_runs
-- fallback shows FAILED=0; day_task_failures and the PIPE_TASK_FAILURES alert inherit the phantom.
--
-- Re-derives SP_LOAD_DAILY_FACTS from V064 so the FACT_TASK_DAILY rollup aggregates over a
-- terminal-attempt CTE (the same QUALIFY the live readers use); all other arms are byte-identical.
-- No schema change; the next hourly SP_LOAD_DAILY_FACTS(3) run rebuilds the trailing days with the
-- collapsed counts (self-healing, no explicit backfill). Owner applies in Snowsight after V100.
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20101, 'V101 requires V100 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 100) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 101 AS VERSION,
       'FACT_TASK_DAILY retry-collapse: SP_LOAD_DAILY_FACTS re-derived from V064 so the FACT_TASK_DAILY RUNS/FAILED rollup aggregates over a terminal-attempt CTE (QUALIFY ROW_NUMBER() PARTITION BY DATABASE_NAME/SCHEMA_NAME/NAME/SCHEDULED_TIME ORDER BY COMPLETED_TIME DESC), matching the live ops_sql.task_runs — a task auto-retried to success no longer counts as a mart failure, so the Task Health panel, day_task_failures drill and PIPE_TASK_FAILURES alert stop over-reporting failures the live tab collapses. Other loader arms byte-identical. Proc only, no schema change; self-heals on the next hourly run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 101);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "WITH task_attempts AS (" in out
assert "EXCEPTION (-20101" in out and "IF (v < 100) THEN" in out
assert "SELECT 101 AS VERSION" in out and "WHERE VERSION = 101)" in out

target = Path(os.environ.get("V101_OUT") or (MIG / "V101__fact_task_daily_retry_collapse.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
