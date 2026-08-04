"""Locks for v4.138 calendar-aware global triage windows."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.config import (
    CURRENT_MONTH_WINDOW,
    CURRENT_YEAR_WINDOW,
    TRIAGE_WINDOW_OPTIONS,
    clamp_days,
)
from app.logic.date_windows import (
    CalendarDayOffset,
    normalize_window,
    resolve_window_days,
    window_option_label,
    window_scope_label,
)

_ROOT = Path(__file__).resolve().parents[1]


def test_calendar_windows_resolve_from_account_calendar_boundaries():
    today = date(2026, 8, 3)
    assert resolve_window_days(CURRENT_MONTH_WINDOW, today) == 2
    assert resolve_window_days(CURRENT_YEAR_WINDOW, today) == 214
    assert window_scope_label(CURRENT_MONTH_WINDOW, today) == "Current month (Aug 1 - Aug 3)"
    assert window_scope_label(CURRENT_YEAR_WINDOW, today) == "Current year (Jan 1 - Aug 3)"


def test_calendar_day_one_stays_today_only_and_leap_ytd_reaches_january_first():
    month_day_one = resolve_window_days(CURRENT_MONTH_WINDOW, date(2026, 8, 1))
    year_day_one = resolve_window_days(CURRENT_YEAR_WINDOW, date(2026, 1, 1))
    assert isinstance(month_day_one, CalendarDayOffset)
    assert clamp_days(month_day_one) == 0
    assert clamp_days(year_day_one) == 0
    assert clamp_days(0) == 1  # invalid legacy integer still keeps the old safety floor
    assert resolve_window_days(CURRENT_YEAR_WINDOW, date(2024, 12, 31)) == 365


def test_options_and_legacy_integer_views_normalize_without_losing_calendar_mode():
    assert TRIAGE_WINDOW_OPTIONS[-2:] == (CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW)
    assert normalize_window("CURRENT_MONTH") == CURRENT_MONTH_WINDOW
    assert normalize_window(61) == 60
    assert normalize_window("bad") == 7
    assert window_option_label(30) == "30d"
    assert window_option_label(CURRENT_YEAR_WINDOW) == "Current year"


def test_saved_view_window_takes_precedence_over_legacy_resolved_days(monkeypatch):
    from app.core import state

    fake_st = SimpleNamespace(session_state={})
    monkeypatch.setattr(state, "st", fake_st)
    state.apply_filters(company="Trexis", days=214, window=CURRENT_YEAR_WINDOW)
    assert fake_st.session_state["flt_company"] == "Trexis"
    assert fake_st.session_state["flt_days"] == CURRENT_YEAR_WINDOW

    state.apply_filters(days=61)
    assert fake_st.session_state["flt_days"] == 60


def test_shell_and_pages_render_calendar_selection_instead_of_a_rolling_alias():
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"Date range"' in main
    assert "TRIAGE_WINDOW_OPTIONS" in main and "format_func=window_option_label" in main
    assert "resolve_window_days(_window)" in main

    for relative in (
        "app/ui/pages/overview.py",
        "app/ui/pages/control_room.py",
        "app/ui/pages/cost.py",
        "app/ui/pages/operations.py",
        "app/ui/pages/security.py",
    ):
        source = (_ROOT / relative).read_text(encoding="utf-8")
        assert "window_label" in source, relative


def test_filter_payload_carries_token_resolved_days_and_label():
    source = (_ROOT / "app" / "core" / "state.py").read_text(encoding="utf-8")
    body = source.split("def filters()", 1)[1].split("\ndef ", 1)[0]
    assert '"days": resolve_window_days(window)' in body
    assert '"window": window' in body
    assert '"window_label": window_scope_label(window)' in body


def test_exec_board_selects_calendar_row_with_snowflake_account_date():
    from app.data import mart_sql

    mtd = mart_sql.exec_board("ALL", 2, CURRENT_MONTH_WINDOW)
    ytd = mart_sql.exec_board("Trexis", 214, CURRENT_YEAR_WINDOW)
    assert "WINDOW_DAYS = DATEDIFF('day', DATE_TRUNC('month', CURRENT_DATE())" in mtd
    assert "WINDOW_DAYS = DATEDIFF('day', DATE_TRUNC('year', CURRENT_DATE())" in ytd
    assert "COMPANY = 'Trexis'" in ytd
    assert "WINDOW_DAYS = 30" in mart_sql.exec_board("ALL", 30)
