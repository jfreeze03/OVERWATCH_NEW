"""Tests for the ported AI/Cortex user-attribution feature."""

import re

import pandas as pd
import pytest

from app.data import cortex_sql
from app.logic.cortex import classify_exceptions, enrich_user_rollup, rollup_summary

# ---- SQL builders ---------------------------------------------------------

@pytest.mark.parametrize("builder", [
    lambda: cortex_sql.cortex_code_user_rollup(7, "ALFA"),
    lambda: cortex_sql.cortex_code_daily(7, "ALFA"),
    lambda: cortex_sql.cortex_ai_functions_daily(7),
])
def test_cortex_scans_are_bounded(builder):
    assert re.search(r"DATEADD\('day',\s*-\d+", builder())


def test_rollup_covers_both_code_sources_and_users_join():
    sql = cortex_sql.cortex_code_user_rollup(14, "ALL")
    assert "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY" in sql
    assert "CORTEX_CODE_CLI_USAGE_HISTORY" in sql
    assert "ACCOUNT_USAGE.USERS" in sql
    assert "LIMIT 500" in sql


def test_rollup_carries_real_names():
    # Owner ask (v4.50): FIRST/LAST name ride the USERS join — selected in
    # both CTEs and grouped, so the per-user grain is unchanged.
    sql = cortex_sql.cortex_code_user_rollup(14, "ALL")
    assert "U.FIRST_NAME" in sql and "U.LAST_NAME" in sql
    assert "GROUP BY USER_NAME, EMAIL, FIRST_NAME, LAST_NAME, SOURCE" in sql


def test_display_name_prefers_person_falls_back_to_login():
    named = enrich_user_rollup(_rollup(FIRST_NAME="Kevin", LAST_NAME="Barr"), 2.20, 30)
    assert named.iloc[0]["DISPLAY_NAME"] == "Kevin Barr"
    # NULL names (service accounts, dropped users) fall back to the login —
    # never blank, never invented.
    unnamed = enrich_user_rollup(_rollup(FIRST_NAME=None, LAST_NAME=None), 2.20, 30)
    assert unnamed.iloc[0]["DISPLAY_NAME"] == "KEBARR1"
    # Pre-name frames (no columns at all) behave the same.
    legacy = enrich_user_rollup(_rollup(), 2.20, 30)
    assert legacy.iloc[0]["DISPLAY_NAME"] == "KEBARR1"


def test_rollup_company_scope_carries_kebarr1():
    alfa = cortex_sql.cortex_code_user_rollup(7, "ALFA")
    trexis = cortex_sql.cortex_code_user_rollup(7, "Trexis")
    assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in alfa
    assert "COMPANY_FOR_USER(USER_NAME) = 'Trexis'" in trexis


def test_no_dollar_rates_baked_into_sql():
    for sql in (cortex_sql.cortex_code_user_rollup(7),
                cortex_sql.cortex_code_daily(7),
                cortex_sql.cortex_ai_functions_daily(7)):
        assert "2.2" not in sql and "3.68" not in sql and "USD" not in sql.upper().replace("USD_", "")


def test_windows_clamped():
    # v4.54: Cortex user-cost readers honor the long window (owner-named live
    # exception — per-user token telemetry is low-volume, cheap to scan long).
    assert "-365," in cortex_sql.cortex_code_daily(9999, "ALL").replace(" ", "")
    assert "-365," in cortex_sql.cortex_code_user_rollup(9999, "ALL").replace(" ", "")


# ---- classification logic --------------------------------------------------

def _rollup(**overrides) -> pd.DataFrame:
    base = {
        "USER_NAME": "KEBARR1", "EMAIL": "k@x.com", "SOURCE": "Snowsight",
        "ACTIVE_DAYS": 5, "TOTAL_REQUESTS": 100, "TOTAL_CREDITS": 10.0,
        "TOTAL_TOKENS": 50000, "CREDITS_PER_REQUEST": 0.05, "AVG_DAILY_CREDITS": 2.0,
    }
    base.update(overrides)
    return pd.DataFrame([base])


def test_enrich_projects_30d_and_dollarizes():
    out = enrich_user_rollup(_rollup(), ai_rate_usd=2.20, window_days=30)
    row = out.iloc[0]
    # Calendar basis: 10 total credits over a 30d window -> 10 cr/30d.
    assert row["PROJECTED_30D_CREDITS"] == 10.0
    assert row["PROJECTED_30D_USD"] == 22.0            # * $2.20
    assert row["SPEND_USD"] == 22.0                    # 10 credits * $2.20


def test_budget_breach_is_critical():
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=300.0), 2.20, 30)  # 300 cr/30d
    out = classify_exceptions(enriched, ai_budget_usd=440.0, ai_rate_usd=2.20)  # budget=200 cr
    assert out.iloc[0]["SEVERITY"] == "Critical"
    assert out.iloc[0]["SIGNAL"] == "Budget breach"


def test_half_budget_is_concentration_and_quarter_is_high_usage():
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=120.0), 2.20, 30)  # 120 cr/30d
    out = classify_exceptions(enriched, ai_budget_usd=440.0, ai_rate_usd=2.20)  # budget=200 cr
    assert out.iloc[0]["SEVERITY"] == "High"
    assert out.iloc[0]["SIGNAL"] == "Budget concentration"
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=60.0), 2.20, 30)  # 60 cr/30d > 25%
    out = classify_exceptions(enriched, 440.0, 2.20)
    assert out.iloc[0]["SEVERITY"] == "Medium"


def test_cost_per_request_spike_flags_without_budget():
    enriched = enrich_user_rollup(_rollup(CREDITS_PER_REQUEST=0.25), 2.20, 30)
    out = classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20)
    assert out.iloc[0]["SEVERITY"] == "High"
    assert out.iloc[0]["SIGNAL"] == "Cost per request spike"


def test_no_budget_means_no_budget_severities():
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=30000.0), 2.20, 30)
    out = classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20)
    assert out.empty  # huge usage, but no configured budget and no CPR spike


def test_exceptions_ranked_critical_first():
    frames = [
        _rollup(USER_NAME="A", TOTAL_CREDITS=300.0),      # breach (>200 cr budget)
        _rollup(USER_NAME="B", CREDITS_PER_REQUEST=0.5),  # spike (High)
        _rollup(USER_NAME="C", TOTAL_CREDITS=60.0),       # high usage (Medium, >25%)
    ]
    enriched = enrich_user_rollup(pd.concat(frames, ignore_index=True), 2.20, 30)
    out = classify_exceptions(enriched, 440.0, 2.20)
    assert list(out["SEVERITY"]) == sorted(out["SEVERITY"], key={"Critical": 0, "High": 1, "Medium": 2}.get)
    assert out.iloc[0]["USER_NAME"] == "A"


def test_summary_totals():
    frames = pd.concat([_rollup(USER_NAME="A"), _rollup(USER_NAME="B", SOURCE="CLI")], ignore_index=True)
    summary = rollup_summary(enrich_user_rollup(frames, 2.20, 7), window_days=7)
    assert summary["active_users"] == 2
    assert summary["total_requests"] == 200
    assert summary["spend_usd"] == 44.0
    assert summary["projected_30d_usd"] == round(44.0 / 7 * 30, 2)


def test_empty_inputs_are_safe():
    assert enrich_user_rollup(pd.DataFrame(), 2.2, 7).empty
    assert classify_exceptions(pd.DataFrame(), 100, 2.2).empty
    assert rollup_summary(pd.DataFrame(), 7)["active_users"] == 0
