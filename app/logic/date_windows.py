"""Account-time calendar presets for the global triage filter."""

from __future__ import annotations

from datetime import date

from app.config import (
    CURRENT_MONTH_WINDOW,
    CURRENT_YEAR_WINDOW,
    DAY_WINDOW_OPTIONS,
    DEFAULT_DAY_WINDOW,
    TRIAGE_WINDOW_OPTIONS,
)
from app.logic.formulas import account_today


class CalendarDayOffset(int):
    """Integer day offset that may legitimately be zero on a period's first day."""

    calendar_window = True


def normalize_window(value: object) -> int | str:
    """Return a valid triage-window selection, preserving calendar presets."""
    if value in TRIAGE_WINDOW_OPTIONS:
        return value
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_DAY_WINDOW
    return min(DAY_WINDOW_OPTIONS, key=lambda option: abs(option - number))


def resolve_window_days(value: object, today: date | None = None) -> int:
    """Resolve a selection to the app's day-offset convention.

    Day-grain builders use ``DAY >= DATEADD(day, -days, CURRENT_DATE())``;
    therefore Aug 3 MTD is offset 2 (Aug 1 through today), not a rolling 3.
    """
    selection = normalize_window(value)
    current = today or account_today()
    if selection == CURRENT_MONTH_WINDOW:
        return CalendarDayOffset((current - current.replace(day=1)).days)
    if selection == CURRENT_YEAR_WINDOW:
        return CalendarDayOffset((current - current.replace(month=1, day=1)).days)
    return int(selection)


def window_option_label(value: object) -> str:
    selection = normalize_window(value)
    if selection == CURRENT_MONTH_WINDOW:
        return "Current month"
    if selection == CURRENT_YEAR_WINDOW:
        return "Current year"
    return f"{int(selection)}d"


def _short_date(value: date) -> str:
    return f"{value:%b} {value.day}"


def window_scope_label(value: object, today: date | None = None) -> str:
    """Readable selected scope, including exact calendar boundaries."""
    selection = normalize_window(value)
    current = today or account_today()
    if selection == CURRENT_MONTH_WINDOW:
        start = current.replace(day=1)
        return f"Current month ({_short_date(start)} - {_short_date(current)})"
    if selection == CURRENT_YEAR_WINDOW:
        start = current.replace(month=1, day=1)
        return f"Current year ({_short_date(start)} - {_short_date(current)})"
    return f"Last {int(selection)} days"
