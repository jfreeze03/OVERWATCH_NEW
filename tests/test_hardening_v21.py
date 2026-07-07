"""Regression locks for the 2026-07 hardening pass (v4.1.0).

Each test pins one fix so it cannot silently regress:
- word-boundary LIMIT detection (row caps survive RATE_LIMIT columns)
- account-timezone 'today'
- HTML-escaped executive summary
- expired-session error mapping
- Cortex timeout constant + model validation
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.ai import CORTEX_TIMEOUT_SECONDS, MAX_PROMPT_CHARS, normalize_model
from app.core.errors import format_snowflake_error
from app.core.query import _with_row_cap
from app.logic.formulas import ACCOUNT_TIMEZONE, account_today, exec_summary_html

# ---------------------------------------------------------------------------
# Row-cap LIMIT detection
# ---------------------------------------------------------------------------

def test_row_cap_appended_when_no_limit():
    sql = "SELECT * FROM T"
    assert _with_row_cap(sql, 100).endswith("LIMIT 101")


def test_row_cap_not_duplicated_when_limit_present():
    sql = "SELECT * FROM T ORDER BY X LIMIT 50"
    assert _with_row_cap(sql, 100) == sql


def test_row_cap_survives_limit_like_column_names():
    # The old substring check ("LIMIT" in sql.upper()) skipped the cap here,
    # leaving the scan unbounded.
    sql = "SELECT RATE_LIMIT, CREDIT_LIMIT_USD FROM T"
    assert _with_row_cap(sql, 100).endswith("LIMIT 101")


def test_row_cap_survives_limit_in_comment_text():
    sql = "SELECT X FROM T -- limits apply per account"
    assert _with_row_cap(sql, 100).endswith("LIMIT 101")


def test_row_cap_zero_means_uncapped():
    sql = "SELECT * FROM T"
    assert _with_row_cap(sql, 0) == sql


def test_row_cap_strips_trailing_semicolon():
    out = _with_row_cap("SELECT * FROM T;", 10)
    assert ";" not in out.split("LIMIT")[0].rstrip()[-3:]
    assert out.endswith("LIMIT 11")


# ---------------------------------------------------------------------------
# Account-timezone today
# ---------------------------------------------------------------------------

def test_account_today_matches_chicago_calendar():
    expected = datetime.now(tz=ZoneInfo(ACCOUNT_TIMEZONE)).date()
    assert account_today() == expected


def test_account_timezone_is_chicago():
    assert ACCOUNT_TIMEZONE == "America/Chicago"


# ---------------------------------------------------------------------------
# Executive summary HTML escaping
# ---------------------------------------------------------------------------

def _summary(**overrides):
    kwargs = {
        "company": "ALFA", "days": 7, "generated": "2026-07-07 09:00",
        "window_spend": "$1,000", "mtd_line": "$2,000", "forecast_line": "$3,000",
        "alerts_line": "0 critical", "score_line": "98/100",
        "drivers": [], "actions": [],
    }
    kwargs.update(overrides)
    return exec_summary_html(**kwargs)


def test_exec_summary_escapes_company():
    html = _summary(company='<script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_exec_summary_escapes_drivers_and_actions():
    html = _summary(
        drivers=[("<b>drv</b>", "1.0", 'evidence & "quotes"')],
        actions=["<img src=x onerror=y>"],
    )
    assert "<b>drv</b>" not in html
    assert "<img" not in html
    assert "&amp;" in html


def test_exec_summary_normal_values_unchanged():
    html = _summary()
    assert "ALFA" in html and "$1,000" in html and "98/100" in html


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

def test_expired_session_maps_to_refresh_hint():
    msg = format_snowflake_error("390111: Session no longer exists: 12345")
    assert "Refresh data" in msg


def test_expired_token_maps_to_refresh_hint():
    msg = format_snowflake_error("Authentication token has expired. The user must authenticate again.")
    assert "Refresh data" in msg


def test_timeout_mapping_still_wins():
    msg = format_snowflake_error("Statement reached its statement or warehouse timeout")
    assert "statement timeout" in msg


# ---------------------------------------------------------------------------
# Cortex guards
# ---------------------------------------------------------------------------

def test_cortex_timeout_is_bounded():
    assert 30 <= CORTEX_TIMEOUT_SECONDS <= 300


def test_prompt_cap_unchanged():
    assert MAX_PROMPT_CHARS == 6000


def test_model_validation_rejects_injection():
    assert normalize_model("bad'model; DROP TABLE X") == "llama3.1-8b"
    assert normalize_model("claude-3-5-sonnet") == "claude-3-5-sonnet"
