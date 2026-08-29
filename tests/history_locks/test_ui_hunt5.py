"""Round-5 UI-layer bug hunt (v4.342.0) — locks for the two confirmed fixes.

Round 5 ran seven not-yet-run lenses; five came back clean (charts-exhaustive,
empty-honesty, boundary-thresholds, column-config, account-time). The two survivors
were both help-text strings that misdescribed a correct computation — fixed here.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.logic.formulas import budget_pace_variance

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_reopen_rate_help_matches_the_90d_no_bound_metric():
    # incident_metrics(90).REOPEN_PCT applies NO 14-day bound and INCIDENT_REOPEN_DAYS
    # is dead config — the help must not claim a "14 days (owner-set window)".
    a = _src("app/ui/pages/alerts.py")
    reopen = a.split('"label": "Reopen rate"', 1)[1].split("},", 1)[0]
    assert "within 14 days" not in reopen and "owner-set window" not in reopen
    assert "resolved in the last 90 days" in reopen and "REOPENED_FROM" in reopen


def test_budget_pace_help_describes_completed_days_today_excluded():
    # budget_pace_variance uses completed = elapsed - 1 (today excluded), so the help's
    # formula must say "completed days", not "day_of_month".
    ov = _src("app/ui/pages/overview.py")
    assert '"label": "Pace vs budget calendar"' in ov
    assert "/ days_in_month x completed days" in ov
    assert "today excluded, since metering lags" in ov
    assert "x day_of_month)" not in ov
    # behavioral: the value the help describes really excludes today (completed = elapsed-1)
    # day 15 of a 31-day month, $31000 budget: expected-to-date = 31000 * 14/31 = 14000
    _var, _expected = budget_pace_variance(0.0, 31000.0, date(2026, 8, 15))
    assert round(_expected, 2) == round(31000.0 * 14 / 31, 2)   # 14 completed days, NOT 15
