"""Timezone standard guard (see the timezone-consistency audit, 2026-09-21).

OVERWATCH runs in the Snowflake ACCOUNT default TIMEZONE, which IS America/Chicago
(verified 2026-09-21: SHOW PARAMETERS ... IN ACCOUNT -> value=America/Chicago,
level=ACCOUNT). The deployed Streamlit-in-Snowflake app CANNOT ALTER SESSION, so
that account default is the ONLY lever keeping the app's clock Central: every
session-tz read renders Central only because the account is Central.

These locks stop a future edit from silently re-anchoring the app's canonical
timezone, and keep the Central-PINNED SQL helpers (the account-correct escape hatch
new builders must use) intact. They do NOT change app behavior — the app is already
consistent; this is the "new builders pin Central" standard, enforced.
"""

from __future__ import annotations

import inspect
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_account_timezone_is_america_chicago():
    # the app's canonical zone. If this ever changes, it must be a conscious decision
    # (the whole app clock + account_today() + the Central-pinned SQL helpers key off it).
    from app.logic.formulas import ACCOUNT_TIMEZONE
    assert ACCOUNT_TIMEZONE == "America/Chicago"


def test_account_date_helpers_pin_central_in_sql():
    # the account-correct escape hatch a new builder must use for a date boundary that
    # has to be right regardless of the session zone — these MUST stay Central-pinned
    # via CONVERT_TIMEZONE and must NOT fall back to session-tz CURRENT_DATE().
    from app.data.common import account_month_start_sql, account_today_sql
    today, month = account_today_sql(), account_month_start_sql()
    assert "CONVERT_TIMEZONE('America/Chicago'" in today
    assert "CONVERT_TIMEZONE('America/Chicago'" in month
    assert "CURRENT_DATE()" not in today
    assert "CURRENT_DATE()" not in month


def test_python_account_clock_is_tz_aware():
    # account_now()/account_today() resolve "now"/"today" in the account zone, not the
    # naive server (UTC) clock — the Python side of the same Central anchor.
    from app.logic import formulas
    src = inspect.getsource(formulas.account_now)
    assert "ZoneInfo(ACCOUNT_TIMEZONE)" in src


def test_timezone_standard_is_documented():
    # the standard (and the WHY — the SiS ALTER SESSION no-op reliance) is on record in
    # the data-layer conventions, where a builder author will see it.
    common = (_ROOT / "app" / "data" / "common.py").read_text(encoding="utf-8")
    assert "TIMEZONE STANDARD" in common
    assert "account_today_sql" in common and "CONVERT_TIMEZONE('America/Chicago'" in common
    assert "CANNOT ALTER SESSION" in common
