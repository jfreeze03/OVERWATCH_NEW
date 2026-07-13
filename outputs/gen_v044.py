#!/usr/bin/env python3
"""Forward-generate V044__unknown_classification.sql (r28 / adjudication #18).

Owner 2026-07-13: "do 18" — unknown entities stop defaulting to ALFA.
The model becomes evidence-based on BOTH sides: Trexis by mapping/prefix/
role (unchanged), ALFA by mapping/name-pattern/role, residual = UNKNOWN.
COMPANY_SCOPE mapping rows are the explicit-classification lever.

Derivation law: COMPANY_FOR_WAREHOUSE + COMPANY_FOR_DATABASE from V001,
COMPANY_FOR_USER from V019, SP_REFRESH_EXEC_BOARD from V043 — verbatim +
enumerated edits; tests/test_v044_unknown.py re-derives and byte-compares.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_fn(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE FUNCTION DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


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


wh = apply(extract_fn("V001__core.sql", "COMPANY_FOR_WAREHOUSE"), [
    ("""          WHERE SCOPE_TYPE = 'WAREHOUSE' AND PATTERN = UPPER(COALESCE(WH, ''))),
        'ALFA'
    )""",
     """          WHERE SCOPE_TYPE = 'WAREHOUSE' AND PATTERN = UPPER(COALESCE(WH, ''))),
        -- V044 (#18): ALFA needs evidence too (WH_ALFA_* naming);
        -- anything else is UNKNOWN until a COMPANY_SCOPE row maps it.
        IFF(UPPER(COALESCE(WH, '')) LIKE 'WH!_ALFA!_%' ESCAPE '!', 'ALFA', 'UNKNOWN')
    )"""),
], "wh")

db = apply(extract_fn("V001__core.sql", "COMPANY_FOR_DATABASE"), [
    ("""    IFF(UPPER(COALESCE(DB, '')) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis', 'ALFA')""",
     """    COALESCE(
        -- V044 (#18): explicit mapping wins (SCOPE_TYPE='DATABASE' rows —
        -- DBA_MAINT_DB is seeded ALFA below: the app infra is ALFA-owned).
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'DATABASE' AND PATTERN = UPPER(COALESCE(DB, ''))),
        IFF(UPPER(COALESCE(DB, '')) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis',
        IFF(UPPER(COALESCE(DB, '')) LIKE 'ALFA%' OR UPPER(COALESCE(DB, '')) = 'ADMIN',
            'ALFA', 'UNKNOWN'))
    )"""),
], "db")

usr = apply(extract_fn("V019__scoping_fixes.sql", "COMPANY_FOR_USER"), [
    ("""            'Trexis', 'ALFA')
    )""",
     """            'Trexis',
            -- V044 (#18): ALFA needs role evidence too (%ALFA% roles or the
            -- two DBA roles); no company-indicating role = UNKNOWN.
            IFF(EXISTS (
                    SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
                    WHERE g.DELETED_ON IS NULL
                      AND UPPER(g.GRANTEE_NAME) = UPPER(COALESCE(U, ''))
                      AND (g.ROLE ILIKE '%ALFA%'
                           OR g.ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'))),
                'ALFA', 'UNKNOWN'))
    )"""),
], "usr")

board = apply(extract_proc("V043__task_retirement_alert_teeth.sql", "SP_REFRESH_EXEC_BOARD"), [
    ("""        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'""",
     """        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'
        UNION ALL SELECT 'UNKNOWN'  -- V044 (#18): the unmapped bucket is a first-class pill"""),
], "board")

out = f"""-- V044__unknown_classification.sql — adjudication #18 (owner: "do 18").
--
--   Unknown entities stop defaulting to ALFA. Classification is now
--   evidence-based on BOTH sides:
--     warehouse: COMPANY_SCOPE row -> WH_TRXS_* list stays Trexis via rows;
--                WH_ALFA_* -> ALFA; residual -> UNKNOWN
--     database:  COMPANY_SCOPE 'DATABASE' row -> TRXS_* -> ALFA%/ADMIN ->
--                residual UNKNOWN (DBA_MAINT_DB seeded ALFA: app infra)
--     user:      USER_OVERRIDE row -> %TRXS% role -> %ALFA% or DBA role ->
--                residual UNKNOWN (SYSTEM lands here on purpose: it runs
--                both companies' work)
--   The exec board gains an UNKNOWN scope so the pill is mart-served.
--
--   HISTORY NOTE: mart rows keep the COMPANY stamped at load time. The
--   nightly reconcile re-stamps the trailing 3 days; older rows re-stamp
--   only if you re-run the backfill. Go-forward is honest immediately.
--
-- Derivation law: UDFs from V001/V019, board proc from V043, verbatim +
-- enumerated edits; tests/test_v044_unknown.py re-derives and compares.

-- >>> derived:COMPANY_FOR_WAREHOUSE
{wh}
-- >>> derived:COMPANY_FOR_DATABASE
{db}
-- >>> derived:COMPANY_FOR_USER
{usr}
-- >>> derived:SP_REFRESH_EXEC_BOARD
{board}
-- >>> seeds
MERGE INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE t
USING (
    SELECT 'DATABASE' AS SCOPE_TYPE, 'DBA_MAINT_DB' AS PATTERN, 'ALFA' AS COMPANY
) s
ON t.SCOPE_TYPE = s.SCOPE_TYPE AND t.PATTERN = s.PATTERN
WHEN NOT MATCHED THEN INSERT (SCOPE_TYPE, PATTERN, COMPANY)
     VALUES (s.SCOPE_TYPE, s.PATTERN, s.COMPANY);

-- >>> first fills
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 44 AS VERSION, 'UNKNOWN classification (#18): evidence-based company on both sides, COMPANY_SCOPE mapping lever (DATABASE rows supported, DBA_MAINT_DB seeded ALFA), exec board UNKNOWN scope' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 44);
"""
target = Path(os.environ.get("V044_OUT") or (MIG / "V044__unknown_classification.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
