"""rec27 — humanize_age: compact "how stale" for a past timestamp.

Display-only companion to a real timestamp column (which stays for sort + tz).
`now` is caller-supplied (account-naive) so the function is pure/deterministic.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.logic.formulas import humanize_age, humanize_minutes_ago

NOW = dt.datetime(2026, 8, 3, 12, 0, 0)   # naive account time reference


def test_humanize_minutes_ago_is_day_aware():
    # Cost3 opener: the primary case is a multi-day return, which must read "Nd
    # ago" (humanize_duration caps at hours -> "72h ago").
    assert humanize_minutes_ago(0) == "just now"
    assert humanize_minutes_ago(5) == "5m ago"
    assert humanize_minutes_ago(90) == "2h ago"        # rounds
    assert humanize_minutes_ago(60) == "1h ago"
    assert humanize_minutes_ago(2880) == "2d ago"      # 2 days, not "48h ago"
    assert humanize_minutes_ago(None) == "just now"    # safe_float -> 0


def test_buckets():
    assert humanize_age(NOW, NOW) == "just now"
    assert humanize_age(NOW - dt.timedelta(seconds=20), NOW) == "just now"
    assert humanize_age(NOW - dt.timedelta(minutes=5), NOW) == "5m ago"
    assert humanize_age(NOW - dt.timedelta(hours=3), NOW) == "3h ago"
    assert humanize_age(NOW - dt.timedelta(days=2), NOW) == "2d ago"


def test_future_or_skew_reads_just_now():
    assert humanize_age(NOW + dt.timedelta(hours=1), NOW) == "just now"


def test_nan_and_missing_now_render_em_dash():
    assert humanize_age(None, NOW) == "—"
    assert humanize_age(NOW, None) == "—"
    assert humanize_age(pd.NaT, NOW) == "—"
    assert humanize_age("garbage", NOW) == "—"


def test_accepts_strings_and_timestamps():
    assert humanize_age("2026-08-03 09:00:00", NOW) == "3h ago"
    assert humanize_age(pd.Timestamp("2026-08-01 12:00:00"), NOW) == "2d ago"
