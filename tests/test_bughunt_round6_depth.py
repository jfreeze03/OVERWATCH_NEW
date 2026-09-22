"""Locks for bug-hunt round 6 (wf_b64c483b): forecast today-anchor, Security unload uncapped
totals, Control Room prior-day-by-date delta, and the Overview platform-score sparkline disclosure.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from app.data import security_sql
from app.logic.forecast import month_end_projection

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_linear_forecast_anchors_on_today_not_last_present_day():
    # metering lags ~1-2d, so the last present complete day is routinely today-2; the projection
    # must anchor on TODAY (matching gap_fill's "starts at today" + the seasonal engine) so a
    # trailing gap is not double-counted and the month tail is not dropped (bug-hunt wdmz68vd4).
    today = dt.date(2026, 9, 20)
    gapped = pd.DataFrame([{"DAY": dt.date(2026, 9, d), "USD": 100.0 + 10.0 * d}
                           for d in range(4, 18)])          # present only through 9-17 (trailing gap)
    fresh = pd.DataFrame([{"DAY": dt.date(2026, 9, d), "USD": 100.0 + 10.0 * d}
                          for d in range(4, 20)])           # through 9-19 (no trailing gap)
    r_gap = month_end_projection(gapped, today=today)
    r_fresh = month_end_projection(fresh, today=today)
    assert abs(r_gap.projected_usd - r_fresh.projected_usd) / r_fresh.projected_usd < 0.05
    src = _read("app/logic/forecast.py")
    assert "today_x = float((today - _origin).days)" in src
    assert "slope * (today_x + k)) for k in range(project_days)" in src
    assert "last_x = xs[-1]" not in src and "slope * (last_x + k)" not in src  # anchor gone


def test_unload_kpis_use_uncapped_totals():
    # the unload feed is LIMIT-300 per (day,user,role); the KPI tiles must sum an uncapped aggregate.
    sql = security_sql.unload_activity_totals(90, "ALFA", "MYDB", "STG")
    assert "COUNT(*) AS UNLOADS" in sql and "COUNT(DISTINCT USER_NAME) AS USERS" in sql
    assert "LIMIT" not in sql and "GROUP BY" not in sql
    src = _read("app/ui/pages/security.py")
    assert "security_sql.unload_activity_totals(" in src


def test_control_room_prior_day_delta_is_by_calendar_date():
    src = _read("app/ui/pages/control_room.py")
    # compares yesterday vs day-before-yesterday BY DATE, not the last two present rows (iloc)
    assert "_yday, _pday = account_today() - timedelta(days=1), account_today() - timedelta(days=2)" in src
    assert '_ca["QUERIES"].iloc[-1]' not in src


def test_overview_platform_score_discloses_account_wide_sparkline():
    src = _read("app/ui/pages/overview.py")
    assert "sparkline is the" in src and "ACCOUNT-WIDE even under a company" in src
