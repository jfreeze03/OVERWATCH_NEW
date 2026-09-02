import pandas as pd

from app.data import insights_sql
from app.logic.sizing import (
    RECOMMEND_CADENCE,
    RECOMMEND_DOWN,
    RECOMMEND_KEEP,
    RECOMMEND_OBSERVE,
    RECOMMEND_SUSPEND,
    RECOMMEND_UP,
    size_recommendations,
    sizing_summary,
)


def _wh(name, credits=100.0, queued_sec=0.0, spill=0.0, p95=5.0, idle=0.0,
        queries=1000, active_days=7):
    return {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "CREDITS_TOTAL": credits,
            "QUERY_COUNT": queries, "ACTIVE_QUERY_DAYS": active_days,
            "P95_ELAPSED_SEC": p95, "QUEUED_SEC": queued_sec,
            "SPILL_REMOTE_GB": spill, "IDLE_PCT": idle}


def test_rules():
    df = pd.DataFrame([
        _wh("QUEUED", queued_sec=7 * 45 * 60),          # 45 min/day queued -> up
        _wh("SPILLY", spill=9.0),                        # spill -> up
        _wh("CALM_IDLE", p95=3.0, idle=40.0),            # fast + idle -> down
        _wh("MOSTLY_IDLE", idle=80.0),                   # suspend first
        _wh("BUSY_FIT", p95=30.0, idle=5.0),             # keep
    ])
    out = size_recommendations(df, credit_rate_usd=3.68, window_days=7)
    rec = dict(zip(out["WAREHOUSE_NAME"], out["RECOMMENDATION"], strict=True))
    assert rec["QUEUED"] == RECOMMEND_UP
    assert rec["SPILLY"] == RECOMMEND_UP
    assert rec["CALM_IDLE"] == RECOMMEND_DOWN
    assert rec["MOSTLY_IDLE"] == RECOMMEND_SUSPEND
    assert rec["BUSY_FIT"] == RECOMMEND_KEEP
    # pressure first in the ordering
    assert out.iloc[0]["RECOMMENDATION"] == RECOMMEND_UP
    # D2: a reversible timer fix outranks the speculative size-down bet
    order = list(out["RECOMMENDATION"])
    assert order.index(RECOMMEND_SUSPEND) < order.index(RECOMMEND_DOWN)


def test_spill_signal_is_per_day_not_a_window_total():
    """D2: spill was a WINDOW TOTAL sitting next to a per-day queue threshold,
    so the same warehouse crossed it at 90d and not at 7d."""
    df = pd.DataFrame([_wh("SPILLY", spill=9.0)])     # 9 GB of remote spill
    out7 = size_recommendations(df, 3.68, 7)
    assert out7.iloc[0]["SPILL_GB_PER_DAY"] == 1.29
    assert out7.iloc[0]["RECOMMENDATION"] == RECOMMEND_UP    # 1.29 GB/day is pressure
    out90 = size_recommendations(df, 3.68, 90)
    assert out90.iloc[0]["SPILL_GB_PER_DAY"] == 0.1
    assert out90.iloc[0]["RECOMMENDATION"] != RECOMMEND_UP   # same 9 GB over 90d is noise


def test_provisioning_time_is_not_a_size_up_signal():
    """D2: QUEUED_PROVISIONING_SEC is a warehouse waking up. Sizing up to fix
    resume overhead buys nothing — it belongs in the suspend rationale."""
    df = pd.DataFrame([{**_wh("WAKER", idle=70.0), "QUEUED_PROVISIONING_SEC": 7 * 40 * 60}])
    row = size_recommendations(df, 3.68, 7).iloc[0]
    assert row["RECOMMENDATION"] == RECOMMEND_SUSPEND
    assert "provisioning" in row["RATIONALE"]
    assert row["PROVISION_MIN_PER_DAY"] == 40.0


def test_suspend_rows_carry_measured_idle_dollars():
    """D2: the SUSPEND recommendation had no number next to it while the
    speculative half-rate scenario got the KPI."""
    df = pd.DataFrame([_wh("MOSTLY_IDLE", credits=70.0, idle=80.0)])
    out = size_recommendations(df, 3.68, 7)
    row = out.iloc[0]
    assert row["IDLE_MONTHLY_USD"] == round(row["MONTHLY_USD_NOW"] * 0.8, 0)
    summary = sizing_summary(out)
    assert summary["idle_saving_usd"] == row["IDLE_MONTHLY_USD"]
    assert summary["potential_saving_usd"] == 0.0    # kept apart: model vs measurement


def test_scenario_math_and_saving():
    df = pd.DataFrame([_wh("W", credits=70.0, p95=3.0, idle=40.0)])  # down candidate
    out = size_recommendations(df, credit_rate_usd=3.68, window_days=7)
    row = out.iloc[0]
    assert row["MONTHLY_USD_NOW"] == round(70 * 3.68 / 7 * 30, 0)
    assert row["SCENARIO_DOWN_USD"] == round(row["MONTHLY_USD_NOW"] * 0.5, 0)
    assert row["SCENARIO_UP_USD"] == round(row["MONTHLY_USD_NOW"] * 2.0, 0)
    # rec #13: headline saving is the conservative floor (only idle reliably shrinks),
    # not the old 0.5*bill; SAVING_HIGH is the optimistic bound (busy halves too).
    assert row["SAVING_LOW_USD"] == round(row["IDLE_MONTHLY_USD"] * 0.5, 0)
    assert row["SAVING_HIGH_USD"] == round(row["MONTHLY_USD_NOW"] * 0.5, 0)
    assert row["POTENTIAL_MONTHLY_SAVING_USD"] == row["SAVING_LOW_USD"]
    assert row["SAVING_LOW_USD"] <= row["SAVING_HIGH_USD"]
    summary = sizing_summary(out)
    assert summary["down"] == 1 and summary["potential_saving_usd"] > 0
    assert summary["potential_saving_high_usd"] >= summary["potential_saving_usd"]


def test_resize_advice_is_withheld_for_one_off_pressure_and_savings():
    df = pd.DataFrame([
        _wh("ONE_OFF_UP", queued_sec=7 * 60 * 60, active_days=1),
        _wh("ONE_OFF_DOWN", p95=2.0, idle=40.0, active_days=1),
    ])
    out = size_recommendations(df, 3.68, 7).set_index("WAREHOUSE_NAME")
    assert out.loc["ONE_OFF_UP", "RECOMMENDATION"] == RECOMMEND_OBSERVE
    assert out.loc["ONE_OFF_DOWN", "RECOMMENDATION"] == RECOMMEND_OBSERVE
    assert not bool(out.loc["ONE_OFF_UP", "ACTIONABLE"])
    assert out.loc["ONE_OFF_DOWN", "POTENTIAL_MONTHLY_SAVING_USD"] == 0.0


def test_already_short_timer_routes_high_idle_to_cadence_not_timer_tuning():
    row = {**_wh("TUNED_IDLE", idle=80.0), "AUTO_SUSPEND": 30,
           "AUTO_SUSPEND_KNOWN": True}
    result = size_recommendations(pd.DataFrame([row]), 3.68, 7).iloc[0]
    assert result["RECOMMENDATION"] == RECOMMEND_CADENCE
    assert not bool(result["ACTIONABLE"])
    assert sizing_summary(pd.DataFrame([result]))["idle_saving_usd"] == 0.0


def test_empty_safe():
    assert size_recommendations(pd.DataFrame(), 3.68, 7).empty
    assert sizing_summary(pd.DataFrame())["potential_saving_usd"] == 0.0


def test_sizing_profile_sql_invariants():
    sql = insights_sql.warehouse_sizing_profile(7, "Trexis")
    assert "WAREHOUSE_METERING_HISTORY" in sql and "IDLE_PCT" in sql
    assert "COUNT(DISTINCT DATE(START_TIME)) AS ACTIVE_QUERY_DAYS" in sql
    # round-16: scoped by the COMPANY_FOR_WAREHOUSE UDF (matching the eff_sizing_profile
    # mart twin + the builder's own COMPANY label), not the name pattern
    assert "COMPANY_FOR_WAREHOUSE(M.WAREHOUSE_NAME) = 'Trexis'" in sql
    assert "IN ('WH_TRXS_LOAD'" not in sql


def test_query_detail_validates_id():
    import pytest

    good = insights_sql.query_detail("01b2c3d4-0000-1111-2222-333344445555")
    assert "QUERY_TEXT" in good and "01b2c3d4" in good
    with pytest.raises(ValueError):
        insights_sql.query_detail("x'; DROP TABLE q;--")
    with pytest.raises(ValueError):
        insights_sql.query_detail("short")
