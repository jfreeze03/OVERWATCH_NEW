#!/usr/bin/env python3
"""Forward-generate V107: COST_DEPT_BUDGET_PACE department join + pace-window corrections.

Two confirmed cost-hunt #5 findings, both inside the [17] COST_DEPT_BUDGET_PACE arm of
SP_ALERT_SCAN (the def V106 last re-derived), fixed together in one re-derivation:

[MED] Department join is case-sensitive. The arm joins DEPT_BUDGETS b to DEPARTMENT_MAP m on
    m.DEPARTMENT = b.DEPARTMENT
a plain, un-folded equality on the free-text DEPARTMENT string. DEPARTMENT is written verbatim on
BOTH sides (ai_chargeback.py uppercases only NAME, not DEPARTMENT; the budget MERGE writes it raw),
so a case drift like 'Etl' vs 'ETL' for the same logical department makes the LEFT JOIN miss, no
FACT_WAREHOUSE_DAILY rows attach, MTD_USD folds to 0 via COALESCE, OVER_PCT = -100%, and the rule
silently never fires -- while Cost > Chargeback (which groups by DEPARTMENT_MAP.DEPARTMENT and shows
real spend, and lists the budget) shows the department over budget. This is the exact sibling of the
warehouse-name case-fold V106 already fixed, left on the other join key. Fix: case-fold both sides,
UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT).

[LOW] Pace window is partial-on-one-side / full-on-the-other. MTD_USD sums FACT_WAREHOUSE_DAILY where
f.DAY >= DATE_TRUNC('month', CURRENT_DATE()) -- which includes today's still-growing (or loader-
lagged, near-zero) partial row -- but TIME_SHARE = DAY(CURRENT_DATE()) / DAY(LAST_DAY(...)) scales the
budget to a FULLY elapsed today. Numerator through ~yesterday, denominator through end-of-today, so
OVER_PCT is systematically understated early in the month (a 2x-pace dept on day 2 reads 0% over),
biasing toward a false all-clear. Fix: put both on completed days only -- exclude today from MTD
(AND f.DAY < CURRENT_DATE()) and base TIME_SHARE on completed days
((DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE()))).

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V106; the next
hourly SP_ALERT_SCAN evaluates the corrected join + pace window. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V106__cost_dept_budget_pace_case_insensitive_join.sql"

OLD_DEPT = "AND m.DEPARTMENT = b.DEPARTMENT"
NEW_DEPT = "AND UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT)"

OLD_TIME = "DAY(CURRENT_DATE()) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE"
NEW_TIME = "(DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE"

OLD_MTD = "AND f.DAY >= DATE_TRUNC('month', CURRENT_DATE())"
NEW_MTD = ("AND f.DAY >= DATE_TRUNC('month', CURRENT_DATE())\n"
           "                 AND f.DAY < CURRENT_DATE()")


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")

# base sanity: V106's warehouse-name case-fold is present and its department join is not yet folded
assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in proc, "V106 warehouse-name fold missing from base"
assert proc.count(OLD_DEPT) == 1, f"department join: got {proc.count(OLD_DEPT)}"
assert proc.count(OLD_TIME) == 1, f"TIME_SHARE expr: got {proc.count(OLD_TIME)}"
assert proc.count(OLD_MTD) == 1, f"MTD month bound: got {proc.count(OLD_MTD)}"

proc = proc.replace(OLD_DEPT, NEW_DEPT)
proc = proc.replace(OLD_TIME, NEW_TIME)
proc = proc.replace(OLD_MTD, NEW_MTD)

# post-conditions: exactly the three targeted edits, nothing else disturbed
assert NEW_DEPT in proc and "AND m.DEPARTMENT = b.DEPARTMENT" not in proc
assert NEW_TIME in proc
assert "AND f.DAY < CURRENT_DATE()" in proc
assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in proc  # V106 fix still intact
assert "COST_DEPT_BUDGET_PACE" in proc

out = f"""-- V107__cost_dept_budget_pace_dept_join_and_pace_window.sql
--
-- COST_DEPT_BUDGET_PACE department join + pace window. Two cost-hunt #5 fixes in the [17] arm of
-- SP_ALERT_SCAN (the def V106 last re-derived):
--   1. [MED] the department join m.DEPARTMENT = b.DEPARTMENT was case-sensitive on a free-text
--      string written verbatim on both sides, so a case drift ('Etl' vs 'ETL') made the join miss,
--      folded MTD_USD to 0, and the rule silently never fired while Cost > Chargeback showed the
--      department over budget. Now case-folded: UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT) -- the
--      sibling of the warehouse-name fold V106 fixed.
--   2. [LOW] MTD_USD summed FACT_WAREHOUSE_DAILY including today's partial row while TIME_SHARE
--      counted today as fully elapsed, so OVER_PCT was understated early in the month (a 2x-pace
--      department read on-pace on day 2). Now both use completed days only: today is excluded from
--      MTD (AND f.DAY < CURRENT_DATE()) and TIME_SHARE = (DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY()).
--
-- Re-derives SP_ALERT_SCAN from V106; everything else (incl. the V106 warehouse-name case-fold and
-- the V104 cred-expiry dedupe) is byte-identical. No schema change; owner applies in Snowsight after
-- V106 and the next hourly SP_ALERT_SCAN evaluates the corrected join + window. Never runs from app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20107, 'V107 requires V106 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 106) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 107 AS VERSION,
       'COST_DEPT_BUDGET_PACE department join + pace window: SP_ALERT_SCAN re-derived from V106 so the [17] arm (1) joins DEPT_BUDGETS to DEPARTMENT_MAP via UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT) instead of the case-sensitive m.DEPARTMENT = b.DEPARTMENT -- a case drift no longer folds MTD_USD to 0 and silently suppresses an over-budget department the Chargeback screen shows; and (2) computes MTD_USD over completed days only (f.DAY < CURRENT_DATE()) with TIME_SHARE = (DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) so the pace numerator and denominator cover the same elapsed window and early-month OVER_PCT is no longer understated. Everything else byte-identical (incl. V106 warehouse-name case-fold, V104 cred-expiry fix). Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 107);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT)" in out
assert "(DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE" in out
assert "AND f.DAY < CURRENT_DATE()" in out
assert "EXCEPTION (-20107" in out and "IF (v < 106) THEN" in out
assert "SELECT 107 AS VERSION" in out and "WHERE VERSION = 107)" in out

target = Path(os.environ.get("V107_OUT") or (MIG / "V107__cost_dept_budget_pace_dept_join_and_pace_window.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
