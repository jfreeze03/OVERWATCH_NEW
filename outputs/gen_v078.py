#!/usr/bin/env python3
"""Forward-generate V078: cast the ai_code arm's FIRST_TS/LAST_TS to
TIMESTAMP_NTZ in SP_LOAD_MARTS_V27.

Live failure (owner-run backfill, 2026-08-13, APP_ERROR_LOG context
'FACT_AI_USAGE_DAILY (code views)'):

    SQL compilation error: Expression type does not match column data type,
    expecting TIMESTAMP_NTZ(9) but got TIMESTAMP_TZ(9) for column FIRST_TS

CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY / CORTEX_CODE_CLI_USAGE_HISTORY expose
USAGE_TIME as TIMESTAMP_TZ; FACT_AI_USAGE_DAILY.FIRST_TS/LAST_TS are
TIMESTAMP_NTZ and MERGE does not coerce TZ->NTZ. The sibling ai_functions arm
survives because CORTEX_FUNCTIONS_USAGE_HISTORY.START_TIME is TIMESTAMP_LTZ,
which does coerce. The failed arm is why the mart never gains SOURCE<>'Functions'
rows, the app's coverage gate never passes, and the 20s live Cortex scan fires
on every AI-users render.

Re-derived byte-for-byte from the latest proc definition (V066) plus ONE
enumerated edit to the ai_code arm; every other arm is byte-identical."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


proc = extract_proc(BASE.read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27")

# The one edit: only the ai_code arm builds FIRST_TS/LAST_TS from c.USAGE_TIME
# (the ai_functions arm uses START_TIME), so the anchor is unique by construction.
edits = [
    (
        "                       MIN(c.USAGE_TIME) AS FIRST_TS,\n"
        "                       MAX(c.USAGE_TIME) AS LAST_TS,",
        "                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact\n"
        "                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ\n"
        "                       -- (live 2026-08-13: \"expecting TIMESTAMP_NTZ(9) but got\n"
        "                       -- TIMESTAMP_TZ(9) for column FIRST_TS\" killed this arm on\n"
        "                       -- every run, starving the AI coverage gate).\n"
        "                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,\n"
        "                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,",
    ),
]

for old, new in edits:
    assert proc.count(old) == 1, f"anchor drifted:\n{old[:80]}"
    proc = proc.replace(old, new)

# Sanity: the cast landed, the functions arm is untouched, and the surrounding
# arms are still present byte-for-byte.
for guard in (
    "MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS",
    "MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS",
    "MIN(START_TIME) AS FIRST_TS",                       # ai_functions arm unchanged
    "MART_TASK_NODE_DAILY - other marts unaffected",     # [6b] unchanged
    "MART_SECURITY_POSTURE_DAILY - other marts unaffected",
    "FACT_AI_USAGE_DAILY (code views) - other marts unaffected",
):
    assert guard in proc, guard
assert proc.count("::TIMESTAMP_NTZ AS FIRST_TS") == 1
assert proc.count("::TIMESTAMP_NTZ AS LAST_TS") == 1

out = f"""-- V078__ai_usage_ts_cast.sql
--
-- Unbreak the FACT_AI_USAGE_DAILY ai_code loader arm: CORTEX_CODE_SNOWSIGHT/
-- CLI_USAGE_HISTORY expose USAGE_TIME as TIMESTAMP_TZ, the fact's FIRST_TS/
-- LAST_TS are TIMESTAMP_NTZ, and MERGE refuses the implicit TZ->NTZ coercion —
-- so the arm failed on EVERY run ("expecting TIMESTAMP_NTZ(9) but got
-- TIMESTAMP_TZ(9) for column FIRST_TS", APP_ERROR_LOG context
-- 'FACT_AI_USAGE_DAILY (code views)'). Without SOURCE<>'Functions' rows the
-- app's AI coverage gate never passes and the Chargeback & AI -> AI users panel
-- pays a ~20s live ACCOUNT_USAGE scan on every render. Two ::TIMESTAMP_NTZ
-- casts fix it; the ai_functions arm (TIMESTAMP_LTZ, coerces fine) and every
-- other arm are byte-identical to V066.
--
-- Ends with a DAILY/365 first-fill so the apply itself heals the mart depth —
-- the same call the owner ran on 2026-08-13 (a few minutes on WH_ALFA_ADMIN),
-- this time with the ai_code arm landing rows.
--
-- POST-APPLY VERIFY (do not skip): ai_code is an OPTIONAL loader arm, so if the
-- cast were still wrong the first-fill would report 'MARTS OK ... [1 optional
-- failed]' and version 78 would stamp anyway. Confirm the heal:
--   SELECT MIN(DAY) AS FIRST_DAY, COUNT(*) AS ROWS
--   FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY WHERE SOURCE <> 'Functions';
-- Healthy = rows present and FIRST_DAY near a year back (or the account's first
-- Cortex Code usage day). Zero rows = check APP_ERROR_LOG context
-- 'FACT_AI_USAGE_DAILY (code views)'.
--
-- Owner applies in Snowsight after V077. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20078, 'V078 requires V077 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 77) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27  (ai_code arm FIRST_TS/LAST_TS ::TIMESTAMP_NTZ casts only)
{proc}
-- First fill: rerun the DAILY backfill so FACT_AI_USAGE_DAILY gains the
-- code-view rows the broken arm never landed (coverage gate needs the depth).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 78 AS VERSION,
       'AI usage loader unbreak: SP_LOAD_MARTS_V27 ai_code arm casts MIN/MAX(USAGE_TIME) to TIMESTAMP_NTZ (CORTEX_CODE_* views are TIMESTAMP_TZ; MERGE does not coerce), ending the every-run arm failure that starved FACT_AI_USAGE_DAILY of code-view rows and forced the 20s live Cortex fallback. All other arms byte-identical to V066. DAILY/365 first-fill included.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 78);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20078" in out and "IF (v < 77) THEN" in out
assert "SELECT 78 AS VERSION" in out and "WHERE VERSION = 78)" in out
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);") == 1

target = Path(os.environ.get("V078_OUT") or (MIG / "V078__ai_usage_ts_cast.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
