"""V082 locks: MART_QUERY_FAMILY_DAILY regrained to per-company grain (DS #3).

The query-family loader arm now stamps COMPANY = COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)
and groups (DAY, QUERY_HASH, COMPANY) with a per-company top-2000; the mart gains a
COMPANY column (cleared once for the grain change) and the app scopes on the real
company instead of the ANY_VALUE(DATABASE_NAME) heuristic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.data import mart27_sql
from app.data.workbench_sql import workload_portfolio

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V082 = (_MIG / "V082__query_family_company_regrain.sql").read_text(encoding="utf-8")


# --- the migration file ----------------------------------------------------

def test_v082_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v082.py")],
        env={**os.environ, "V082_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V082, (
        "V082 drifted from its forward-generation — edit outputs/gen_v082.py, "
        "not the .sql, then regenerate."
    )


def test_v082_is_guarded_and_re_derives_one_proc():
    assert "EXCEPTION (-20082" in _V082 and "IF (v < 81) THEN" in _V082
    assert "SELECT 82 AS VERSION" in _V082 and "WHERE VERSION = 82)" in _V082
    assert _V082.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _V082.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27") == 1
    assert "CREATE OR REPLACE VIEW" not in _V082
    # additive column + one-time grain reset, guarded so a re-apply can't wipe a backfill
    assert "ADD COLUMN IF NOT EXISTS COMPANY VARCHAR(40)" in _V082
    assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY\n"
            "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 82);") in _V082


def test_v082_qfam_arm_is_regrained_per_company():
    # COMPANY is derived per-row in an INNER subquery; the OUTER groups on the plain
    # column -- never GROUP BY the correlated UDF (the V029 mart_load_failed shape).
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY" in _V082
    assert "GROUP BY DAY, QUERY_HASH, COMPANY" in _V082   # outer groups on the plain column
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000" in _V082
    assert "ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY" in _V082
    # the old account-grain qfam QUALIFY/ON are gone
    assert "PARTITION BY DAY ORDER BY TOTAL_EXEC_SEC" not in _V082
    assert "ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH\n" not in _V082


def test_v082_other_loader_arms_survive_the_extraction():
    # the whole proc was re-emitted, not just the qfam arm — spot-check siblings
    for token in ("loaded := loaded || 'wh_eff ';", "loaded := loaded || 'qfam ';",
                  "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t",
                  "COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL"):  # wh_eff UPDATE
        assert token in _V082, token


def test_v082_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V082):
        sqlglot.parse(statement, dialect="snowflake")


# --- the app rewire --------------------------------------------------------

def test_workload_portfolio_scopes_families_on_company():
    sql = workload_portfolio(30, "ALFA", 200)
    assert "SELECT DISTINCT p.QUERY_HASH, p.COMPANY" in sql
    assert "ON s.QUERY_HASH = f.QUERY_HASH AND s.COMPANY = f.COMPANY" in sql
    # the lossy database-name join is gone
    assert "s.DATABASE_NAME = f.DATABASE_NAME" not in sql


def test_family_builders_scope_on_exact_company_not_database_heuristic():
    ch = mart27_sql.family_compile_heavy(30, "ALFA")
    assert "f.COMPANY = 'ALFA'" in ch
    rp = mart27_sql.family_repeat_fingerprints(30, "ALFA")
    assert "f.COMPANY = 'ALFA'" in rp
    # ALL scope adds no company predicate (empty arm), still bounded by DAY
    ch_all = mart27_sql.family_compile_heavy(30, "ALL")
    assert "f.COMPANY" not in ch_all and "f.DAY >=" in ch_all


def test_query_families_surfaces_the_company_column():
    sql = mart27_sql.query_families(2, 10)
    assert "QUERY_HASH, COMPANY, SAMPLE_TEXT" in sql
