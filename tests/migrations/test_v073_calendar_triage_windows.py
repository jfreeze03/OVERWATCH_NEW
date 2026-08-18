"""V073 locks: dynamic MTD/YTD rows for the first-paint executive board."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V073 = (_MIG / "V073__calendar_triage_windows.sql").read_text(encoding="utf-8")


def _proc(text: str) -> str:
    return re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_REFRESH_EXEC_BOARD\(.*?\n\$\$;\n",
        text,
        re.S,
    ).group(0)


def test_v073_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v073.py")],
        env={**os.environ, "V073_OUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V073


def test_v073_is_one_guarded_proc_swap_and_board_reload():
    assert "EXCEPTION (-20073" in _V073 and "IF (v < 72) THEN" in _V073
    assert "SELECT 73 AS VERSION" in _V073 and "WHERE VERSION = 73)" in _V073
    assert _V073.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V073 and "CREATE TASK" not in _V073
    assert _V073.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1


def test_v073_windows_are_calendar_offsets_and_deduplicated():
    board = _proc(_V073)
    assert "SELECT DISTINCT WINDOW_DAYS" in board
    assert "DATEDIFF('day', DATE_TRUNC('month', CURRENT_DATE()), CURRENT_DATE())" in board
    assert "DATEDIFF('day', DATE_TRUNC('year', CURRENT_DATE()), CURRENT_DATE())" in board
    for fixed in (7, 14, 30, 60, 90, 180, 365):
        assert re.search(rf"SELECT {fixed}(?: AS WINDOW_DAYS)?(?:\s|$)", board)


def test_v073_changes_only_the_latest_windows_cte():
    previous = _proc((_MIG / "V069__exec_board_serverless_ai_drivers.sql").read_text(encoding="utf-8"))
    current = _proc(_V073)
    old = previous.split("    windows AS (", 1)[1].split("    -- Aggregate", 1)[0]
    new = current.split("    windows AS (", 1)[1].split("    -- Aggregate", 1)[0]
    assert previous.replace(old, new) == current
    assert "'COST_DRIVER_SVC', 'CREDITS'" in current
    assert "SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;" in current


def test_v073_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements

    for statement in _plain_statements(_V073):
        sqlglot.parse(statement, dialect="snowflake")
