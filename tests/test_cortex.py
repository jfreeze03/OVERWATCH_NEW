"""Tests for the ported AI/Cortex user-attribution feature."""

import re
from datetime import timedelta

import pandas as pd
import pytest

from app.data import cortex_sql, mart27_sql
from app.logic.cortex import (
    CPR_MIN_PROJECTED_USD,
    CPR_MIN_REQUESTS,
    aggregate_budget_row,
    classify_exceptions,
    daily_from_user_daily,
    effective_window_days,
    enrich_user_rollup,
    rollup_from_user_daily,
    rollup_summary,
    with_aggregate_budget_row,
)
from app.logic.formulas import account_today

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
    assert rollup_from_user_daily(pd.DataFrame(), 7).empty
    assert daily_from_user_daily(pd.DataFrame(), 7).empty
    assert aggregate_budget_row({}, 100.0) is None
    assert with_aggregate_budget_row(pd.DataFrame(), {}, 100.0).empty


# ---- P2: the fact readers must match the live builders' contracts ----------

def _select_columns(sql: str) -> list[str]:
    """Output column names of the FINAL SELECT (alias if present)."""
    body = sql.rsplit("\nSELECT\n", 1)[1].split("\nFROM ", 1)[0]
    out = []
    for raw in body.split(",\n"):
        token = raw.strip().rstrip(",")
        if " AS " in token.upper():
            token = re.split(r"\s+AS\s+", token, flags=re.IGNORECASE)[-1]
        out.append(token.strip().split(".")[-1])
    return out


def test_ai_code_rollup_matches_the_live_rollup_contract():
    # The live builder is SELECT * over by_user; its column order is the CTE's.
    live = cortex_sql.cortex_code_user_rollup(30, "ALL")
    live_cols = _select_columns(live.split("by_user AS (", 1)[1])
    mart_cols = _select_columns(mart27_sql.ai_code_user_rollup(30, "ALL"))
    assert mart_cols == live_cols


def test_ai_code_daily_matches_the_live_daily_contract():
    live_cols = _select_columns(cortex_sql.cortex_code_daily(30, "ALL"))
    mart_cols = _select_columns(mart27_sql.ai_code_daily(30, "ALL"))
    assert mart_cols == live_cols


def test_fact_readers_exclude_the_functions_arm():
    # The live builders read ONLY the Snowsight/CLI code views; the fact also
    # carries the account-wide Functions arm (USER_NAME 'ACCOUNT').
    for sql in (mart27_sql.ai_code_user_rollup(30, "ALL"),
                mart27_sql.ai_code_daily(30, "ALL")):
        assert "SOURCE <> 'Functions'" in sql


def test_fact_readers_are_coverage_gated():
    # A young fact must emit ZERO rows for a window it cannot cover, so the
    # panel falls back to live instead of silently UNDER-REPORTING spend.
    for sql in (mart27_sql.ai_code_user_rollup(180, "ALL"),
                mart27_sql.ai_code_daily(180, "ALL")):
        assert "MIN(DAY) AS FIRST_DAY" in sql
        assert "(SELECT FIRST_DAY FROM cov) <= DATEADD('day', -180 + 1, CURRENT_DATE())" in sql


def test_fact_rollup_scopes_company_after_grouping():
    sql = mart27_sql.ai_code_user_rollup(30, "Trexis")
    assert sql.count("COMPANY_FOR_USER") == 1
    # ...and after the GROUP BY, not inside the scanned CTE.
    assert sql.index("GROUP BY USER_NAME, SOURCE") < sql.index("COMPANY_FOR_USER")
    assert "COMPANY_FOR_USER" not in mart27_sql.ai_code_user_rollup(30, "ALL")


def test_fact_rollup_cannot_fan_out_on_recreated_logins():
    # A login dropped and recreated has several ACCOUNT_USAGE.USERS rows; a raw
    # join would DOUBLE that user's credits. The name join is pre-collapsed.
    sql = mart27_sql.ai_code_user_rollup(30, "ALL")
    assert "named AS (" in sql and "GROUP BY NAME" in sql


# ---- P9: one 365d live fetch, both aggregates derived in pandas ------------

def test_live_derive_fetch_is_days_independent_and_long_window():
    sql = cortex_sql.cortex_code_user_daily("ALFA")
    assert "-365," in sql.replace(" ", "")
    assert cortex_sql.cortex_code_user_daily("ALFA") == sql        # no days knob at all
    # company scope is applied ONCE post-aggregation, not per raw usage row
    assert sql.count("COMPANY_FOR_USER") == 1
    assert sql.index("GROUP BY 1, 2, 3, 4, 5, 6") < sql.index("COMPANY_FOR_USER")


def _user_daily(days_back=(0, 1, 2), user="KEBARR1", source="CLI", credits=1.0):
    today = account_today()
    return pd.DataFrame([
        {"USER_NAME": user, "EMAIL": "k@x.com", "FIRST_NAME": "Kevin", "LAST_NAME": "Barr",
         "SOURCE": source, "USAGE_DATE": today - timedelta(days=d),
         "REQUESTS": 10, "CREDITS": credits, "TOKENS": 100,
         "FIRST_TS": pd.Timestamp(today - timedelta(days=d)),
         "LAST_TS": pd.Timestamp(today - timedelta(days=d))}
        for d in days_back
    ])


def test_derived_rollup_matches_the_sql_contract_columns():
    live_cols = _select_columns(
        cortex_sql.cortex_code_user_rollup(30, "ALL").split("by_user AS (", 1)[1])
    out = rollup_from_user_daily(_user_daily(), 30)
    assert list(out.columns) == live_cols
    row = out.iloc[0]
    assert row["ACTIVE_DAYS"] == 3 and row["TOTAL_REQUESTS"] == 30
    assert row["CREDITS_PER_REQUEST"] == pytest.approx(0.1)
    assert row["AVG_DAILY_CREDITS"] == pytest.approx(1.0)


def test_derived_daily_matches_the_sql_contract_columns():
    live_cols = _select_columns(cortex_sql.cortex_code_daily(30, "ALL"))
    out = daily_from_user_daily(_user_daily(), 30)
    assert list(out.columns) == live_cols
    assert len(out) == 3 and out["ACTIVE_USERS"].max() == 1


def test_window_slice_drops_days_outside_the_asked_window():
    frame = pd.concat([_user_daily((0, 1)), _user_daily((200,))], ignore_index=True)
    assert rollup_from_user_daily(frame, 7).iloc[0]["ACTIVE_DAYS"] == 2
    assert rollup_from_user_daily(frame, 365).iloc[0]["ACTIVE_DAYS"] == 3


# ---- C7: classifier corrections -------------------------------------------

def test_projection_divisor_clamps_to_days_since_first_usage():
    # 3 observed days inside a 365d ask: dividing by 365 would report ~1/120th
    # of the real burn and the budget ladder would never fire.
    rollup = rollup_from_user_daily(_user_daily(), 365)
    assert effective_window_days(rollup, 365) == 3
    assert effective_window_days(rollup, 2) == 2          # never widens the ask
    enriched = enrich_user_rollup(rollup, 2.20, 365)
    assert enriched.iloc[0]["PROJECTED_30D_CREDITS"] == pytest.approx(30.0)  # 3cr/3d * 30
    # the KPI row must use the same divisor as the per-user rows
    assert rollup_summary(enriched, 365)["window_days"] == 3


def test_cpr_spike_needs_a_real_sample_and_real_money():
    # One request at 0.5 cr/request is $1.10 a month — not an exception.
    tiny = enrich_user_rollup(_rollup(TOTAL_REQUESTS=1, TOTAL_CREDITS=0.5,
                                      CREDITS_PER_REQUEST=0.5), 2.20, 30)
    assert classify_exceptions(tiny, 0.0, 2.20).empty
    # Enough requests but trivial money is still not worth an operator's time.
    cheap = enrich_user_rollup(_rollup(TOTAL_REQUESTS=CPR_MIN_REQUESTS, TOTAL_CREDITS=2.2,
                                       CREDITS_PER_REQUEST=0.11), 2.20, 30)
    assert cheap.iloc[0]["PROJECTED_30D_USD"] < CPR_MIN_PROJECTED_USD
    assert classify_exceptions(cheap, 0.0, 2.20).empty
    # Both floors cleared -> the spike is real.
    real = enrich_user_rollup(_rollup(TOTAL_REQUESTS=200, TOTAL_CREDITS=30.0,
                                      CREDITS_PER_REQUEST=0.15), 2.20, 30)
    assert classify_exceptions(real, 0.0, 2.20).iloc[0]["SIGNAL"] == "Cost per request spike"


def test_aggregate_budget_breach_is_reported_even_when_no_user_breaches():
    # Ten users at 20% of budget each = 200% of budget. Every per-user rule
    # stays silent; the scope total must not.
    frames = [_rollup(USER_NAME=f"U{i}", TOTAL_CREDITS=18.0) for i in range(10)]
    enriched = enrich_user_rollup(pd.concat(frames, ignore_index=True), 2.20, 30)
    budget = 200.0                       # 180 cr projected total vs ~90.9 cr budget
    per_user = classify_exceptions(enriched, budget, 2.20)
    assert per_user.empty                # 18cr each is under 25% of 90.9cr
    summary = rollup_summary(enriched, 30)
    assert summary["projected_30d_usd"] > budget
    out = with_aggregate_budget_row(per_user, summary, budget)
    assert len(out) == 1
    assert out.iloc[0]["SEVERITY"] == "Critical"
    assert out.iloc[0]["USER_NAME"] == "(all users)"
    # the columns the panel table and the Action Queue insert both read
    for col in ("SIGNAL", "SOURCE", "TOTAL_REQUESTS", "CREDITS_PER_REQUEST", "PROJECTED_30D_USD"):
        assert col in out.columns


def test_aggregate_row_leads_and_never_fires_without_a_budget():
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=300.0), 2.20, 30)
    summary = rollup_summary(enriched, 30)
    per_user = classify_exceptions(enriched, 440.0, 2.20)
    out = with_aggregate_budget_row(per_user, summary, 440.0)
    assert out.iloc[0]["USER_NAME"] == "(all users)"     # headline first
    assert len(out) == len(per_user) + 1
    assert with_aggregate_budget_row(per_user, summary, 0.0).equals(per_user)
