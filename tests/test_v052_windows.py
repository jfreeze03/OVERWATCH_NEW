"""Locks for V052 — long-history window filter (owner ask 2026-07-27).

180/365 added to the window filter. The exec-board loader (effective proc) must
compute every configured window, or a selected window with no board row falls
through to a 13-month live scan. Live ACCOUNT_USAGE scans stay capped at 90;
the one owner-named live exception is Cortex user costs.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V52 = (_ROOT / "snowflake" / "migrations" / "V052__exec_board_windows_180_365.sql").read_text(encoding="utf-8")


def test_v052_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v052.py")],
                       env={**os.environ, "V052_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V52, (
        "V052 drifted from its forward-generation — edit outputs/gen_v052.py, "
        "regenerate, never hand-edit the migration.")


def test_v052_guard_version_house_rules():
    assert "EXCEPTION (-20052" in _V52 and "RAISE not_ready;" in _V52
    assert "IF (v < 51) THEN" in _V52 and "SELECT 52 AS VERSION" in _V52


def test_v052_board_windows_equal_the_config_tuple():
    """The effective exec-board loader must build exactly DAY_WINDOW_OPTIONS —
    this is the lock that couples the window filter to the KPI board."""
    from app.config import DAY_WINDOW_OPTIONS
    proc = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_REFRESH_EXEC_BOARD\(.*?\n\$\$;\n",
        _V52, re.S).group(0)
    windows_cte = proc.split("windows AS (", 1)[1].split(")", 1)[0]
    built = tuple(int(n) for n in re.findall(r"SELECT (\d+)", windows_cte))
    assert built == tuple(DAY_WINDOW_OPTIONS), (built, tuple(DAY_WINDOW_OPTIONS))
    assert 180 in built and 365 in built


def test_v052_is_a_proc_swap_only():
    assert _V52.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V52 and "CREATE TASK" not in _V52
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();" in _V52   # populate new windows


def test_v052_live_cap_unchanged_but_mart_and_cortex_honor_long_window():
    from app.config import MAX_LIVE_WINDOW_DAYS, MAX_MART_WINDOW_DAYS
    assert MAX_LIVE_WINDOW_DAYS == 90 and MAX_MART_WINDOW_DAYS == 365
    from app.data import chargeback_sql, cortex_sql, cost_sql
    # mart-history + the owner-named Cortex live exception honor 365...
    for sql in (cortex_sql.cortex_code_user_rollup(9999, "ALFA"),
                cortex_sql.cortex_code_daily(9999, "ALFA"),
                cost_sql.storage_by_database(9999, "ALFA"),
                chargeback_sql.department_window_credits(9999, "ALFA")):
        assert "-365," in sql.replace(" ", ""), sql[:60]
    # ...while a live ACCOUNT_USAGE scan stays capped at 90
    assert "-90," in cost_sql.warehouse_daily_credits(9999, "ALFA").replace(" ", "")


def test_v052_filter_strip_discloses_the_cap():
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "MAX_LIVE_WINDOW_DAYS" in main
    assert "live Operations" in main and "and Security scans cap at" in main


def test_v052_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V52):
        sqlglot.parse(stmt, dialect="snowflake")
