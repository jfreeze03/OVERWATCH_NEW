"""Locks for app/logic/budgets.py — native Snowflake budget folding.

The read layer hands in GET_SERVICE_TYPE_USAGE_V2 rows (MTD spend by service) and a
best-effort scalar spending limit; these fold them to totals + a breakdown + an
in-app month-end projection, degrading cleanly when the native limit is unknown.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.logic.budgets import native_budget_summary, project_month_end


def test_summary_folds_by_service_and_prices():
    usage = pd.DataFrame([
        {"SERVICE_TYPE": "WAREHOUSE_METERING", "CREDITS": 100.0},
        {"SERVICE_TYPE": "WAREHOUSE_METERING", "CREDITS": 50.0},   # two rows -> summed
        {"SERVICE_TYPE": "AI_SERVICES", "CREDITS": 30.0},
    ])
    summary, by_service = native_budget_summary(usage, 2.0)
    assert summary["mtd_credits"] == 180.0
    assert summary["mtd_usd"] == 360.0
    # grouped, priced, sorted desc by credits
    assert list(by_service["SERVICE_TYPE"]) == ["WAREHOUSE_METERING", "AI_SERVICES"]
    assert by_service.iloc[0]["CREDITS"] == 150.0 and by_service.iloc[0]["USD"] == 300.0


def test_summary_empty_and_missing_columns():
    z, empty = native_budget_summary(pd.DataFrame(), 3.68)
    assert z == {"mtd_credits": 0.0, "mtd_usd": 0.0} and empty.empty
    assert native_budget_summary(None, 3.68)[0]["mtd_usd"] == 0.0
    # a frame with no SERVICE_TYPE column still folds (labelled ALL)
    s, bs = native_budget_summary(pd.DataFrame([{"CREDITS": 12.0}]), 1.0)
    assert s["mtd_credits"] == 12.0 and list(bs["SERVICE_TYPE"]) == ["ALL"]


def test_project_month_end_straight_lines_by_day_of_month():
    # $100 MTD on day 10 of a 30-day month -> $300 projected
    assert project_month_end(100.0, dt.date(2026, 9, 10)) == 300.0
    # day 15 of a 31-day month
    assert round(project_month_end(150.0, dt.date(2026, 1, 15)), 2) == round(150.0 / 15 * 31, 2)


def test_builder_uses_the_confirmed_live_table_function():
    from app.data import cost_sql
    u = cost_sql.native_budget_service_usage("2026-09")
    assert "TABLE(SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!GET_SERVICE_TYPE_USAGE_V2(" in u
    assert "'2026-09', '2026-09'" in u
    assert "SUM(CREDITS_USED) AS CREDITS" in u
    # native read carries NO ACCOUNT_USAGE literal (SNOWFLAKE.LOCAL / CORE.BUDGET), and no
    # scalar GET_SPENDING_LIMIT() SELECT (CALL-only -> would compile-error + log noise)
    assert "ACCOUNT_USAGE" not in u
    assert not hasattr(cost_sql, "native_budget_spending_limit")
