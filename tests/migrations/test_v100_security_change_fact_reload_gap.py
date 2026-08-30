"""V100 locks: security change-fact reload no longer drops the oldest day's earliest hours.

SP_LOAD_SECURITY_FACTS(3) reloaded FACT_SECURITY_CHANGE from OW_QH_EXTRACT but deleted the
full calendar window (DAY >= -d days) while the extract retains only a rolling ~72h — silently,
permanently dropping [midnight(D-3), now-72h). V100 re-derives the proc so the d<=3 branch
deletes only the window the extract can refill (EVENT_TS >= MIN(extract.START_TIME)); the d>3
full-backfill branch keeps the whole-window delete. Byte-locked to outputs/gen_v100.py.
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
_V100 = (_MIG / "V100__security_change_fact_reload_gap.sql").read_text(encoding="utf-8")
_V075 = (_MIG / "V075__security_operating_model.sql").read_text(encoding="utf-8")


def test_v100_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v100.py")],
        env={**os.environ, "V100_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V100, (
        "V100 drifted from its forward-generation — edit outputs/gen_v100.py, "
        "not the .sql, then regenerate."
    )


def test_v100_is_one_guarded_proc_redefinition():
    assert "EXCEPTION (-20100" in _V100 and "IF (v < 99) THEN" in _V100
    assert "SELECT 100 AS VERSION" in _V100 and "WHERE VERSION = 100)" in _V100
    assert _V100.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS" in _V100
    assert "CREATE TABLE" not in _V100 and "ALTER TABLE" not in _V100
    assert "CREATE OR REPLACE VIEW" not in _V100 and "CREATE TASK" not in _V100


def test_v100_d_le_3_deletes_only_what_the_extract_can_refill():
    # the fix: the d<=3 change reload deletes only the extract's coverage window
    assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
            "         WHERE EVENT_TS >= (SELECT MIN(START_TIME) "
            "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);") in _V100
    # the shared full-calendar-window delete no longer sits right before the IF (it was
    # the bug: deleting more than the 72h extract could refill)
    assert "     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());\n\n    IF (d <= 3) THEN" not in _V100
    # the d>3 full-backfill branch (reads QUERY_HISTORY directly) keeps the whole-window delete
    assert ("    ELSE\n"
            "        -- Full backfill" in _V100)
    # V075 (base) still has the shared pre-IF delete, confirming V100 supersedes it
    assert "     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());\n\n    IF (d <= 3) THEN" in _V075


def test_v100_preserves_both_load_branches_and_the_login_fact():
    assert _V100.count("WITH raw AS (") == 2                       # both change-fact branches
    assert "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT" in _V100    # d<=3 source
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in _V100   # d>3 source
    assert "FACT_SECURITY_LOGIN_DAILY" in _V100                    # login fact untouched


def test_v100_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V100):
        sqlglot.parse(statement, dialect="snowflake")
