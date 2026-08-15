"""Tests for the ported AI/Cortex user-attribution feature."""

import re
from datetime import timedelta

import pandas as pd
import pytest

from app.data import cortex_sql, mart27_sql
from app.logic.cortex import (
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


def test_cost_per_request_spike_flags_a_cohort_outlier():
    # A spike is unusual FOR THE COHORT: five baseline users at ~0.05 cr/req plus
    # one at 0.25. Only the outlier flags; the baseline users (normal rate) do not
    # — the old flat > 0.10 rule flagged all-or-none regardless of the cohort.
    base = [_rollup(USER_NAME=f"U{i}", TOTAL_REQUESTS=200, TOTAL_CREDITS=10.0,
                    CREDITS_PER_REQUEST=0.05) for i in range(5)]
    outlier = _rollup(USER_NAME="SPIKE", TOTAL_REQUESTS=200, TOTAL_CREDITS=50.0,
                      CREDITS_PER_REQUEST=0.25)
    enriched = enrich_user_rollup(pd.concat([*base, outlier], ignore_index=True), 2.20, 30)
    out = classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20)
    spikes = out[out["SIGNAL"] == "Cost per request spike"]
    assert list(spikes["USER_NAME"]) == ["SPIKE"]
    assert spikes.iloc[0]["SEVERITY"] == "High"


def test_uniformly_priced_cohort_has_no_spikes():
    # The KEY fix: a cohort of heavy-but-normal users all at the same rate has no
    # outlier, so nothing is flagged. The flat > 0.10 rule flagged every one of
    # them permanently.
    users = [_rollup(USER_NAME=f"U{i}", TOTAL_REQUESTS=500, TOTAL_CREDITS=75.0,
                     CREDITS_PER_REQUEST=0.15) for i in range(6)]
    enriched = enrich_user_rollup(pd.concat(users, ignore_index=True), 2.20, 30)
    out = classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20)
    assert out.empty


def test_cpr_spike_needs_five_peers_to_have_a_cohort():
    # With fewer than five eligible peers there is no cohort to be an outlier of,
    # so even a lone expensive user is not called a spike (honest — a spike is
    # relative).
    base = [_rollup(USER_NAME=f"U{i}", TOTAL_REQUESTS=200, TOTAL_CREDITS=10.0,
                    CREDITS_PER_REQUEST=0.05) for i in range(3)]
    outlier = _rollup(USER_NAME="SPIKE", TOTAL_REQUESTS=200, TOTAL_CREDITS=50.0,
                      CREDITS_PER_REQUEST=0.25)
    enriched = enrich_user_rollup(pd.concat([*base, outlier], ignore_index=True), 2.20, 30)
    assert classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20).empty


def test_no_budget_means_no_budget_severities():
    enriched = enrich_user_rollup(_rollup(TOTAL_CREDITS=30000.0), 2.20, 30)
    out = classify_exceptions(enriched, ai_budget_usd=0.0, ai_rate_usd=2.20)
    assert out.empty  # huge usage, but no configured budget and no CPR spike


def test_exceptions_ranked_critical_first():
    frames = [
        _rollup(USER_NAME="A", TOTAL_CREDITS=300.0),      # breach (>200 cr budget)
        _rollup(USER_NAME="B", TOTAL_CREDITS=120.0),      # concentration (High, >50%)
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


def test_cpr_spike_is_cohort_relative_and_needs_real_money():
    # Baseline cohort at ~0.02 cr/req. A user with huge spend but the SAME rate is
    # not a spike (relativity — the flat rule missed this). A one-request 0.5
    # cr/req user has no sample to trend. Only a genuine positive outlier that also
    # clears the >=20-request and >=$10-projected floors is flagged.
    base = [_rollup(USER_NAME=f"B{i}", TOTAL_REQUESTS=300, TOTAL_CREDITS=6.0,
                    CREDITS_PER_REQUEST=0.02) for i in range(5)]
    heavy_normal = _rollup(USER_NAME="HEAVY", TOTAL_REQUESTS=5000, TOTAL_CREDITS=100.0,
                           CREDITS_PER_REQUEST=0.02)   # huge spend, normal RATE
    tiny = _rollup(USER_NAME="TINY", TOTAL_REQUESTS=1, TOTAL_CREDITS=0.5,
                   CREDITS_PER_REQUEST=0.5)            # high rate, no sample
    real = _rollup(USER_NAME="REAL", TOTAL_REQUESTS=200, TOTAL_CREDITS=30.0,
                   CREDITS_PER_REQUEST=0.15)           # genuine outlier + real money
    enriched = enrich_user_rollup(
        pd.concat([*base, heavy_normal, tiny, real], ignore_index=True), 2.20, 30)
    spikes = classify_exceptions(enriched, 0.0, 2.20)
    spikes = spikes[spikes["SIGNAL"] == "Cost per request spike"]
    assert list(spikes["USER_NAME"]) == ["REAL"]


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


def test_rec38_per_user_window_and_small_n_guard():
    # rec #38: each user projects against their OWN observable window, and a
    # brand-new user (< the small-N floor) never fires the budget ladder.
    today = account_today()
    df = pd.DataFrame({
        "USER_NAME": ["OLD", "NEW_HEAVY", "BRAND_NEW"],
        "TOTAL_CREDITS": [30.0, 20.0, 10.0],
        "TOTAL_REQUESTS": [300, 200, 100],
        "FIRST_USAGE": [
            pd.Timestamp(today - timedelta(days=29)),  # 30 observable days
            pd.Timestamp(today - timedelta(days=9)),   # 10 observable days (heavy + new)
            pd.Timestamp(today - timedelta(days=1)),   # 2 observable days (guarded)
        ],
    })
    enriched = enrich_user_rollup(df, ai_rate_usd=2.20, window_days=30)
    obs = dict(zip(enriched["USER_NAME"], enriched["OBSERVABLE_DAYS"], strict=True))
    assert obs == {"OLD": 30, "NEW_HEAVY": 10, "BRAND_NEW": 2}
    proj = dict(zip(enriched["USER_NAME"], enriched["PROJECTED_30D_CREDITS"], strict=True))
    # NEW_HEAVY projects on its own 10-day window (20/10*30 = 60), not the scope max
    # (20/30*30 = 20) — the under-projection the fix removes.
    assert proj["NEW_HEAVY"] == pytest.approx(60.0)
    assert proj["OLD"] == pytest.approx(30.0)
    # budget_credits = 100/2.20 = 45.45: NEW_HEAVY (60) breaches; BRAND_NEW would
    # project 10/2*30 = 150 but is guarded out for having < 4 observable days.
    exc = classify_exceptions(enriched, ai_budget_usd=100.0, ai_rate_usd=2.20)
    breached = set(exc["USER_NAME"]) if not exc.empty else set()
    assert "NEW_HEAVY" in breached and "BRAND_NEW" not in breached
