"""Account-time calendar presets for the global triage filter."""

from __future__ import annotations

from datetime import date, timedelta

from app.config import (
    CURRENT_MONTH_WINDOW,
    CURRENT_YEAR_WINDOW,
    DAY_WINDOW_OPTIONS,
    DEFAULT_DAY_WINDOW,
    LAST_MONTH_WINDOW,
    TRIAGE_WINDOW_OPTIONS,
)
from app.logic.formulas import account_today


class CalendarDayOffset(int):
    """Integer day offset that may legitimately be zero on a period's first day."""

    calendar_window = True


def _last_month_bounds(current: date) -> tuple[date, date]:
    """(first day of last month, first day of this month) — start inclusive, end EXCLUSIVE.

    Used by LAST_MONTH, the one bounded calendar window: it ends before today, so it
    cannot be a trailing day-offset. The exclusive end is the first of the current
    month, so `col >= start AND col < end` covers exactly the previous calendar month
    for both DATE and TIMESTAMP columns.
    """
    first_this = current.replace(day=1)
    if first_this.month == 1:
        first_last = first_this.replace(year=first_this.year - 1, month=12)
    else:
        first_last = first_this.replace(month=first_this.month - 1)
    return first_last, first_this


def window_bounds(value: object, today: date | None = None) -> tuple[date, date] | None:
    """Explicit (start, end_exclusive) dates for a calendar-anchored window, else None.

    Returned for the three calendar presets — LAST_MONTH (a complete previous month) and
    the period-to-date presets CURRENT_MONTH / CURRENT_YEAR (first-of-period through today,
    inclusive) — all computed on the ACCOUNT clock (account_today). Trailing windows
    (7/30/...) end at "now" and return None; callers use the `resolve_window_days` offset.

    A builder that honors these emits `col >= start AND col < end` instead of the
    `DATEADD('day', -days, CURRENT_DATE())` trailing predicate. r30 #2: the period-to-date
    presets USED to return None and fall back to that trailing predicate — but its
    session-tz CURRENT_DATE() anchor disagreed with the account-clock day OFFSET
    (resolve_window_days uses account_today), so MTD/YTD drifted a day at the evening
    boundary (e.g. "Current month" dropped the 1st). Returning explicit account-clock
    bounds here anchors both the window and its label to the same clock, like LAST_MONTH.
    """
    selection = normalize_window(value)
    current = today or account_today()
    if selection == LAST_MONTH_WINDOW:
        return _last_month_bounds(current)
    # period-to-date: first of the period (inclusive) .. tomorrow (exclusive, so today is in)
    if selection == CURRENT_MONTH_WINDOW:
        return current.replace(day=1), current + timedelta(days=1)
    if selection == CURRENT_YEAR_WINDOW:
        return current.replace(month=1, day=1), current + timedelta(days=1)
    return None


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
    if selection == LAST_MONTH_WINDOW:
        # LAST_MONTH is bounded (see window_bounds); this offset is the SPAN of last month
        # (28-31), correct for /day normalization and used as a trailing fallback only on
        # surfaces that do not yet honor the bounds. Builders that support Last month read
        # window_bounds() for the exact range instead of this offset.
        start, end = _last_month_bounds(current)
        return CalendarDayOffset((end - start).days)
    if selection == CURRENT_YEAR_WINDOW:
        return CalendarDayOffset((current - current.replace(month=1, day=1)).days)
    return int(selection)


def window_option_label(value: object) -> str:
    selection = normalize_window(value)
    if selection == CURRENT_MONTH_WINDOW:
        return "Current month"
    if selection == LAST_MONTH_WINDOW:
        return "Last month"
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
    if selection == LAST_MONTH_WINDOW:
        start, end = _last_month_bounds(current)
        return f"Last month ({_short_date(start)} - {_short_date(end - timedelta(days=1))})"
    if selection == CURRENT_YEAR_WINDOW:
        start = current.replace(month=1, day=1)
        return f"Current year ({_short_date(start)} - {_short_date(current)})"
    return f"Last {int(selection)} days"
