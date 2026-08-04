#!/usr/bin/env python3
"""Forward-generate V073: calendar MTD/YTD rows in MART_EXEC_BOARD."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V069__exec_board_serverless_ai_drivers.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


board = extract_proc(BASE.read_text(encoding="utf-8"), "SP_REFRESH_EXEC_BOARD")
old = """    windows AS (
        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
        UNION ALL SELECT 60 UNION ALL SELECT 90
        UNION ALL SELECT 180 UNION ALL SELECT 365  -- V052: mart-history windows
    ),"""
new = """    windows AS (
        -- V073: fixed rolling windows plus Snowsight-style calendar presets.
        -- MTD/YTD are day OFFSETS because the joins are inclusive of CURRENT_DATE.
        -- DISTINCT prevents a duplicate board when today's offset equals a fixed pill.
        SELECT DISTINCT WINDOW_DAYS
        FROM (
            SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
            UNION ALL SELECT 60 UNION ALL SELECT 90
            UNION ALL SELECT 180 UNION ALL SELECT 365
            UNION ALL
            SELECT DATEDIFF('day', DATE_TRUNC('month', CURRENT_DATE()), CURRENT_DATE())
            UNION ALL
            SELECT DATEDIFF('day', DATE_TRUNC('year', CURRENT_DATE()), CURRENT_DATE())
        ) calendar_windows
    ),"""
assert board.count(old) == 1, "latest board windows shape drifted"
board = board.replace(old, new)

for guard in (
    "'COST_DRIVER_SVC', 'CREDITS'",
    "UNION ALL SELECT 'UNKNOWN'",
    "SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;",
    "RETURN 'exec board refreshed (atomic swap)';",
):
    assert guard in board, guard

out = f"""-- V073__calendar_triage_windows.sql
--
-- The global triage filter now offers Current month and Current year, matching
-- Snowsight Admin > Cost Management calendar presets. MART_EXEC_BOARD previously
-- materialized only seven fixed rolling windows, so a calendar selection had no
-- exact first-paint board row. Re-derived from the latest definition (V069), this
-- adds account-time MTD/YTD day offsets to the windows CTE. DISTINCT handles days
-- where a calendar offset equals a fixed option. Source horizons, output contract,
-- atomic stage swap, UNKNOWN scope, and serverless/AI driver arm are unchanged.
--
-- Owner applies in Snowsight after V072. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20073, 'V073 requires V072 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 72) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD  (calendar MTD/YTD window rows)
{board}
-- Populate today's calendar offsets immediately; the hourly task refreshes them
-- as the account date advances.
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 73 AS VERSION,
       'Calendar triage windows: SP_REFRESH_EXEC_BOARD adds deduplicated Current month and Current year day offsets alongside the seven fixed rolling windows, matching Snowsight Admin Cost Management date presets while preserving the board contract and atomic swap.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 73);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20073" in out and "IF (v < 72) THEN" in out
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1
assert "SELECT 73 AS VERSION" in out and "WHERE VERSION = 73)" in out

target = Path(os.environ.get("V073_OUT") or (MIG / "V073__calendar_triage_windows.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
