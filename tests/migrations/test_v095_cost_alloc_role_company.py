"""V095 locks: evidence-based ROLE company classification in cost allocation.

SP_LOAD_MARTS_V27's cost-allocation arm stamped MART_COST_ALLOCATION_DAILY's ROLE
dimension COMPANY with an inline `CASE ... THEN 'Trexis' ELSE 'ALFA' END` that defaulted
every non-TRXS role to ALFA and never emitted 'UNKNOWN' — bypassing the V044 evidence-
based classification law that its USER / DATABASE / SCHEMA siblings already honor via
COMPANY_FOR_USER / COMPANY_FOR_DATABASE. V095 introduces a COMPANY_FOR_ROLE(R) scalar
UDF (same evidence as COMPANY_FOR_USER's role predicates and app.companies.role_clause)
and re-derives the proc so the ROLE arm calls it. Byte-locked to outputs/gen_v095.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V095 = (_MIG / "V095__cost_alloc_role_company.sql").read_text(encoding="utf-8")
_V082 = (_MIG / "V082__query_family_company_regrain.sql").read_text(encoding="utf-8")


def test_v095_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v095.py")],
        env={**os.environ, "V095_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V095, (
        "V095 drifted from its forward-generation — edit outputs/gen_v095.py, "
        "not the .sql, then regenerate."
    )


def test_v095_is_one_guarded_udf_plus_proc_redefinition():
    assert "EXCEPTION (-20095" in _V095 and "IF (v < 94) THEN" in _V095
    assert "SELECT 95 AS VERSION" in _V095 and "WHERE VERSION = 95)" in _V095
    assert _V095.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in _V095
    assert _V095.count(
        "CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(R VARCHAR)"
    ) == 1
    # UDF + proc only — no table/view/task/schema change, no data rewrite
    assert "CREATE TABLE" not in _V095 and "ALTER TABLE" not in _V095
    assert "CREATE OR REPLACE VIEW" not in _V095 and "CREATE TASK" not in _V095
    assert "INSERT OVERWRITE" not in _V095


def test_v095_role_dim_routes_through_udf_not_inline_case():
    # the fix: the ROLE arm now calls COMPANY_FOR_ROLE, like its siblings
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in _V095
    # the silent ELSE 'ALFA' default is gone
    assert "ELSE 'ALFA'" not in _V095
    assert "CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END" not in _V095
    # V082 (the base) still carries the bug, confirming V095 supersedes it
    assert "CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END" in _V082


def test_v095_udf_uses_the_v044_role_evidence_with_unknown_residual():
    # the UDF classifies a role NAME by the SAME evidence as COMPANY_FOR_USER's role
    # predicates (V044) and app.companies.role_clause: %TRXS% -> Trexis, %ALFA% or the
    # two DBA roles -> ALFA, else UNKNOWN.
    udf = _V095.split(
        "CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(R VARCHAR)", 1
    )[1].split("$$;", 1)[0]
    assert "LIKE '%TRXS%' THEN 'Trexis'" in udf
    assert "LIKE '%ALFA%'" in udf
    assert "IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS') THEN 'ALFA'" in udf
    assert "ELSE 'UNKNOWN'" in udf
    assert "COALESCE(R, '')" in udf   # NULL role stays classifiable (residual UNKNOWN)


def test_v095_sibling_dims_are_byte_identical_to_v082():
    # USER / DATABASE / SCHEMA arms are unchanged; only the ROLE arm was re-pointed
    assert _V095.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,") == 1
    assert _V095.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),") == 2
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t" in _V095


def test_v095_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V095):
        sqlglot.parse(statement, dialect="snowflake")
