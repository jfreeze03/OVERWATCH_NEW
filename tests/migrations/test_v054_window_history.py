"""Locks for V054 — the exec board's long windows actually read long history.

Codex r29 #1 (CONFIRMED regression): V052 added 180/365 to the exec-board
`windows` CTE but left the three source CTEs (wh_daily/qh_daily/tk_daily) capped
at 90 days, so the 180/365 pills computed over only 90 days — identical to the
90-day row. V054 widens the source horizons to 365 and (companion, r29 #20)
raises the SP_PURGE_FACTS daily-fact retention floor 180 -> 365 so retained
history always covers the max advertised window.

The last test here is a STANDING CI invariant (r29 #20): whatever the current
effective SP_REFRESH_EXEC_BOARD and SP_PURGE_FACTS are, every source horizon and
the retention floor must be >= max(DAY_WINDOW_OPTIONS). This is the check that
would have caught #1 — the V052 test only compared window *names*.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V54 = (_MIG / "V054__exec_board_window_history.sql").read_text(encoding="utf-8")


def _latest_proc(name: str) -> str:
    """The effective definition of a proc = its body in the highest-numbered
    migration that (re)defines it."""
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    for mig in sorted(_MIG.glob("V[0-9]*.sql"), reverse=True):
        found = pat.findall(mig.read_text(encoding="utf-8"))
        if found:
            return found[-1]
    raise AssertionError(f"no definition of {name} found")


def test_v054_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v054.py")],
                       env={**os.environ, "V054_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V54, (
        "V054 drifted from its forward-generation — edit outputs/gen_v054.py, "
        "regenerate, never hand-edit the migration.")


def test_v054_guard_version_house_rules():
    assert "EXCEPTION (-20054" in _V54 and "RAISE not_ready;" in _V54
    assert "IF (v < 53) THEN" in _V54 and "SELECT 54 AS VERSION" in _V54


def test_v054_ships_two_derived_procs_no_new_objects():
    assert _V54.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "-- >>> derived:SP_REFRESH_EXEC_BOARD" in _V54
    assert "-- >>> derived:SP_PURGE_FACTS" in _V54
    assert "CREATE TABLE" not in _V54 and "CREATE TASK" not in _V54
    # repopulate the board immediately so the fixed windows carry real data
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();" in _V54


def test_v054_board_source_horizons_widened_to_365():
    """The bug: the three source CTEs capped at 90. V054 widens all three to
    365 while the window join keeps -w.WINDOW_DAYS (unchanged)."""
    board = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_REFRESH_EXEC_BOARD\(.*?\n\$\$;\n",
        _V54, re.S).group(0)
    src_horizons = re.findall(r"DATEADD\('day', -(\d+), CURRENT_DATE\(\)\)", board)
    assert src_horizons == ["365", "365", "365"], src_horizons          # all three widened
    assert "DATEADD('day', -90, CURRENT_DATE())" not in board            # no 90-cap remains
    assert "-w.WINDOW_DAYS" in board                                     # window join intact
    # the windows CTE the V052 fix added is carried forward verbatim
    assert "UNION ALL SELECT 180 UNION ALL SELECT 365" in board


def test_v054_retention_floor_raised_to_365():
    purge = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_PURGE_FACTS\(.*?\n\$\$;\n",
        _V54, re.S).group(0)
    assert "daily_days := GREATEST(daily_days, 365);" in purge
    assert "daily_days := GREATEST(daily_days, 180);" not in purge


def test_v054_board_is_v052_plus_only_horizon_edits():
    """Derivation-law diff check: V054's board == V052's board + exactly the
    three -90 -> -365 edits, nothing else."""
    v052 = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_REFRESH_EXEC_BOARD\(.*?\n\$\$;\n",
        (_MIG / "V052__exec_board_windows_180_365.sql").read_text(encoding="utf-8"), re.S).group(0)
    v054 = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_REFRESH_EXEC_BOARD\(.*?\n\$\$;\n",
        _V54, re.S).group(0)
    assert v052.replace("-90, CURRENT_DATE()", "-365, CURRENT_DATE()") == v054


# --- standing invariant (r29 #20) ---------------------------------------------

def test_invariant_effective_board_reads_at_least_the_max_window():
    """Whatever the current effective exec-board loader is, every source-fact
    horizon must cover the widest advertised window — else that window renders a
    silently-truncated pill (the exact V052 defect). Window joins use
    -w.WINDOW_DAYS (a column, not an int literal) and are correctly ignored."""
    from app.config import DAY_WINDOW_OPTIONS
    max_window = max(DAY_WINDOW_OPTIONS)
    board = _latest_proc("SP_REFRESH_EXEC_BOARD")
    horizons = [int(n) for n in re.findall(r"DATEADD\('day', -(\d+), CURRENT_DATE\(\)\)", board)]
    assert horizons, "no integer source horizons found — regex/proc shape changed"
    assert min(horizons) >= max_window, (
        f"exec-board source horizon {min(horizons)}d < max advertised window "
        f"{max_window}d — the long windows would read truncated history (Codex r29 #1).")


def test_invariant_effective_retention_covers_the_max_window():
    """Reading 365 days only helps if 365 days are retained. The daily-fact
    retention floor must be >= the widest advertised window so a low
    FACT_RETENTION_DAYS_DAILY setting can't re-truncate the long windows."""
    from app.config import DAY_WINDOW_OPTIONS
    max_window = max(DAY_WINDOW_OPTIONS)
    purge = _latest_proc("SP_PURGE_FACTS")
    floor = int(re.search(r"daily_days := GREATEST\(daily_days, (\d+)\)", purge).group(1))
    assert floor >= max_window, (
        f"daily-fact retention floor {floor}d < max advertised window {max_window}d — "
        f"a low retention setting could leave the long windows short (Codex r29 #20).")


def test_v054_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V54):
        sqlglot.parse(stmt, dialect="snowflake")
