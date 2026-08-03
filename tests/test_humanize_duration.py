"""rec26 — humanize_duration: compact H/M/S display for raw duration columns.

Display-only formatter (Styler callback); the underlying numeric column is
untouched so tables still sort by the real value and the CSV keeps the raw
number. These pin the reading contract across the unit and magnitude ranges.
"""
from __future__ import annotations

from app.logic.formulas import humanize_duration


def test_seconds_break_into_hms():
    assert humanize_duration(5400, "s") == "1h 30m"     # 1.5h
    assert humanize_duration(3600, "s") == "1h"          # exact hour drops 0m
    assert humanize_duration(90, "s") == "1m 30s"
    assert humanize_duration(60, "s") == "1m"            # exact minute drops 0s
    assert humanize_duration(45, "s") == "45s"


def test_sub_ten_seconds_keep_one_decimal():
    assert humanize_duration(2.5, "s") == "2.5s"
    assert humanize_duration(9.9, "s") == "9.9s"


def test_milliseconds_unit():
    assert humanize_duration(850, "ms") == "850ms"       # 0.85s -> ms
    assert humanize_duration(45000, "ms") == "45s"       # 45s
    assert humanize_duration(5_400_000, "ms") == "1h 30m"


def test_sub_second_seconds_render_ms():
    assert humanize_duration(0.85, "s") == "850ms"
    assert humanize_duration(0.05, "s") == "50ms"


def test_minutes_and_hours_units():
    assert humanize_duration(90, "min") == "1h 30m"      # 90 minutes
    assert humanize_duration(2.5, "h") == "2h 30m"


def test_zero_and_nan_and_negative():
    assert humanize_duration(0, "s") == "0s"
    assert humanize_duration(None, "s") == "—"
    assert humanize_duration(float("nan"), "s") == "—"
    assert humanize_duration("garbage", "s") == "—"
    assert humanize_duration(-90, "s") == "-1m 30s"      # signed for delta columns


def test_unknown_unit_falls_back_to_seconds():
    assert humanize_duration(90, "furlongs") == "1m 30s"
