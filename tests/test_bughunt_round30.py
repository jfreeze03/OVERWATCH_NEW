"""Bug-hunt round 30: pure computation core (date/window, scoring, governance, anomaly).

2 confirmed defects, both account-tz-vs-UTC temporal edges of a class the codebase already
fixed elsewhere (exec board, MTD strip, quarter, cost-vs-last-month all use the account clock).
The scoring/governance/anomaly finders' candidates were all refuted (page_verdict unknown-level
never supplied; governance NaN can't occur; C8 present-but-NULL unreachable).

#1 (MED): fact_daily_spend_year() anchored the calendar-year window on session-tz CURRENT_DATE()
   (UTC under SiS), so on New Year's Eve evening (America/Chicago) it already read Jan 1 and the
   YTD chart went empty for ~6 hours. Fixed: DATE_TRUNC('year', account_today_sql()) — matching
   the MTD/quarter sibling builders.
#2 (MED): the CURRENT_MONTH / CURRENT_YEAR presets computed their day OFFSET on the account clock
   (resolve_window_days -> account_today) but window_bounds() returned None for them, so they fell
   back to the trailing DATEADD(-offset, CURRENT_DATE()) predicate — a session-tz anchor that
   disagreed with the account-clock offset and drifted the MTD/YTD window a day at the evening
   boundary. Fixed: window_bounds() now returns explicit account-clock (start, end_exclusive)
   for CURRENT_MONTH/CURRENT_YEAR (like LAST_MONTH), so the window and its label share one clock.
"""

from __future__ import annotations

from datetime import date

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW
from app.data import common, mart_sql
from app.logic.date_windows import window_bounds

# --- #1: the calendar-year spend builder is anchored to the account clock ------------

def test_fact_daily_spend_year_uses_the_account_clock():
    sql = mart_sql.fact_daily_spend_year()
    # the account-tz "today" expression is present; the session-tz CURRENT_DATE() anchor is gone
    assert f"DATE_TRUNC('year', {common.account_today_sql()})" in sql
    assert "DATE_TRUNC('year', CURRENT_DATE())" not in sql


# --- #2: the period-to-date presets are account-clock-bounded, not trailing ----------

def test_period_to_date_presets_return_account_clock_bounds():
    # mid-month/-year: first of the period (inclusive) .. today+1 (exclusive, today included)
    assert window_bounds(CURRENT_MONTH_WINDOW, date(2026, 9, 17)) == (date(2026, 9, 1), date(2026, 9, 18))
    assert window_bounds(CURRENT_YEAR_WINDOW, date(2026, 9, 17)) == (date(2026, 1, 1), date(2026, 9, 18))
    # first day of the period: window is just that one day (start .. start+1)
    assert window_bounds(CURRENT_MONTH_WINDOW, date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 1, 2))
    assert window_bounds(CURRENT_YEAR_WINDOW, date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 1, 2))
    # plain trailing windows stay unbounded (they end at "now")
    assert window_bounds(30, date(2026, 9, 17)) is None


def test_mtd_bounds_reach_the_headline_spend_reader_as_explicit_dates():
    # a bounds-honoring builder, given the CURRENT_MONTH bounds, emits an explicit account-clock
    # date range instead of the drifting trailing CURRENT_DATE() predicate
    bounds = window_bounds(CURRENT_MONTH_WINDOW, date(2026, 9, 17))
    sql = mart_sql.fact_metering_by_service(14, bounds=bounds)
    assert "2026-09-01" in sql
    assert "DATEADD('day', -14, CURRENT_DATE())" not in sql
