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
    # names exactly where it is exact (now Cost & Overview), and that others approximate it
    assert "applied exactly across Cost & Overview" in main and "approximate" in main


def test_overview_economics_route_last_month_to_the_bounded_live_path():
    ov = _src("app/ui/pages/overview.py")
    # the exec board is WINDOW_DAYS-keyed and cannot express a bounded month, so Last
    # month (f["bounds"] set) skips it and reads the bounded live daily aggregate
    assert '_load_board(company, days, f["window"]) if _ov_bounds is None else None' in ov
    assert "_live_fallback_daily(company, days, rate, bounds=_ov_bounds)" in ov
    assert "warehouse_daily_credits(days, company, bounds=bounds)" in ov
    # top drivers for Last month are derived from that same bounded warehouse frame
    assert '_ov_bounds is not None and trend_source.usable()' in ov
    # the vs-prior spend delta honors the bounded window too
    assert "fact_warehouse_window_vs_prior(days, company, bounds=_ov_bounds)" in ov


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


# ---------------------------------------------------------------------------
# Batch 3: the remaining Cost data builders all accept and honor the bounds
# (storage, tags, chargeback, cortex/AI, optimization, unit-costs). One sample
# per file locks that the bounded predicate fires and no trailing scope leaks.
# ---------------------------------------------------------------------------
def test_remaining_cost_builders_honor_last_month_bounds():
    from app.data import (
        chargeback_sql,
        cortex_sql,
        etl_sql,
        graph_sql,
        insights_sql,
        mart27_sql,
        ops_sql,
    )
    samples = [
        cost_sql.warehouse_daily_credits(31, "ALL", bounds=_AUG),
        cost_sql.storage_account_truth(31, bounds=_AUG),
        cost_sql.tag_coverage(31, "ALL", bounds=_AUG),
        cost_sql.qas_roi(31, "ALL", bounds=_AUG),                 # two predicates, both bound
        cost_sql.transfer_egress_priced(31, bounds=_AUG),          # CURRENT_TIMESTAMP anchor
        mart_sql.fact_cortex_daily_spend(31, bounds=_AUG),
        mart27_sql.pattern_cost(31, "ALL", bounds=_AUG),
        cortex_sql.cortex_ai_functions_daily(31, bounds=_AUG),
        chargeback_sql.department_window_credits(31, "ALL", bounds=_AUG),
        insights_sql.expensive_queries_usd(31, "ALL", bounds=_AUG),  # where_q AND where_m
        etl_sql.etl_cost_by_pipeline(31, "ALL", bounds=_AUG),
        graph_sql.graph_daily_costs(31, "ALL", bounds=_AUG),
        ops_sql.result_cache_daily(31, "ALL", bounds=_AUG),
    ]
    for sql in samples:
        assert "'2026-08-01'" in sql and "'2026-09-01'" in sql, sql[:120]
        assert "-31, CURRENT_" not in sql, sql[:120]              # no leftover trailing scope
    # qas_roi and expensive_queries_usd bind BOTH of their scope predicates
    assert cost_sql.qas_roi(31, "ALL", bounds=_AUG).count("'2026-08-01'") >= 2
    assert insights_sql.expensive_queries_usd(31, "ALL", bounds=_AUG).count("'2026-08-01'") >= 2
    # Ask's cortex_model_costs is unchanged when called without bounds (positional Ask path)
    assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" in cortex_sql.cortex_model_costs(30)


# ---------------------------------------------------------------------------
# Batch 4: the Cost page threads f["bounds"] into every remaining tab
# ---------------------------------------------------------------------------
def test_cost_page_threads_bounds_into_every_tab():
    cost = _src("app/ui/pages/cost.py")
    for call in (
        "_storage_tab(f[\"company\"], f[\"days\"], settings, bounds=f[\"bounds\"])",
        "_chargeback_tab(f[\"company\"], f[\"days\"], rate, is_operator, bounds=f[\"bounds\"])",
        "_cortex_spend_tab(f[\"days\"], ai_rate, bounds=f[\"bounds\"])",
        "_ai_users_tab(f[\"company\"], f[\"days\"], ai_rate, settings, is_operator, bounds=f[\"bounds\"])",
        "_optimization_tab(f[\"company\"], f[\"days\"], rate, settings, is_operator, bounds=f[\"bounds\"])",
    ):
        assert call in cost, call
    # query-tag governance (inline) threads bounds + a Last-month cache discriminator
    assert "_tag_bounds = f[\"bounds\"]" in cost
    assert "tag_coverage_daily(f[\"days\"], f[\"company\"], bounds=_tag_bounds)" in cost
    assert "untagged_executions_for_user(\n" in cost and "bounds=_tag_bounds)" in cost
    # unit-costs receives the whole filter dict, so it reads bounds itself
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert 'bounds = f["bounds"]' in uc
