#!/usr/bin/env python3
"""Forward-generate V052__exec_board_windows_180_365.sql.

Owner ask 2026-07-27: expand the window filter to 180 and 365 days
("mart-history only" — keep live ACCOUNT_USAGE scans at 90).

The exec board (MART_EXEC_BOARD, Overview KPIs) pre-computes one KPI set per
window in the loader's `windows` CTE, and a lock requires that set to EQUAL
app.config.DAY_WINDOW_OPTIONS — otherwise a selected window with no board row
falls through to a 13-month live scan. So adding 180/365 to the config tuple
requires re-deriving SP_REFRESH_EXEC_BOARD to compute those windows too. The
board reads long-retention facts (FACT_METERING_DAILY et al., 800-day
retention), so 180/365 are real history, not a live rescan.

Derivation law: SP_REFRESH_EXEC_BOARD from V045 (its CURRENT definition)
verbatim + one enumerated edit (the windows CTE gains 180 and 365);
tests/test_v052_windows.py re-derives and byte-compares.
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
        assert n == 1, f"{name}: needle x{n}: {old[:80]!r}"
        body = body.replace(old, new)
    return body


proc = apply(extract_proc("V045__task_monitoring_restored.sql", "SP_REFRESH_EXEC_BOARD"), [
    ("""        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
        UNION ALL SELECT 60 UNION ALL SELECT 90
    )""",
     """        SELECT 7 AS WINDOW_DAYS UNION ALL SELECT 14 UNION ALL SELECT 30
        UNION ALL SELECT 60 UNION ALL SELECT 90
        UNION ALL SELECT 180 UNION ALL SELECT 365  -- V052: mart-history windows
    )"""),
], "board")

out = f"""-- V052__exec_board_windows_180_365.sql — long-history window filter
-- (owner ask 2026-07-27: expand triage window to 180/365, mart-history only).
--
--   Adds 180 and 365 to the exec-board loader's window set so Overview's KPIs
--   have real board rows for the two new filter options (the loader windows
--   are locked to equal app.config.DAY_WINDOW_OPTIONS). Board inputs are
--   long-retention facts, so these are history, not a live rescan. Live
--   ACCOUNT_USAGE scans stay capped at 90 (MAX_LIVE_WINDOW_DAYS) — the app
--   surfaces which panels honor the long window.
--
--   Proc swap + one board reload; no new objects. Apply AFTER V051.
--   Idempotent; safe to re-run.
--
-- Derivation law: SP_REFRESH_EXEC_BOARD from V045 verbatim + one enumerated
-- edit (windows CTE gains 180/365); tests/test_v052_windows.py re-derives
-- and byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20052, 'V052 requires V051 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 51) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD
{proc}
-- Populate the new 180/365 board rows immediately (the daily task keeps them fresh).
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 52 AS VERSION,
       'Exec-board loader windows gain 180 and 365 (mart-history window filter); live ACCOUNT_USAGE scans stay capped at 90' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 52);
"""
target = Path(os.environ.get("V052_OUT") or (MIG / "V052__exec_board_windows_180_365.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
