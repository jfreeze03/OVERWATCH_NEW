"""Locks for the 2026-07-08 live findings, round 2 (v4.6.4).

1. Alert feeds honor the company filter: Trexis warehouse noise must not
   surface under an ALFA scope, while account-level events (COMPANY='ALL')
   show for everyone.
2. The Database picker honors the Environment filter: ALFA + PROD offers
   exactly the two PROD databases, not the whole family.
3. Contract truth from ORGANIZATION_USAGE: builder shapes and the
   burn/runway math (top-ups must not read as negative burn).
"""

from __future__ import annotations

import pandas as pd

from app.companies import (
    classify_environment,
    database_options,
    databases_for,
)
from app.core.sqlsafe import sql_literal
from app.data import cost_sql, mart_sql
from app.logic.contract_planner import remaining_balance_summary

# ---------------------------------------------------------------------------
# 1. open_alert_events company scoping
# ---------------------------------------------------------------------------

def test_open_alert_events_default_is_unscoped():
    sql = mart_sql.open_alert_events(50)
    assert "COMPANY = '" not in sql
    assert "STATUS IN ('OPEN', 'ACK')" in sql


def test_open_alert_events_all_is_unscoped():
    assert "COMPANY = '" not in mart_sql.open_alert_events(50, "ALL")


def test_open_alert_events_scopes_company_plus_account_level():
    sql = mart_sql.open_alert_events(50, "ALFA")
    assert "COMPANY = 'ALFA'" in sql
    assert "UPPER(COMPANY) = 'ALL'" in sql          # account-wide fires always show
    assert "STATUS IN ('OPEN', 'ACK')" in sql        # base predicate survives ANDing


def test_open_alert_events_trexis_scope():
    sql = mart_sql.open_alert_events(300, "Trexis")
    assert "COMPANY = 'Trexis'" in sql
    assert "UPPER(COMPANY) = 'ALL'" in sql


def test_open_alert_events_company_is_literal_escaped():
    hostile = "x'; DROP TABLE ALERT_EVENTS; --"
    sql = mart_sql.open_alert_events(50, hostile)
    assert sql_literal(hostile) in sql               # doubled-quote escaping applied
    assert "'; DROP" not in sql.replace(sql_literal(hostile), "")


def test_open_alert_events_limit_still_clamped():
    assert "LIMIT 1000" in mart_sql.open_alert_events(999999, "ALFA")


# ---------------------------------------------------------------------------
# 2. Environment-aware database picker
# ---------------------------------------------------------------------------

def test_alfa_prod_is_exactly_the_two_prod_databases():
    assert databases_for("ALFA", "PROD") == ("ALFA_EDW_PRD", "ALFA_EDW_MGM")


def test_trexis_prod_is_exactly_the_three_prd_databases():
    assert databases_for("Trexis", "PROD") == (
        "TRXS_ABC_METADATA_PRD", "TRXS_EDW_PRD", "TRXS_GW_DATA_PRD")


def test_alfa_nonprod_excludes_prod_and_keeps_admin():
    dbs = databases_for("ALFA", "NONPROD")
    assert "ALFA_EDW_PRD" not in dbs and "ALFA_EDW_MGM" not in dbs
    assert "ADMIN" in dbs and "ALFA_EDW_DEV" in dbs


def test_env_all_matches_company_options():
    for company in ("ALFA", "Trexis", "ALL"):
        assert databases_for(company, "ALL") == database_options(company)
        assert databases_for(company, "") == database_options(company)


def test_databases_for_agrees_with_the_sql_classifier():
    # The picker list and the SQL environment_clause must never drift.
    for company in ("ALFA", "Trexis", "ALL"):
        for env in ("PROD", "NONPROD"):
            for db in databases_for(company, env):
                assert classify_environment(db) == env, (company, env, db)


# ---------------------------------------------------------------------------
# 3. Org contract truth — builders
# ---------------------------------------------------------------------------

def test_org_contract_items_shape():
    sql = cost_sql.org_contract_items()
    assert "SNOWFLAKE.ORGANIZATION_USAGE.CONTRACT_ITEMS" in sql
    assert "START_DATE" in sql and "END_DATE" in sql and "AMOUNT" in sql


def test_org_remaining_balance_shape_and_clamp():
    sql = cost_sql.org_remaining_balance(120)
    assert "SNOWFLAKE.ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY" in sql
    assert "-120," in sql
    assert "TOTAL_REMAINING" in sql
    assert "ON_DEMAND_CONSUMPTION_BALANCE" in sql
    clamped = cost_sql.org_remaining_balance(999999)
    assert "-400," in clamped                        # bounded, not unbounded history


# ---------------------------------------------------------------------------
# 3b. Org contract truth — burn/runway math
# ---------------------------------------------------------------------------

def _frame(days_values, on_demand=0.0):
    days = pd.date_range("2026-06-01", periods=len(days_values), freq="D")
    return pd.DataFrame({
        "DAY": days,
        "TOTAL_REMAINING": days_values,
        "ON_DEMAND_CONSUMPTION_BALANCE": [on_demand] * len(days_values),
    })


def test_summary_steady_burn():
    out = remaining_balance_summary(_frame([1000, 990, 980, 970, 960]))
    assert out["ok"]
    assert out["remaining_usd"] == 960
    assert out["burn_per_day_usd"] == 10
    assert out["runway_days"] == 96


def test_summary_ignores_renewal_topups():
    # +2000 top-up on day 3 must not average into burn as negative spend.
    out = remaining_balance_summary(_frame([1000, 990, 2990, 2980, 2970]))
    assert out["ok"]
    assert out["burn_per_day_usd"] == 10
    assert out["remaining_usd"] == 2970


def test_summary_flat_balance_has_no_runway():
    out = remaining_balance_summary(_frame([500, 500, 500]))
    assert out["ok"]
    assert out["burn_per_day_usd"] == 0
    assert out["runway_days"] is None


def test_summary_empty_frame_degrades():
    assert remaining_balance_summary(pd.DataFrame()) == {
        "ok": False, "reason": "No balance rows visible."}


def test_summary_sums_multiple_contracts_per_day():
    days = pd.to_datetime(["2026-06-01", "2026-06-01", "2026-06-02", "2026-06-02"])
    df = pd.DataFrame({
        "DAY": days,
        "TOTAL_REMAINING": [500, 500, 495, 495],
        "ON_DEMAND_CONSUMPTION_BALANCE": [0, 0, -25, -25],
    })
    out = remaining_balance_summary(df)
    assert out["ok"]
    assert out["remaining_usd"] == 990
    assert out["burn_per_day_usd"] == 10
    assert out["on_demand_usd"] == -50               # overrun surfaces, summed


def test_summary_burn_window_is_trailing():
    # Old, faster burn outside the window must not affect the average.
    values = [2000, 1900, 1800, *range(1800, 1660, -10)]  # then 10/day
    out = remaining_balance_summary(_frame(values), burn_window_days=5)
    assert out["burn_per_day_usd"] == 10
