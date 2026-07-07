import pandas as pd

from app.data import insights_sql
from app.logic.sizing import (
    RECOMMEND_DOWN,
    RECOMMEND_KEEP,
    RECOMMEND_SUSPEND,
    RECOMMEND_UP,
    size_recommendations,
    sizing_summary,
)


def _wh(name, credits=100.0, queued_sec=0.0, spill=0.0, p95=5.0, idle=0.0, queries=1000):
    return {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "CREDITS_TOTAL": credits,
            "QUERY_COUNT": queries, "P95_ELAPSED_SEC": p95, "QUEUED_SEC": queued_sec,
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


def test_scenario_math_and_saving():
    df = pd.DataFrame([_wh("W", credits=70.0, p95=3.0, idle=40.0)])  # down candidate
    out = size_recommendations(df, credit_rate_usd=3.68, window_days=7)
    row = out.iloc[0]
    assert row["MONTHLY_USD_NOW"] == round(70 * 3.68 / 7 * 30, 0)
    assert row["SCENARIO_DOWN_USD"] == round(row["MONTHLY_USD_NOW"] * 0.5, 0)
    assert row["SCENARIO_UP_USD"] == round(row["MONTHLY_USD_NOW"] * 2.0, 0)
    assert row["POTENTIAL_MONTHLY_SAVING_USD"] == row["MONTHLY_USD_NOW"] - row["SCENARIO_DOWN_USD"]
    summary = sizing_summary(out)
    assert summary["down"] == 1 and summary["potential_saving_usd"] > 0


def test_empty_safe():
    assert size_recommendations(pd.DataFrame(), 3.68, 7).empty
    assert sizing_summary(pd.DataFrame())["potential_saving_usd"] == 0.0


def test_sizing_profile_sql_invariants():
    sql = insights_sql.warehouse_sizing_profile(7, "Trexis")
    assert "WAREHOUSE_METERING_HISTORY" in sql and "IDLE_PCT" in sql
    assert "IN ('WH_TRXS_LOAD'" in sql


def test_query_detail_validates_id():
    import pytest

    good = insights_sql.query_detail("01b2c3d4-0000-1111-2222-333344445555")
    assert "QUERY_TEXT" in good and "01b2c3d4" in good
    with pytest.raises(ValueError):
        insights_sql.query_detail("x'; DROP TABLE q;--")
    with pytest.raises(ValueError):
        insights_sql.query_detail("short")
