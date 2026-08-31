#!/usr/bin/env python3
"""Forward-generate V113: MART_INCIDENT_TIMELINE TASK_FAIL uses COMPLETED_TIME (incident-hunt finding).

[MED] The incident correlation timeline places a TASK_FAIL event at a DIFFERENT instant on its two
paths: the live reader mart_sql.incident_timeline uses COMPLETED_TIME (the failure's instant), but the
7-day mart path -- the MART_INCIDENT_TIMELINE TASK_FAIL arm loaded by SP_LOAD_MARTS_V27 (latest def
V103) -- uses QUERY_START_TIME and bounds its window on QUERY_START_TIME. For a task that ran a while
before failing, the same failure appears shifted by the run duration between the 48h live view and the
7d mart view, so cause/effect ordering in the correlation timeline can invert.

Fix: re-derive SP_LOAD_MARTS_V27 from V103 with the TASK_FAIL arm selecting and bounding on
COMPLETED_TIME (matching the live reader, the semantically correct choice for a failure event).
Everything else byte-identical. Owner applies in Snowsight after V112; the next SP_LOAD_MARTS_V27 run
re-stamps the mart. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V103__wh_efficiency_active_hours_span.sql"

OLD_ARM = (
    "            SELECT QUERY_START_TIME, 'TASK_FAIL',\n"
    "                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),\n"
    "                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME\n"
    "            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "            WHERE QUERY_START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'"
)
NEW_ARM = (
    "            SELECT COMPLETED_TIME, 'TASK_FAIL',\n"
    "                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),\n"
    "                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME\n"
    "            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY\n"
    "            WHERE COMPLETED_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'"
)


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_LOAD_MARTS_V27\(")
assert proc.count(OLD_ARM) == 1, f"TASK_FAIL arm: got {proc.count(OLD_ARM)}"
proc = proc.replace(OLD_ARM, NEW_ARM)
assert NEW_ARM in proc
assert "SELECT QUERY_START_TIME, 'TASK_FAIL'" not in proc
assert "MART_INCIDENT_TIMELINE" in proc   # the arm's target survives
# V103's wh-efficiency ACTIVE_HOURS span fix survives untouched
assert "IDLE_PCT" in proc

out = f"""-- V113__incident_timeline_task_fail_completed_time.sql
--
-- MART_INCIDENT_TIMELINE TASK_FAIL uses COMPLETED_TIME (incident-hunt). The mart's TASK_FAIL arm
-- (loaded by SP_LOAD_MARTS_V27) selected and bounded on QUERY_START_TIME while the live reader
-- mart_sql.incident_timeline uses COMPLETED_TIME, so the same task failure appeared at different
-- instants on the 48h live vs 7d mart paths and could invert cause/effect ordering in the correlation
-- timeline. Re-derives SP_LOAD_MARTS_V27 from V103 with the TASK_FAIL arm on COMPLETED_TIME (the
-- failure's instant, matching the reader); everything else byte-identical. No schema change; owner
-- applies after V112 and the next SP_LOAD_MARTS_V27 run re-stamps the mart. Never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20113, 'V113 requires V112 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 112) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 113 AS VERSION,
       'Incident timeline TASK_FAIL uses COMPLETED_TIME: SP_LOAD_MARTS_V27 re-derived from V103 so the MART_INCIDENT_TIMELINE TASK_FAIL arm selects and bounds on COMPLETED_TIME instead of QUERY_START_TIME, matching the live reader mart_sql.incident_timeline. The same task failure no longer appears at different instants on the 48h live vs 7d mart paths, so cause/effect ordering in the correlation timeline is consistent. Everything else byte-identical (incl. the V103 wh-efficiency ACTIVE_HOURS span fix). Proc only, no schema change; forward-healing on the next SP_LOAD_MARTS_V27 run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 113);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "SELECT COMPLETED_TIME, 'TASK_FAIL'" in out
assert "EXCEPTION (-20113" in out and "IF (v < 112) THEN" in out
assert "SELECT 113 AS VERSION" in out and "WHERE VERSION = 113)" in out

target = Path(os.environ.get("V113_OUT") or (MIG / "V113__incident_timeline_task_fail_completed_time.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
