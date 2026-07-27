#!/usr/bin/env python3
"""Forward-generate V054__exec_board_window_history.sql.

Codex r29 #1 (CONFIRMED, deployed regression): V052 added 180/365 to the
exec-board loader's `windows` CTE, but the three source CTEs it reads from
(wh_daily / qh_daily / tk_daily) still cap at 90 days
(`WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())`). The window join then
reads from those already-capped frames, so the 180- and 365-day pills compute
over only 90 days of data — values identical to the 90-day row. The board
advertises long windows but reads 90.

Fix: widen the three source horizons to 365 (= max app.config.DAY_WINDOW_OPTIONS).
The CTEs read FACT_*_DAILY (mart, long retention — see SP_PURGE_FACTS below), so
365-day reads are history, not a live rescan, and the aggregate-once/join-small
shape is unchanged.

Codex r29 #20 companion (source history must exist to be read): the daily-fact
retention FLOOR in SP_PURGE_FACTS was 180, so a low FACT_RETENTION_DAYS_DAILY
setting could leave the 365 window silently short even after the read is fixed.
Raise the floor to 365 so retained history always covers the max advertised
window. Under the default 800-day setting this is a runtime no-op; it only
tightens the guarantee (and makes the CI invariant in
tests/test_v054_window_history.py enforceable end-to-end: read horizon AND
retained history both >= max window).

Codex r29 #7 (QH watermark advance) was NOT bundled here — it is already fixed:
SP_LOAD_QH_EXTRACT was re-derived in V042 (codex_r22 #7) to gate the watermark
MERGE behind an `ok` flag set only on the extract arm's COMMIT. Codex cited the
superseded V041 body.

Derivation law:
  - SP_REFRESH_EXEC_BOARD from V052 (its CURRENT definition) verbatim + three
    enumerated horizon edits (-90 -> -365, one per source CTE).
  - SP_PURGE_FACTS from V045 (its CURRENT definition) verbatim + one enumerated
    floor edit (180 -> 365).
tests/test_v054_window_history.py re-derives and byte-compares.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

MAX_WINDOW = 365  # == max(app.config.DAY_WINDOW_OPTIONS); the CI invariant couples them


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


# --- #1: exec-board source horizons 90 -> 365 (one edit per source CTE) --------
board = apply(extract_proc("V052__exec_board_windows_180_365.sql", "SP_REFRESH_EXEC_BOARD"), [
    ("""        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())""",
     """        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())"""),
    ("""        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())""",
     """        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())"""),
    ("""        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())""",
     """        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())"""),
], "board")

# --- #20 companion: daily-fact retention floor 180 -> 365 ----------------------
purge = apply(extract_proc("V045__task_monitoring_restored.sql", "SP_PURGE_FACTS"), [
    ("    daily_days := GREATEST(daily_days, 180);",
     "    daily_days := GREATEST(daily_days, 365);"),
], "purge")

out = f"""-- V054__exec_board_window_history.sql — the long windows actually read long
-- (Codex r29 #1, CONFIRMED regression in deployed V052).
--
--   V052 added 180/365 to the exec-board `windows` CTE, but the three source
--   CTEs (wh_daily/qh_daily/tk_daily) still capped at 90 days, so the 180/365
--   pills computed over only 90 days — identical to the 90-day row. This
--   widens the three source horizons to 365 (= max DAY_WINDOW_OPTIONS). The
--   sources are FACT_*_DAILY (mart, long retention), so this is history, not a
--   live rescan; the aggregate-once/join-small shape is unchanged.
--
--   Companion (Codex r29 #20): raise the SP_PURGE_FACTS daily-fact retention
--   floor 180 -> 365 so retained history always covers the max advertised
--   window (a no-op under the default 800-day setting; it only tightens the
--   guarantee). tests/test_v054_window_history.py locks BOTH: read horizon and
--   retention floor >= max window.
--
--   Two proc swaps + one board reload; no new objects. Apply AFTER V053.
--   Idempotent; safe to re-run. (Codex r29 #7 not included — already fixed in
--   V042/r22 #7; that reviewer cited the superseded V041 body.)
--
-- Derivation law: SP_REFRESH_EXEC_BOARD from V052 + three horizon edits;
-- SP_PURGE_FACTS from V045 + one floor edit; the test re-derives and
-- byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20054, 'V054 requires V053 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 53) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD
{board}
-- >>> derived:SP_PURGE_FACTS
{purge}
-- Rebuild the board so the 180/365 pills carry real long-window data now (the
-- daily task keeps them fresh thereafter).
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 54 AS VERSION,
       'Exec-board 180/365 windows read full history (V052 capped their source CTEs at 90); daily-fact retention floor raised 180->365 so advertised windows always have source history' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 54);
"""
target = Path(os.environ.get("V054_OUT") or (MIG / "V054__exec_board_window_history.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines; MAX_WINDOW={MAX_WINDOW}")
