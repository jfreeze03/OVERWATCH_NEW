#!/usr/bin/env python3
"""Forward-generate V095: evidence-based ROLE company classification in cost allocation.

SP_LOAD_MARTS_V27's cost-allocation arm ([5]) stamps the COMPANY of the ROLE
dimension of MART_COST_ALLOCATION_DAILY with an inline
`CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END`, which
defaults every non-TRXS role to ALFA and can never emit 'UNKNOWN' -- violating the
V044 evidence-based classification law (residual = UNKNOWN, never a silent ALFA
default). Its sibling dimensions in the SAME MERGE already route through UDFs that DO
return UNKNOWN for no-evidence entities (USER -> COMPANY_FOR_USER, DATABASE / SCHEMA ->
COMPANY_FOR_DATABASE). Consequences: shared roles (PUBLIC / SYSADMIN / ACCOUNTADMIN)
that are neither %TRXS% nor %ALFA% inflate the ALFA ROLE total; the app's first-class
UNKNOWN company pill returns ZERO ROLE-dim rows while USER / DATABASE dims populate it;
a Trexis role not literally containing 'TRXS' is silently booked to ALFA.

Fix (mirrors the sibling dims exactly): introduce a COMPANY_FOR_ROLE(R) scalar UDF that
classifies a role NAME by the SAME evidence the live V044 COMPANY_FOR_USER role
predicates and app.companies.role_clause already use -- %TRXS% -> Trexis, %ALFA% or the
two DBA roles (SNOW_ACCOUNTADMINS / SNOW_SYSADMINS) -> ALFA, else UNKNOWN -- and
re-derive SP_LOAD_MARTS_V27 (from its LATEST def, V082) so the ROLE arm calls
DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME) instead of the inline CASE. This
centralizes the role-company law server-side (the buggy inline CASE was copy-pasted
across ~19 historical, now-superseded migrations; a UDF stops it drifting back) and
makes the ROLE dim structurally identical to USER / DATABASE / SCHEMA.

New scalar UDF + procedure re-derivation: no table / schema change, no backfill in this
file. Owner applies in Snowsight after V094, then re-runs the SP_LOAD_MARTS_V27(HOURLY)
backfill to re-stamp trailing MART_COST_ALLOCATION_DAILY ROLE history. This file never
runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V082__query_family_company_regrain.sql"

# --- the single proc edit: the ROLE-dimension classifier routes through a UDF -------
# The inline CASE defaulted every non-TRXS role to ALFA (never UNKNOWN); replace it
# with the COMPANY_FOR_ROLE UDF created below, exactly mirroring how the USER /
# DATABASE / SCHEMA arms in the same MERGE already call COMPANY_FOR_USER /
# COMPANY_FOR_DATABASE. Byte-scoped to the extracted SP_LOAD_MARTS_V27 proc only --
# the identical inline CASE also lives in ~19 other (superseded) migrations, so a
# repo-wide replace would be wrong.
OLD_ROLE = "CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END"
NEW_ROLE = "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27")

assert proc.count(OLD_ROLE) == 1, f"expected exactly 1 ROLE CASE, got {proc.count(OLD_ROLE)}"
proc = proc.replace(OLD_ROLE, NEW_ROLE)
assert NEW_ROLE in proc and OLD_ROLE not in proc
# the ELSE 'ALFA' silent default is gone from the extracted proc (it occurred once)
assert "ELSE 'ALFA'" not in proc
# the sibling classifiers survive UNTOUCHED (USER once, DATABASE/SCHEMA twice), and the
# ROLE arm now calls COMPANY_FOR_ROLE just like them
assert proc.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,") == 1
assert proc.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),") == 2
assert proc.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME),") == 1
# untouched anchors: the alloc arm structure + the wider loader carry over from V082
for anchor in (
    "MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t",
    "SELECT DAY, 'ROLE', ROLE_NAME,",
    "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)",
    "loaded := loaded || 'alloc ';",
    "EXECUTE AS OWNER",
):
    assert anchor in proc, anchor

out = f"""-- V095__cost_alloc_role_company.sql
--
-- Evidence-based ROLE company classification in cost allocation. SP_LOAD_MARTS_V27's
-- cost-allocation arm stamped the COMPANY of MART_COST_ALLOCATION_DAILY's ROLE
-- dimension with an inline CASE that defaulted every non-TRXS role to ALFA and could
-- never emit 'UNKNOWN' -- violating the V044 evidence-based classification law (the
-- residual is UNKNOWN, never a silent ALFA default). Its sibling dims already route
-- through UDFs that DO return UNKNOWN (USER -> COMPANY_FOR_USER, DATABASE / SCHEMA ->
-- COMPANY_FOR_DATABASE), so shared roles inflated the ALFA ROLE total and the app's
-- first-class UNKNOWN company pill returned zero ROLE-dim rows while USER / DATABASE
-- populated it.
--
-- Introduces COMPANY_FOR_ROLE(R): a scalar UDF that classifies a role NAME by the SAME
-- evidence the live V044 COMPANY_FOR_USER role predicates and app.companies.role_clause
-- already use -- %TRXS% -> Trexis, %ALFA% or the two DBA roles (SNOW_ACCOUNTADMINS /
-- SNOW_SYSADMINS) -> ALFA, else UNKNOWN. Then re-derives SP_LOAD_MARTS_V27 (from its
-- LATEST def, V082) so the ROLE arm calls COMPANY_FOR_ROLE(ROLE_NAME) instead of the
-- inline CASE -- making the ROLE dim structurally identical to its siblings and
-- centralizing the role-company law server-side.
--
-- New scalar UDF + procedure re-derivation: no table / schema change, no backfill in
-- this file. Owner applies in Snowsight after V094, then re-runs the
-- SP_LOAD_MARTS_V27(HOURLY) backfill to re-stamp trailing ROLE-dim history. This file
-- never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20095, 'V095 requires V094 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 94) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- COMPANY_FOR_ROLE: role-name -> company, mirroring the V044 COMPANY_FOR_USER role
-- evidence and app.companies.role_clause. NULL-safe via COALESCE; residual = 'UNKNOWN'.
-- The two shared DBA roles (SNOW_ACCOUNTADMINS / SNOW_SYSADMINS) count as ALFA
-- evidence, consistent with the live COMPANY_FOR_USER law -- they are the only
-- account-access roles (owner decision 2026-07-13).
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(R VARCHAR)
RETURNS VARCHAR
AS
$$
    CASE
        WHEN UPPER(COALESCE(R, '')) LIKE '%TRXS%' THEN 'Trexis'
        WHEN UPPER(COALESCE(R, '')) LIKE '%ALFA%'
             OR UPPER(COALESCE(R, '')) IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS') THEN 'ALFA'
        ELSE 'UNKNOWN'
    END
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 95 AS VERSION,
       'Cost-allocation ROLE company classification: new COMPANY_FOR_ROLE(R) scalar UDF (TRXS->Trexis, ALFA-name or SNOW_ACCOUNTADMINS/SNOW_SYSADMINS->ALFA, else UNKNOWN -- same evidence as COMPANY_FOR_USER and app.companies.role_clause) plus SP_LOAD_MARTS_V27 re-derived from V082 so MART_COST_ALLOCATION_DAILY ROLE dim calls COMPANY_FOR_ROLE(ROLE_NAME) instead of an inline CASE that defaulted every non-TRXS role to ALFA and never emitted UNKNOWN. Fixes the V044 UNKNOWN-law bypass -- shared roles no longer inflate ALFA and the UNKNOWN pill now populates ROLE like USER/DATABASE. Sibling arms byte-identical to V082. New UDF + proc, no schema change, no backfill; owner re-runs SP_LOAD_MARTS_V27(HOURLY) to re-stamp ROLE history.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 95);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert out.count(
    "CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(R VARCHAR)"
) == 1
assert "CREATE OR REPLACE VIEW" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "INSERT OVERWRITE" not in out          # no data rewrite in this migration
assert "ELSE 'ALFA' END" not in out           # the silent-default bug is gone
assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in out
assert "EXCEPTION (-20095" in out and "IF (v < 94) THEN" in out
assert "SELECT 95 AS VERSION" in out and "WHERE VERSION = 95)" in out

target = Path(os.environ.get("V095_OUT")
              or (MIG / "V095__cost_alloc_role_company.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
