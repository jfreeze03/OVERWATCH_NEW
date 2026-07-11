"""Locks + behavior for V037 (pattern mart v2) and Compare Phase 1 (v4.28.0).

Design authority: docs/design/COMPARE_MODE.md. Locks per the design doc:
pairing math boundary edges, partial-month exclusion, triage-filter law,
movers parity with the CR pattern.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.data import mart27_sql
from app.logic import compare as compare_logic

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V037__pattern_env_grain.sql").read_text(encoding="utf-8")


def test_default_pairing_is_last_full_month_vs_prior():
    p = compare_logic.period_pair("month", date(2026, 7, 15))
    assert p["a"] == ("2026-06-01", "2026-07-01")
    assert p["b"] == ("2026-05-01", "2026-06-01")
    assert not p["partial"]


def test_month_pairing_crosses_the_year_boundary():
    p = compare_logic.period_pair("month", date(2026, 1, 10))
    assert p["a"] == ("2025-12-01", "2026-01-01")
    assert p["b"] == ("2025-11-01", "2025-12-01")


def test_partial_month_never_a_side_by_default_and_toggle_pairs_equal_days():
    today = date(2026, 7, 11)
    default = compare_logic.period_pair("month", today)
    assert today.isoformat() not in default["a"]          # partial excluded
    p = compare_logic.period_pair("month", today, include_partial=True)
    assert p["partial"] and p["a"] == ("2026-07-01", "2026-07-12")
    assert p["b"] == ("2026-06-01", "2026-06-12")         # same 11 days
    assert "partial" in p["label_a"]


def test_partial_pairing_clamps_at_a_short_prior_month():
    p = compare_logic.period_pair("month", date(2026, 3, 30), include_partial=True)
    assert p["b"] == ("2026-02-01", "2026-03-01")         # Feb ends where Feb ends


def test_trailing_windows_are_contiguous_and_exclude_today():
    today = date(2026, 7, 11)
    p = compare_logic.period_pair("7d", today)
    assert p["a"] == ("2026-07-04", "2026-07-11")         # today (partial) excluded
    assert p["b"] == ("2026-06-27", "2026-07-04")         # b1 == a0, no gap
    p30 = compare_logic.period_pair("30d", today)
    assert p30["a"][1] == "2026-07-11" and p30["b"][1] == p30["a"][0]


def test_kpi_grains_are_the_corrected_ones():
    wh = mart27_sql.compare_warehouse_credits("2026-06-01", "2026-07-01",
                                              "2026-05-01", "2026-06-01", "ALFA")
    assert "FACT_WAREHOUSE_DAILY" in wh and "COMPANY = 'ALFA'" in wh
    act = mart27_sql.compare_activity("2026-06-01", "2026-07-01",
                                      "2026-05-01", "2026-06-01", "ALFA")
    assert "FACT_QUERY_HOURLY" in act and "FAILED_COUNT" in act
    bill = mart27_sql.compare_billed("2026-06-01", "2026-07-01",
                                     "2026-05-01", "2026-06-01")
    assert "FACT_METERING_DAILY" in bill and "COMPANY" not in bill  # account-wide
    assert "ACCOUNT_USAGE" not in wh + act + bill         # facts only, no live scans


def test_pattern_movers_are_measured_scoped_and_floored():
    sql = mart27_sql.compare_pattern_costs("2026-06-01", "2026-07-01",
                                           "2026-05-01", "2026-06-01", "ALFA")
    assert "MART_PATTERN_COST_DAILY" in sql
    assert "(p.COMPANY = 'ALFA' OR UPPER(p.COMPANY) = 'ALL')" in sql
    assert "GREATEST" in sql and "> 0.01" in sql          # noise floor, either side
    assert "ABS(A_CREDITS - B_CREDITS) DESC" in sql       # movers parity


def test_compare_dates_validate_and_company_escapes():
    with pytest.raises(ValueError):
        mart27_sql.compare_billed("2026-06-01", "not-a-date",
                                  "2026-05-01", "2026-06-01")
    sql = mart27_sql.compare_warehouse_credits("2026-06-01", "2026-07-01",
                                               "2026-05-01", "2026-06-01", "x'y")
    assert "x''y" in sql                                  # sql_literal doubling


def test_v037_guard_shape_and_pieces():
    assert "EXCEPTION (-20037" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                # the V035 lesson holds
    assert "IF (v < 36) THEN" in _MIG and "SELECT 37 AS VERSION" in _MIG
    assert "CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(30);" in _MIG
    body = _MIG.split("SP_LOAD_PATTERN_COST", 1)[1]
    assert "COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME)" in body   # V030 shape law
    assert "COMPANY_FOR_WAREHOUSE(MAX(" not in _MIG
    assert "HLL_ACCUMULATE(q.USER_NAME)" in body          # mergeable state in
    assert "HLL_COMBINE(m.USERS_HLL)" in body             # combined at mart grain
    assert "AND t.DATABASE_NAME = s.DATABASE_NAME" in body  # new grain in the MERGE key
    tail = _MIG.split("TASK_PATTERN_COST_DAILY RESUME", 1)[1]
    assert "TASK_LOAD_DAILY RESUME" in tail               # root resumes


def test_pattern_reader_estimates_true_window_distinct_users():
    sql = mart27_sql.pattern_cost(30, "ALFA", 25)
    assert "HLL_ESTIMATE(HLL_COMBINE(p.USERS_HLL)) AS USERS" in sql
    assert "MAX(p.USERS)" not in sql                      # the r11 #9 bug stays dead


def test_compare_tab_is_wired_mart_only_and_honest():
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert '"Compare",' in cost and "_compare_tab" in cost
    cmp_src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "compare.py").read_text(encoding="utf-8")
    assert cmp_src.count("ACCOUNT_USAGE") == 0            # budget pin holds at 0
    assert "account-wide" in cmp_src                      # billed KPI labeled
    assert "period_pair" in cmp_src
    assert "run_batch" in cmp_src                         # one parallel batch, Brief pattern
    ch = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
    assert "def paired_bars" in ch
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("compare_warehouse_credits", "compare_activity",
                 "compare_billed", "compare_pattern_costs"):
        assert f"mart27_sql.{name}" in canary, name
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V037 applied" in val


def test_delta_chip_survives_an_empty_b_side():
    """Live crash 2026-07-11 (Joe's screenshot): pct_delta returns None when
    the prior side is zero — the chip and the volume table must render, not
    raise. Behavioral, per r11 #14."""
    from app.logic.formulas import pct_delta
    from app.ui.pages.cost_parts.compare import _delta_chip

    assert pct_delta(42.0, 0.0) is None                   # the documented contract
    assert _delta_chip(42.0, 0.0) == "no B-side data"     # never formats None
    assert _delta_chip(110.0, 100.0) == "+10.0% vs B"
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "compare.py").read_text(encoding="utf-8")
    assert "pct_delta(a, b):+" not in src                 # no direct format of pct_delta
    assert "round(pct_delta" not in src                   # round(None) is the same crash
