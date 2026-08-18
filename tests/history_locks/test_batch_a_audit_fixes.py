"""Regression locks for the Batch A audit fixes (2026-07-28 bug audit).

Each of these bugs existed because nothing pinned the correct behavior. These
locks make the audit findings permanent guards.
"""
import re

import pandas as pd

from app import companies
from app.data import cost_sql, mart_sql, ops_sql
from app.logic import cortex, sizing


def test_exec_board_reads_the_selected_long_window_not_90():
    """Audit #2: exec_board clamped days at 90, so 180/365 silently read the
    90-day board rows. It must read the real WINDOW_DAYS."""
    assert "WINDOW_DAYS = 180" in mart_sql.exec_board("ALFA", 180)
    assert "WINDOW_DAYS = 365" in mart_sql.exec_board("ALFA", 365)
    assert "WINDOW_DAYS = 90" in mart_sql.exec_board("ALFA", 90)
    # over-long still clamps to the mart maximum (365), never explodes
    assert "WINDOW_DAYS = 365" in mart_sql.exec_board("ALFA", 9999)


def test_fact_daily_spend_honors_the_long_window():
    """Audit #11: the '3-month' forecast backtest (fact_daily_spend(150)) was
    clamped to 90 days."""
    assert "-150," in mart_sql.fact_daily_spend(150).replace(" ", "")
    assert "-365," in mart_sql.fact_daily_spend(9999).replace(" ", "")


def test_all_window_fed_mart_readers_honor_the_long_window():
    """Batch A verify: the #2 fix was incomplete — many sibling mart readers fed
    by the page window filter still clamped at 90. They must honor 180/365."""
    assert "WINDOW_DAYS = 180" in mart_sql.exec_board("ALFA", 180)
    for sql in (mart_sql.fact_metering_by_service(180),
                mart_sql.fact_query_window_summary(365, "ALFA"),
                mart_sql.fact_cloud_services_ratio(180, "ALFA"),
                mart_sql.fact_task_daily(365, "ALFA"),
                mart_sql.fact_warehouse_daily(180, "ALFA"),
                mart_sql.fact_cortex_daily_spend(365),
                mart_sql.cloud_svc_top_shapes(180, "ALFA")):
        s = sql.replace(" ", "")
        assert ("-180," in s or "-365," in s), sql[:80]
        assert "-90," not in s, sql[:80]
    # window-vs-prior scans 2*days, so it caps at HALF the mart max (182), never
    # 365 (which would scan 730d and read the empty prior half as a false swing).
    vp = mart_sql.fact_warehouse_window_vs_prior(365, "ALFA").replace(" ", "")
    assert "-364," in vp   # 2 * 182


def test_alfa_database_scope_includes_dba_maint_db():
    """Audit #9: classify_database says DBA_MAINT_DB is ALFA, but the ALFA
    database_clause allowlist omitted it, so ALFA storage dropped it."""
    assert companies.classify_database("DBA_MAINT_DB") == "ALFA"
    assert "DBA_MAINT_DB" in companies.database_clause("ALFA")


def test_cache_hit_rate_denominator_is_successful_queries():
    """Audit #18: HIT_PCT divided by COUNT(*) (failures included), diluting the
    'share of successful queries' the docstring promises."""
    sql = ops_sql.result_cache_daily(7)
    assert "NULLIF(COUNT_IF(EXECUTION_STATUS = 'SUCCESS'), 0)" in sql
    # the denominator is no longer all-queries
    assert "/ NULLIF(COUNT(*), 0) * 100, 1) AS HIT_PCT" not in sql


def test_storage_calendar_divides_by_period_days_not_present_days():
    """Audit #19: AVG(bytes) divided by days-with-a-row overstated a database
    present only part of the window vs the monthly-average billing basis."""
    sql = cost_sql.storage_by_database_calendar("ALFA")
    assert "DATEDIFF('day'" in sql
    assert "AVG(COALESCE(DB_BYTES, 0))" not in sql


def test_resize_down_recommendation_is_matchable():
    """Audit #1: the resize-savings branch gated on startswith('DOWN') but the
    recommendation is 'Size down candidate' -> 'SIZE DOWN ...', so it was dead."""
    assert sizing.RECOMMEND_DOWN.upper().startswith("SIZE DOWN")
    assert not sizing.RECOMMEND_DOWN.upper().startswith("DOWN")   # the old gate never matched
    opt = (__import__("pathlib").Path(__file__).resolve().parents[2]
           / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    # the est_sz gate matches the real recommendation prefix
    assert 'startswith("SIZE DOWN")' in opt


def test_cortex_budget_breach_is_per_user_across_sources():
    """Audit #17: a user whose TOTAL projected spend breaches budget was never
    flagged Critical because the rollup is per (user, source) and each source
    alone was under budget."""
    rate = 2.0
    budget_usd = 100.0                      # -> budget_credits = 50
    enriched = pd.DataFrame([
        {"USER_NAME": "SVC_X", "SOURCE": "COMPLETE", "PROJECTED_30D_CREDITS": 30.0,
         "PROJECTED_30D_USD": 60.0, "TOTAL_REQUESTS": 1000, "TOTAL_CREDITS": 25.0,
         "CREDITS_PER_REQUEST": 0.001},
        {"USER_NAME": "SVC_X", "SOURCE": "EMBED", "PROJECTED_30D_CREDITS": 30.0,
         "PROJECTED_30D_USD": 60.0, "TOTAL_REQUESTS": 50, "TOTAL_CREDITS": 25.0,
         "CREDITS_PER_REQUEST": 0.50},        # a genuine CPR spike on one source
        {"USER_NAME": "SVC_Y", "SOURCE": "COMPLETE", "PROJECTED_30D_CREDITS": 10.0,
         "PROJECTED_30D_USD": 20.0, "TOTAL_REQUESTS": 9000, "TOTAL_CREDITS": 8.0,
         "CREDITS_PER_REQUEST": 0.001},
        # v4.147: CPR spikes are cohort-relative now — baseline peers give EMBED's
        # 0.50 cr/req a cohort to be a positive outlier of (>=5 eligible peers).
        *[{"USER_NAME": f"SVC_{n}", "SOURCE": "COMPLETE", "PROJECTED_30D_CREDITS": 10.0,
           "PROJECTED_30D_USD": 20.0, "TOTAL_REQUESTS": 1000, "TOTAL_CREDITS": 8.0,
           "CREDITS_PER_REQUEST": 0.001} for n in ("Z", "W", "V", "U")],
    ])
    out = cortex.classify_exceptions(enriched, budget_usd, rate)
    # per-user breach: ONE deduped row, with the AGGREGATE breach dollars (60+60),
    # not a per-source figure that would read below the budget it breached.
    breach = out[(out["USER_NAME"] == "SVC_X") & (out["SIGNAL"] == "Budget breach")]
    assert len(breach) == 1
    assert breach.iloc[0]["SEVERITY"] == "Critical"
    assert breach.iloc[0]["PROJECTED_30D_USD"] == 120.0
    assert breach.iloc[0]["SOURCE"] == "(all sources)"
    # the CPR spike on the breaching user's EMBED source still surfaces (not masked)
    spike = out[(out["USER_NAME"] == "SVC_X") & (out["SIGNAL"] == "Cost per request spike")]
    assert len(spike) == 1 and spike.iloc[0]["SOURCE"] == "EMBED"
    # the under-budget user is not flagged as a breach
    assert out[(out["USER_NAME"] == "SVC_Y") & (out["SIGNAL"] == "Budget breach")].empty


def test_cortex_recreated_user_rows_sum_not_drop():
    """Batch A verify: a login NAME reused across USER_IDs yields two rows with
    the same (USER_NAME, SOURCE) but different EMAIL — distinct spend that must
    SUM to the person's total, not be de-duplicated away (which would miss the
    breach)."""
    enriched = pd.DataFrame([
        {"USER_NAME": "JOE", "SOURCE": "COMPLETE", "EMAIL": "joe@old", "PROJECTED_30D_CREDITS": 30.0,
         "PROJECTED_30D_USD": 60.0, "TOTAL_REQUESTS": 500, "TOTAL_CREDITS": 25.0, "CREDITS_PER_REQUEST": 0.001},
        {"USER_NAME": "JOE", "SOURCE": "COMPLETE", "EMAIL": "joe@new", "PROJECTED_30D_CREDITS": 30.0,
         "PROJECTED_30D_USD": 60.0, "TOTAL_REQUESTS": 500, "TOTAL_CREDITS": 25.0, "CREDITS_PER_REQUEST": 0.001},
    ])
    out = cortex.classify_exceptions(enriched, 100.0, 2.0)   # budget_credits = 50
    breach = out[out["SIGNAL"] == "Budget breach"]
    assert len(breach) == 1
    assert breach.iloc[0]["SEVERITY"] == "Critical"          # 30 + 30 = 60 > 50
    assert breach.iloc[0]["PROJECTED_30D_USD"] == 120.0      # summed, not dropped to 60


def test_department_credits_scope_by_company_column_not_name():
    """Audit #3: chargeback filtered by the WH_ALFA_% name pattern instead of the
    fact's evidence-based COMPANY column."""
    from app.data import chargeback_sql
    s = chargeback_sql.department_window_credits(7, "ALFA")
    assert "M.COMPANY = 'ALFA'" in s
    assert not re.search(r"WH.?_ALFA.?_%", s)
