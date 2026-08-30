#!/usr/bin/env python3
"""Forward-generate V105: CREATE OR REPLACE TABLE is scored DESTRUCTIVE change-risk.

[MED] A `CREATE OR REPLACE TABLE PRODDB.SALES.CUSTOMERS AS SELECT ...` drops and rebuilds a live
table -- destructive by any reasonable definition -- but the FACT_SECURITY_CHANGE loader classified
it purely by QUERY_TYPE: CREATE_TABLE / CREATE_TABLE_AS_SELECT fall into the ELSE arm =>
CHANGE_KIND='CREATE', RISK_SCORE base 30 (~40-50 with PROD/admin bumps). Both
change_risk_destructive_breakdown (security_sql.py) and the V088 CHANGE RISK exception-queue arm
filter CHANGE_KIND='DESTRUCTIVE' AND RISK_SCORE>=70, so a genuinely destructive replace entered
NEITHER -- a false all-clear on the destructive-events KPI / change-risk queue that feeds the
CHANGE RISK domain posture score. The preview text that reveals 'OR REPLACE' was captured but never
inspected.

Fix: in BOTH SP_LOAD_SECURITY_FACTS arms (the d<=3 OW_QH_EXTRACT arm and the d>3 QUERY_HISTORY
backfill arm, which are byte-identical), when the row is a table create (QUERY_TYPE IN
('CREATE_TABLE','CREATE_TABLE_AS_SELECT')) whose text contains 'OR REPLACE', set
CHANGE_KIND='DESTRUCTIVE' and RISK_SCORE base 55 (same band as ALTER, NOT 90).

Why base 55 is safe (does NOT re-flood the panel V080/V088 de-noised): base 55 + the existing bumps
means a routine service-role replace stays MEDIUM and BELOW the 70 destructive-queue threshold --
and crucially the ETL/service roles that drove the historical destructive flood (V080's 18 TF_* /
Glue / Informatica / TF_O_*_SYSADMIN roles) are NONE of them ACCOUNTADMIN/SNOW_ACCOUNTADMINS, so
they can never earn the +10 admin bump; a TF_* replace on a PROD-named DB reaches at most
55+10=65 < 70 and never enters the queue/breakdown. Only a PROD replace by an actual admin role
(55+10 PROD +10 admin = 75 >= 70) surfaces -- the human-wipes-prod case this fix exists to catch.
The V080 exception-queue role exclusion remains a further backstop. CREATE OR REPLACE VIEW is left
alone (definition churn, no data loss). The live recent_ddl_changes builder gets the same base-55
bump (app-side) so the "Who changed what" feed stays consistent.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V104, then re-runs
SP_LOAD_SECURITY_FACTS(90) to re-stamp trailing FACT_SECURITY_CHANGE rows with the new
classification. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V100__security_change_fact_reload_gap.sql"

# CHANGE_KIND: promote a CREATE OR REPLACE TABLE to DESTRUCTIVE (both arms => count 2)
OLD_CK = (
    "                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'\n"
    "                     ELSE 'CREATE'"
)
NEW_CK = (
    "                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'\n"
    "                     WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')\n"
    "                          AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'\n"
    "                     ELSE 'CREATE'"
)

# RISK_SCORE: base 55 for a CREATE OR REPLACE TABLE (both arms => count 2)
OLD_RS = (
    "                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55\n"
    "                       ELSE 30"
)
NEW_RS = (
    "                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55\n"
    "                       WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')\n"
    "                            AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55\n"
    "                       ELSE 30"
)


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_LOAD_SECURITY_FACTS\(")
assert proc.count(OLD_CK) == 2, f"CHANGE_KIND anchor: got {proc.count(OLD_CK)}"
assert proc.count(OLD_RS) == 2, f"RISK_SCORE anchor: got {proc.count(OLD_RS)}"
proc = proc.replace(OLD_CK, NEW_CK).replace(OLD_RS, NEW_RS)

# fix landed in BOTH arms
assert proc.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'") == 2
assert proc.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55") == 2
# the existing DROP/TRUNCATE=DESTRUCTIVE/90 classification is untouched
assert proc.count("WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'") == 2
assert proc.count("WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90") == 2
# both reload arms + the V100 reload-gap fix survive
assert "IF (d <= 3)" in proc
assert "EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)" in proc
# proc body creates no schema (checked on the proc, not the prose header/description)
assert "CREATE OR REPLACE VIEW" not in proc and "CREATE OR REPLACE FUNCTION" not in proc
assert "CREATE TABLE " not in proc and "ALTER TABLE " not in proc and "CREATE TASK" not in proc

out = f"""-- V105__change_risk_create_or_replace_destructive.sql
--
-- CREATE OR REPLACE TABLE is scored DESTRUCTIVE change-risk. The FACT_SECURITY_CHANGE loader
-- classified change-risk purely by QUERY_TYPE, so a CREATE OR REPLACE TABLE (QUERY_TYPE
-- 'CREATE_TABLE'/'CREATE_TABLE_AS_SELECT') that drops and rebuilds a live table fell into the ELSE
-- arm => CHANGE_KIND='CREATE', RISK_SCORE base 30. change_risk_destructive_breakdown and the V088
-- CHANGE RISK exception-queue arm both require CHANGE_KIND='DESTRUCTIVE' AND RISK_SCORE>=70, so a
-- genuinely destructive replace entered neither -- a false all-clear on the destructive-events KPI.
--
-- Re-derives SP_LOAD_SECURITY_FACTS from V100 so BOTH arms (d<=3 OW_QH_EXTRACT + d>3 QUERY_HISTORY
-- backfill) mark a table create whose text contains 'OR REPLACE' as CHANGE_KIND='DESTRUCTIVE' with
-- RISK_SCORE base 55 (same band as ALTER, NOT 90). Base 55 + the existing PROD/admin bumps keeps
-- routine service-role replaces below the 70 queue threshold -- the V080 ETL/service roles that
-- drove the historical destructive flood are never ACCOUNTADMIN/SNOW_ACCOUNTADMINS, so a service
-- replace reaches at most 65; only a PROD replace by an admin role (75) surfaces. CREATE OR REPLACE
-- VIEW is left alone. No schema change; owner applies after V104 and re-runs
-- SP_LOAD_SECURITY_FACTS(90) to re-stamp trailing FACT_SECURITY_CHANGE rows. This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20105, 'V105 requires V104 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 104) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 105 AS VERSION,
       'Change-risk CREATE OR REPLACE destructive: SP_LOAD_SECURITY_FACTS re-derived from V100 so both reload arms mark a table create (CREATE_TABLE / CREATE_TABLE_AS_SELECT) whose text contains OR REPLACE as CHANGE_KIND=DESTRUCTIVE with RISK_SCORE base 55 (ALTER band, not 90). A CREATE OR REPLACE TABLE that wipes a live table now enters the destructive-events breakdown and the RISK>=70 change-risk queue when done by an admin role on a PROD db (55+10+10=75), fixing a false all-clear -- while base 55 keeps routine service-role replaces (never ACCOUNTADMIN/SNOW_ACCOUNTADMINS, so <=65) out of the queue so the V080/V088 de-noise is preserved. CREATE OR REPLACE VIEW untouched. Live recent_ddl_changes given the matching base-55 bump app-side. Proc only, no schema change; owner re-runs SP_LOAD_SECURITY_FACTS(90) to re-stamp.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 105);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS" in out
assert out.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'") == 2
assert out.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55") == 2
assert "EXCEPTION (-20105" in out and "IF (v < 104) THEN" in out
assert "SELECT 105 AS VERSION" in out and "WHERE VERSION = 105)" in out

target = Path(os.environ.get("V105_OUT") or (MIG / "V105__change_risk_create_or_replace_destructive.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
