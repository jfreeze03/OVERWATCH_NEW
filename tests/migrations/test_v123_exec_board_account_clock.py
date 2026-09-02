"""V123: exec board Current-month/Current-year presets on the account clock.

The reader (mart_sql.exec_board) and the loader (SP_REFRESH_EXEC_BOARD) must BOTH key
the calendar-preset WINDOW_DAYS off the account-tz date, or they disagree and the board
blanks / drifts a day from the account_today-anchored MTD pace KPI each evening.
"""

from __future__ import annotations

from pathlib import Path

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW
from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake/migrations/V123__exec_board_account_clock.sql").read_text(encoding="utf-8")
_ACCT = "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE"


def test_v123_guarded_versioned_and_restamps():
    assert "EXCEPTION (-20123" in _MIG
    assert "IF (v < 122)" in _MIG
    assert "SELECT 123 AS VERSION" in _MIG
    assert "WHERE VERSION = 123" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD()" in _MIG
    # re-stamps the board immediately so the fix takes effect before the next hourly task
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();" in _MIG


def test_v123_proc_body_is_account_clock_not_current_date():
    start = _MIG.index("CREATE OR REPLACE PROCEDURE")
    body = _MIG[start:_MIG.index("\n$$;", start)]
    assert "CURRENT_DATE()" not in body                       # no session/UTC date in the proc
    assert body.count(_ACCT) == 12                            # every former CURRENT_DATE() anchor


def test_v123_floor_tracks_the_tip():
    v = (_ROOT / "snowflake/validate.sql").read_text(encoding="utf-8")
    assert "V001..V124 applied" in v
    assert "BETWEEN 1 AND 124) = 124" in v


def test_exec_board_reader_calendar_presets_use_account_clock():
    for window in (CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW):
        sql = mart_sql.exec_board("ALL", 30, window)
        assert _ACCT in sql, window
        assert "DATE_TRUNC('month', CURRENT_DATE())" not in sql
        assert "DATE_TRUNC('year', CURRENT_DATE())" not in sql
    # a trailing (non-calendar) window keys off a fixed number, unaffected by the clock
    assert "WINDOW_DAYS = 30" in mart_sql.exec_board("ALL", 30, None)
