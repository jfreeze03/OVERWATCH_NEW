"""Locks for V050 one-pass loader + read/write arms (Tranche B, 2026-07-27).

V049 scanned QUERY_ATTRIBUTION_HISTORY twice and flattened ACCESS_HISTORY four
times per run; V050 stages each once and labels every split share by role —
QUERY_COMPUTE_WRITE (production) vs QUERY_COMPUTE_READ (consumption) — with
credits/N additivity unchanged.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_V50 = (_ROOT / "snowflake" / "migrations" / "V050__one_pass_read_write_arms.sql").read_text(encoding="utf-8")
_PROC = re.search(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_OBJECT_COST\(.*?\n\$\$;\n",
    _V50, re.S).group(0)


def test_v050_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v050.py")],
                       env={**os.environ, "V050_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V50, (
        "V050 drifted from its forward-generation — edit outputs/gen_v050.py, "
        "regenerate, never hand-edit the migration.")


def test_v050_guard_version_house_rules():
    assert "EXCEPTION (-20050" in _V50 and "RAISE not_ready;" in _V50
    assert "IF (v < 49) THEN" in _V50 and "SELECT 50 AS VERSION" in _V50


def test_v050_is_actually_one_pass():
    """The whole point: each expensive source is read once per run."""
    # one FROM on the QAH view (the stage build); the other mention is prose
    assert _PROC.count("FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY") == 1
    # one flatten per ACCESS_HISTORY array — V049 had four
    assert _PROC.count("LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED)") == 1
    assert _PROC.count("LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED)") == 1
    # both attribution inserts read the stages, not the sources
    assert _PROC.count("FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE") == 2
    assert "CREATE OR REPLACE TEMPORARY TABLE" in _PROC   # session-scoped, no teardown


def test_v050_read_write_arms_partition_the_split():
    assert "IFF(d.IS_WRITE = 1, 'QUERY_COMPUTE_WRITE', 'QUERY_COMPUTE_READ')" in _PROC
    assert "MAX(IS_WRITE) AS IS_WRITE" in _PROC            # write wins on collapse
    assert "SUM(qa.CREDITS / c.N)" in _PROC                # equal split unchanged
    assert "'QUERY_COMPUTE'," not in _PROC                 # the unlabeled arm is gone


def test_v050_residual_anti_joins_the_same_stage():
    """Split and residual must partition the staged credits exactly — the
    V049 NULL-name vanishing act (obj_q counted a query attributed while the
    split had no row for it) cannot recur when both read one stage."""
    assert "LEFT JOIN (SELECT DISTINCT QUERY_ID FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE)" in _PROC
    assert "'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)" in _PROC
    # no separate, differently-filtered obj_q CTE remains
    assert "obj_q AS (" not in _PROC


def test_v050_keeps_prior_invariants():
    assert "CREDITS_USED_QUERY_ACCELERATION" in _PROC      # QAS stays in measured compute
    assert "COALESCE(DATABASE_NAME, 'UNKNOWN')" in _PROC   # null-safe FQN (V048 lesson)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);" in _V50
    assert _V50.count("CREATE OR REPLACE PROCEDURE") == 1  # proc swap only
    assert "CREATE TASK" not in _V50 and "CREATE TRANSIENT TABLE" not in _V50


def test_v050_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V50):
        sqlglot.parse(stmt, dialect="snowflake")


def test_readers_treat_all_query_arms_alike():
    """Pre-V050 days keep legacy 'QUERY_COMPUTE' rows; readers must bucket the
    legacy arm and both new arms as query credits or windows spanning the
    reload boundary under-count."""
    from app.data import cost_sql
    top = cost_sql.object_cost_top(30, "ALFA")
    for arm in ("'QUERY_COMPUTE'", "'QUERY_COMPUTE_READ'", "'QUERY_COMPUTE_WRITE'"):
        assert arm in top, f"object_cost_top: {arm} missing from the query bucket"
    assert "QUERY_COMPUTE_RESIDUAL" in top
