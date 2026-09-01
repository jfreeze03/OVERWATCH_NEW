"""'Last month' bounded scope window — headline-spend coverage (v4.408.0).

Last month is the PREVIOUS COMPLETE CALENDAR MONTH (bounded [start, end_exclusive)),
unlike every other window which is a trailing offset from today. This pins:
- resolve_effective_window emits the explicit calendar range when given bounds;
- the headline metering builders (mart + live fallback) honor it;
- the Cost Spend tab threads f["bounds"] into them;
- the scope bar discloses that coverage honestly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.data import cost_sql, mart_sql
from app.data.common import resolve_effective_window
from app.logic.date_windows import window_bounds

_ROOT = Path(__file__).resolve().parents[1]
# mid-month reference: Last month = all of August 2026 -> [2026-08-01, 2026-09-01)
_AUG = window_bounds("LAST_MONTH", date(2026, 9, 17))


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_resolve_effective_window_bounds_emits_calendar_range():
    eff, frag = resolve_effective_window(0, "DAY", bounds=_AUG)
    assert eff == 31                                   # August span
    assert frag == "DAY >= '2026-08-01' AND DAY < '2026-09-01'"
    # no bounds -> unchanged trailing, today-excluded half-open window
    _, trailing = resolve_effective_window(30, "DAY")
    assert "DATEADD('day', -30, CURRENT_DATE())" in trailing and "< CURRENT_DATE()" in trailing


def test_fact_metering_by_service_honors_last_month_bounds():
    sql = mart_sql.fact_metering_by_service(31, bounds=_AUG)
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in sql
    assert "DATEADD" not in sql                         # bounded range, not trailing
    # every other window keeps the trailing predicate untouched
    plain = mart_sql.fact_metering_by_service(30)
    assert "DAY >= DATEADD('day', -30, CURRENT_DATE())" in plain
    assert "2026-08-01" not in plain


def test_metering_daily_live_fallback_honors_last_month_bounds():
    sql = cost_sql.metering_daily_by_service(31, bounds=_AUG)
    assert "USAGE_DATE >= '2026-08-01' AND USAGE_DATE < '2026-09-01'" in sql
    assert "DATEADD" not in sql
    plain = cost_sql.metering_daily_by_service(30)
    assert "USAGE_DATE >= DATEADD('day', -30, CURRENT_DATE())" in plain


def test_cost_spend_tab_threads_bounds_to_the_headline_metering():
    cost = _src("app/ui/pages/cost.py")
    spend = _src("app/ui/pages/cost_parts/spend.py")
    # cost.py hands the calendar bounds to both the prefetch and the tab
    assert '_spend_attr_recent_jobs(f["company"], f["days"], f["bounds"])' in cost
    assert 'bounds=f["bounds"]' in cost
    # the tab (prefetch job + serial read + live fallback) all carry bounds
    assert "fact_metering_by_service(days, bounds=bounds)" in spend
    assert "metering_daily_by_service(days, bounds=bounds)" in spend


def test_scope_bar_discloses_last_month_coverage_honestly():
    main = _src("app/main.py")
    assert "_window == LAST_MONTH_WINDOW" in main
    # names exactly where it is exact, and that other panels approximate it
    assert "applied exactly on Cost" in main and "approximate" in main
