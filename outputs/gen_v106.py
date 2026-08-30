#!/usr/bin/env python3
"""Forward-generate V106: COST_DEPT_BUDGET_PACE alert join is case-insensitive on both sides.

[LOW] The COST_DEPT_BUDGET_PACE alert arm (SP_ALERT_SCAN [17]) joins the department map to the
warehouse fact with a ONE-SIDED case fold:
    LEFT JOIN FACT_WAREHOUSE_DAILY f ON f.WAREHOUSE_NAME = UPPER(m.NAME)
DEPARTMENT_MAP.NAME is stored uppercased by the app MERGE (ai_chargeback.py `sql_literal(name.upper())`),
so `UPPER(m.NAME)` is a no-op and the comparison is effectively raw-fact-name vs an uppercase string.
FACT_WAREHOUSE_DAILY.WAREHOUSE_NAME is copied verbatim from WAREHOUSE_METERING_HISTORY with no UPPER,
preserving the raw case of a quoted mixed-case warehouse identifier (e.g. "Etl_Prod"). So
'Etl_Prod' = 'ETL_PROD' fails, the LEFT JOIN misses, SUM(f.CREDITS_TOTAL) folds to 0 via COALESCE,
MTD_USD is understated, and the MTD>=50 / OVER_PCT>THRESHOLD gates never fire -- while the Cost >
Chargeback screen (which uppercases BOTH sides, chargeback_sql.py) shows the department well over
budget. The two surfaces the owner reads side by side disagree.

Fix: re-derive SP_ALERT_SCAN (from V104, the latest def) with the join case-folded on both sides
(`ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)`), matching the chargeback screen. Plain identifier
comparison -- no `--`/apostrophe injection risk.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V105; the next
hourly SP_ALERT_SCAN evaluates the corrected join. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V104__sec_cred_expiry_dedupe_key.sql"

OLD_JOIN = "                  ON f.WAREHOUSE_NAME = UPPER(m.NAME)"
NEW_JOIN = "                  ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN\(")
assert proc.count(OLD_JOIN) == 1, f"COST_DEPT_BUDGET_PACE join: got {proc.count(OLD_JOIN)}"
proc = proc.replace(OLD_JOIN, NEW_JOIN)

assert NEW_JOIN in proc and OLD_JOIN not in proc
# the arm and its context survive
assert "COST_DEPT_BUDGET_PACE" in proc
assert "DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP m" in proc
# V104's cred-expiry key (no week token) survives -- we're building on top of it
assert ("c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
        "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')\n") in proc

out = f"""-- V106__cost_dept_budget_pace_case_insensitive_join.sql
--
-- COST_DEPT_BUDGET_PACE alert join is case-insensitive on both sides. The [17] arm of
-- SP_ALERT_SCAN joined FACT_WAREHOUSE_DAILY to DEPARTMENT_MAP with a one-sided fold
-- (f.WAREHOUSE_NAME = UPPER(m.NAME)); since the fact preserves the raw case of a quoted mixed-case
-- warehouse identifier, a warehouse like "Etl_Prod" failed the join, folded MTD_USD to 0, and the
-- department never tripped the over-budget gates -- while the Cost > Chargeback screen (which
-- uppercases both sides) showed it over budget. The two surfaces disagreed.
--
-- Re-derives SP_ALERT_SCAN from V104 with the join case-folded on both sides
-- (UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)), matching the chargeback screen. Everything else
-- (incl. the V104 cred-expiry dedupe fix) is byte-identical. No schema change; owner applies in
-- Snowsight after V105 and the next hourly SP_ALERT_SCAN evaluates the corrected join. This file
-- never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20106, 'V106 requires V105 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 105) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 106 AS VERSION,
       'COST_DEPT_BUDGET_PACE case-insensitive join: SP_ALERT_SCAN re-derived from V104 so the [17] arm joins FACT_WAREHOUSE_DAILY to DEPARTMENT_MAP via UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME) instead of the one-sided f.WAREHOUSE_NAME = UPPER(m.NAME). A quoted mixed-case warehouse identifier (e.g. "Etl_Prod") no longer fails the join and folds MTD_USD to 0, so the department-budget-pace alert stops silently missing an over-budget department the Chargeback screen (which uppercases both sides) shows. Everything else byte-identical (incl. V104 cred-expiry fix). Proc only, no schema change; forward-healing on the next hourly SP_ALERT_SCAN.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 106);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in out
assert "EXCEPTION (-20106" in out and "IF (v < 105) THEN" in out
assert "SELECT 106 AS VERSION" in out and "WHERE VERSION = 106)" in out

target = Path(os.environ.get("V106_OUT") or (MIG / "V106__cost_dept_budget_pace_case_insensitive_join.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
