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


# ---------------------------------------------------------------------------
# Batch 1: the rest of the Cost > Spend tab tiles (cloud-services, CoCo, vs-prior)
# ---------------------------------------------------------------------------
def test_scope_window_where_helper_trailing_and_bounded():
    from app.data.common import scope_window_where
    assert scope_window_where("DAY", 30) == "DAY >= DATEADD('day', -30, CURRENT_DATE())"
    assert scope_window_where("DAY", 30, exclude_today=True) == (
        "DAY >= DATEADD('day', -30, CURRENT_DATE()) AND DAY < CURRENT_DATE()")
    assert scope_window_where("DAY", 31, bounds=_AUG) == "DAY >= '2026-08-01' AND DAY < '2026-09-01'"


def test_cloud_services_ratio_builders_honor_last_month():
    mart = mart_sql.fact_cloud_services_ratio(31, "ALL", bounds=_AUG)
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in mart
    assert "DATEADD" not in mart
    # live fallback keeps its CURRENT_TIMESTAMP trailing form when unbounded
    live_plain = cost_sql.cloud_services_ratio_by_warehouse(30)
    assert "START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())" in live_plain
    live_lm = cost_sql.cloud_services_ratio_by_warehouse(31, bounds=_AUG)
    assert "START_TIME >= '2026-08-01' AND START_TIME < '2026-09-01'" in live_lm


def test_ai_code_daily_coco_honors_last_month():
    from app.data import mart27_sql
    lm = mart27_sql.ai_code_daily(31, "ALL", bounds=_AUG)
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in lm
    # the coverage guard now requires the fact to reach the window START
    assert "(SELECT FIRST_DAY FROM cov) <= '2026-08-01'" in lm
    plain = mart27_sql.ai_code_daily(30, "ALL")
    assert "DAY >= DATEADD('day', -30, CURRENT_DATE())" in plain


def test_warehouse_vs_prior_uses_prior_calendar_month_for_last_month():
    # CURRENT = August, PRIOR = July (the month before), not "the 31 days before August"
    lm = mart_sql.fact_warehouse_window_vs_prior(31, "ALL", bounds=_AUG)
    assert "DAY >= '2026-07-01' AND DAY < '2026-09-01'" in lm          # scan spans both months
    assert "IFF(DAY >= '2026-08-01'" in lm                             # current = August
    assert "IFF(DAY < '2026-08-01'" in lm                              # prior = July split
    # live fallback: same prior-month semantics, START_TIME column
    live = cost_sql.warehouse_window_vs_prior(31, bounds=_AUG)
    assert "START_TIME >= '2026-07-01'" in live and "START_TIME < '2026-09-01'" in live
    assert "START_TIME >= '2026-08-01'" in live                        # current split
    # unbounded keeps the trailing equal-window form
    plain = mart_sql.fact_warehouse_window_vs_prior(30)
    assert "DATEADD('day', -" in plain and "2026-07" not in plain


def test_spend_tab_threads_bounds_to_cloud_services_and_coco():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert "fact_cloud_services_ratio(days, company, bounds=bounds)" in spend
    assert 'ai_code_daily(days, "ALL", bounds=bounds)' in spend
    assert "cloud_services_ratio_by_warehouse(days, company, bounds=bounds)" in spend


# ---------------------------------------------------------------------------
# Batch 2: Cost > Attribution tab — pool AND allocation shares share the window
# ---------------------------------------------------------------------------
def test_allocation_share_builders_honor_last_month():
    lm_live = cost_sql.allocated_attribution(31, "USER_NAME", "ALL", bounds=_AUG)
    assert "START_TIME >= '2026-08-01' AND START_TIME < '2026-09-01'" in lm_live
    from app.data import mart27_sql
    lm_mart = mart27_sql.alloc_xdim_attribution(31, "USER", "ALL", bounds=_AUG)
    assert "x.DAY >= '2026-08-01' AND x.DAY < '2026-09-01'" in lm_mart
    # unbounded keeps the trailing today-excluded effective window
    plain = mart27_sql.alloc_xdim_attribution(30, "USER", "ALL")
    assert "DATEADD('day', -" in plain and "CURRENT_DATE()" in plain and "2026-08" not in plain


def test_attribution_tab_threads_bounds_to_pool_and_shares():
    cost = _src("app/ui/pages/cost.py")
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert 'bounds=f["bounds"], wh_res=_pf.get("wh")' in cost
    # the prefetched vs-prior pool and both allocation share reads all carry bounds,
    # so per-entity dollars still reconcile to the exact-usage pool
    assert "fact_warehouse_window_vs_prior(days, company, bounds=bounds)" in spend
    assert "allocated_attribution(days, dim, company, database, schema_contains,\n" in spend
    assert 'alloc_xdim_attribution(days, dim.replace("_NAME", ""), company, database,\n' in spend
