#!/usr/bin/env python3
"""Forward-generate V100: fix the security change-fact reload dropping the oldest day's hours.

[HIGH] The standing task calls SP_LOAD_SECURITY_FACTS(3), so d=3 takes the `IF (d <= 3)`
branch, which reloads FACT_SECURITY_CHANGE from the 3-day scratch extract OW_QH_EXTRACT:

    DELETE FROM ...FACT_SECURITY_CHANGE WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());
    ...
    FROM ...OW_QH_EXTRACT WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())

Both the DELETE and the reinsert are anchored on CURRENT_DATE() (calendar midnight), so they
cover [midnight of D-3, now] = up to 72h + hour-of-day. But OW_QH_EXTRACT keeps only a rolling
~72h (SP_LOAD_QH_EXTRACT purges START_TIME < now-72h, V041/V094). So the reinsert asks the
extract for data back to midnight-of-(D-3) that the extract already purged: everything in
[midnight of D-3, now-72h) is deleted from the fact but absent from the source, and once that
day drops out of the DELETE window on the next calendar day the loss is permanent. Nothing
else writes FACT_SECURITY_CHANGE (V080/V088 are view-only; reconcile does not rebuild it), so
the CHANGE RISK exception-queue arm (7-day window) reads near-empty for events older than ~2
days — a silent security-visibility loss (a GRANT ACCOUNTADMIN / DROP on PROD / CREATE_USER
older than the extract's reach simply disappears).

Fix: make the d<=3 change reload self-consistent with the extract's real coverage. Delete ONLY
the window the extract can refill -- `WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM
OW_QH_EXTRACT)` -- so older already-loaded rows are preserved instead of erased-and-not-
refilled. The d>3 full-backfill branch reads ACCOUNT_USAGE.QUERY_HISTORY directly (full
history), so it keeps the whole-calendar-window DELETE; the previously-shared DELETE is moved
into each branch. (An empty extract makes MIN() NULL -> the delete is a no-op, safe.)

Procedure re-derivation only, no schema change, no backfill in this file (the next hourly run
self-heals the recent window; the older days that were already lost are historical and stay
lost until a manual SP_LOAD_SECURITY_FACTS(90) backfill). Owner applies in Snowsight after V099.
This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V075__security_operating_model.sql"

_INSERT_HEADER = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
    "            (QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,\n"
    "             DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,\n"
    "             RISK_LEVEL, QUERY_PREVIEW)\n"
    "        WITH raw AS ("
)

# --- edit 1: the shared calendar-window DELETE moves INTO the d<=3 branch as an
# extract-scoped delete (delete only what OW_QH_EXTRACT can refill). ------------------
OLD_1 = (
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
    "     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());\n"
    "\n"
    "    IF (d <= 3) THEN\n"
    "        " + _INSERT_HEADER
)
NEW_1 = (
    "    IF (d <= 3) THEN\n"
    "        -- V100: the d<=3 change reload reads OW_QH_EXTRACT, which retains only a rolling\n"
    "        -- ~72h (SP_LOAD_QH_EXTRACT purges START_TIME < now-72h). Deleting the full calendar\n"
    "        -- window (DAY >= -:d days = up to 72h + hour-of-day) while the extract can refill\n"
    "        -- only the last ~72h silently dropped the earliest hours of the oldest day, for good.\n"
    "        -- Delete ONLY the window the extract actually covers, so older already-loaded rows\n"
    "        -- are preserved instead of erased-and-not-refilled. Empty extract -> MIN() NULL ->\n"
    "        -- the delete matches nothing (safe no-op).\n"
    "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
    "         WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);\n"
    "        " + _INSERT_HEADER
)

# --- edit 2: the d>3 full-backfill branch keeps the whole-calendar-window DELETE
# (it reads ACCOUNT_USAGE.QUERY_HISTORY directly, which has full history). ------------
OLD_2 = (
    "    ELSE\n"
    "        " + _INSERT_HEADER
)
NEW_2 = (
    "    ELSE\n"
    "        -- Full backfill / manual path (d>3): reads ACCOUNT_USAGE.QUERY_HISTORY directly\n"
    "        -- (full history), so delete the whole calendar window and rebuild it.\n"
    "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
    "         WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());\n"
    "        " + _INSERT_HEADER
)


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_LOAD_SECURITY_FACTS")
for old in (OLD_1, OLD_2):
    assert proc.count(old) == 1, f"expected exactly 1 of a change-fact anchor, got {proc.count(old)}"
proc = proc.replace(OLD_1, NEW_1).replace(OLD_2, NEW_2)
# fixes landed: the extract-scoped delete (d<=3) + the moved full-window delete (ELSE)
assert NEW_1 in proc and NEW_2 in proc
assert "WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);" in proc
# the shared pre-IF calendar-window delete is gone (it was moved into the two branches)
assert "     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());\n\n    IF (d <= 3) THEN" not in proc
# untouched anchors: both raw-CTE inserts + both source reads + the login-fact load survive
assert proc.count("WITH raw AS (") == 2
assert "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT" in proc
assert "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in proc
assert "FACT_SECURITY_LOGIN_DAILY" in proc

out = f"""-- V100__security_change_fact_reload_gap.sql
--
-- Fix the security change-fact reload silently dropping the oldest day's earliest hours.
-- SP_LOAD_SECURITY_FACTS(3) (the standing task) reloads FACT_SECURITY_CHANGE from the 3-day
-- scratch extract OW_QH_EXTRACT, but the DELETE + reinsert were anchored on CURRENT_DATE()
-- (calendar midnight, up to 72h + hour-of-day) while the extract retains only a rolling ~72h
-- (SP_LOAD_QH_EXTRACT purges START_TIME < now-72h). So [midnight of D-3, now-72h) was deleted
-- from the fact but absent from the source and never re-inserted -- and once that day left the
-- delete window the loss was permanent. Nothing else writes FACT_SECURITY_CHANGE, so the
-- CHANGE RISK exception-queue arm read near-empty for events older than ~2 days (a GRANT
-- ACCOUNTADMIN / DROP on PROD / CREATE_USER simply disappeared) -- a silent security-visibility
-- loss.
--
-- Re-derives SP_LOAD_SECURITY_FACTS from V075: the d<=3 change reload now deletes ONLY the
-- window OW_QH_EXTRACT can actually refill (EVENT_TS >= MIN(extract.START_TIME)), so older
-- already-loaded rows are preserved; the d>3 full-backfill branch (reads QUERY_HISTORY
-- directly, full history) keeps the whole-calendar-window DELETE. The previously-shared DELETE
-- is moved into each branch. No schema change; the next hourly run self-heals the recent
-- window (older lost days need a manual SP_LOAD_SECURITY_FACTS(90) backfill). Owner applies in
-- Snowsight after V099. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20100, 'V100 requires V099 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 99) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 100 AS VERSION,
       'Security change-fact reload gap: SP_LOAD_SECURITY_FACTS re-derived from V075 so the d<=3 FACT_SECURITY_CHANGE reload deletes only the window OW_QH_EXTRACT can refill (EVENT_TS >= MIN(extract.START_TIME)) instead of the full calendar window it could not refill from the rolling ~72h scratch table -- which was silently, permanently dropping the earliest hours of the oldest day and near-emptying the CHANGE RISK exception-queue arm for events older than ~2 days. The d>3 full-backfill branch (reads QUERY_HISTORY directly) keeps the whole-window delete. Proc only, no schema change, no backfill; forward-healing on the next hourly run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 100);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)" in out
assert "EXCEPTION (-20100" in out and "IF (v < 99) THEN" in out
assert "SELECT 100 AS VERSION" in out and "WHERE VERSION = 100)" in out

target = Path(os.environ.get("V100_OUT")
              or (MIG / "V100__security_change_fact_reload_gap.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
