"""Wave-3 efficiency features (repo review): idle-waste roll-up, per-warehouse
health chip, cross-sectional cost-per-query peer z-score."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.cost_peers import cost_per_query_peers
from app.logic.insights import idle_waste_summary
from app.logic.wh_health import warehouse_health

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------ idle waste ----

def test_idle_waste_summary_prices_and_projects():
    df = pd.DataFrame({"WAREHOUSE_NAME": ["A", "B"],
                       "TOTAL_CREDITS": [100.0, 300.0], "IDLE_CREDITS": [40.0, 60.0]})
    s = idle_waste_summary(df, credit_rate_usd=3.0, window_days=10)
    assert s["IDLE_USD"] == 300.0            # (40+60) * 3
    assert s["IDLE_SHARE_PCT"] == 25.0       # 100 idle / 400 total
    assert s["PROJECTED_MONTHLY_USD"] == 900.0   # 300 / 10 * 30
    assert s["WAREHOUSES"] == 2


def test_idle_waste_summary_empty_and_zero_total_safe():
    assert idle_waste_summary(None, 3.0, 30)["IDLE_USD"] == 0.0
    assert idle_waste_summary(pd.DataFrame(), 3.0, 30)["WAREHOUSES"] == 0
    z = idle_waste_summary(pd.DataFrame({"TOTAL_CREDITS": [0.0], "IDLE_CREDITS": [0.0]}), 3.0, 30)
    assert z["IDLE_SHARE_PCT"] == 0.0        # no div-by-zero


# --------------------------------------------------------- health chip ----

def test_warehouse_health_itemizes_penalties():
    sized = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_BAD", "QUEUED_MIN_PER_DAY": 40.0, "SPILL_GB_PER_DAY": 6.0,
        "P95_ELAPSED_SEC": 240.0, "IDLE_PCT": 10.0, "ACTIVE_DAYS_PER_30D": 20.0,
        "EVIDENCE_SUFFICIENT": True,
    }])
    h = warehouse_health(sized).iloc[0]
    assert h["SCORE"] < 60 and h["GRADE"] in ("Degraded", "At risk")
    # each firing penalty is named.
    assert "queued" in h["WHY"] and "spill" in h["WHY"] and "runtime" in h["WHY"]


def test_healthy_warehouse_scores_100():
    sized = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_OK", "QUEUED_MIN_PER_DAY": 0.0, "SPILL_GB_PER_DAY": 0.0,
        "P95_ELAPSED_SEC": 8.0, "IDLE_PCT": 20.0, "ACTIVE_DAYS_PER_30D": 25.0,
        "EVIDENCE_SUFFICIENT": True,
    }])
    h = warehouse_health(sized).iloc[0]
    assert h["SCORE"] == 100 and h["GRADE"] == "Healthy" and h["WHY"] == "no penalties"


def test_low_util_penalty_is_evidence_gated():
    # 90% idle but only 1 active day -> the low-util penalty must NOT fire (a sparse
    # nightly-batch WH is not "At risk" just for being idle between runs).
    sized = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_BATCH", "QUEUED_MIN_PER_DAY": 0.0, "SPILL_GB_PER_DAY": 0.0,
        "P95_ELAPSED_SEC": 5.0, "IDLE_PCT": 90.0, "ACTIVE_DAYS_PER_30D": 1.0,
        "EVIDENCE_SUFFICIENT": False,
    }])
    h = warehouse_health(sized).iloc[0]
    assert h["SCORE"] == 100 and "utilization" not in h["WHY"]
    assert warehouse_health(None).empty


# ----------------------------------------------------------- peer z ----

def test_cost_per_query_peers_flags_the_expensive_outlier():
    # 6 cheap peers (~$0.01/q) + one 10x-expensive warehouse -> the pricey one is the outlier.
    rows = [{"WAREHOUSE_NAME": f"WH{i}", "COMPANY": "ALFA",
             "CREDITS_TOTAL": 10.0, "QUERY_COUNT": 1000.0} for i in range(6)]
    rows.append({"WAREHOUSE_NAME": "WH_PRICEY", "COMPANY": "ALFA",
                 "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 1000.0})   # 10x $/query
    peers = cost_per_query_peers(pd.DataFrame(rows), rate_usd=3.0)
    top = peers.iloc[0]
    assert top["WAREHOUSE_NAME"] == "WH_PRICEY" and bool(top["IS_OUTLIER"])
    assert top["MULT_OF_MEDIAN"] >= 9.0


def test_cost_per_query_peers_materiality_and_safety():
    # a 3-query sandbox is dropped by the floor (no div-by-zero, no false outlier).
    df = pd.DataFrame({"WAREHOUSE_NAME": ["TINY"], "COMPANY": ["A"],
                       "CREDITS_TOTAL": [50.0], "QUERY_COUNT": [3.0]})
    assert cost_per_query_peers(df).empty
    assert cost_per_query_peers(None).empty
    # a 0-query warehouse never divides by zero (floored out).
    z = pd.DataFrame({"WAREHOUSE_NAME": ["Z"], "COMPANY": ["A"],
                      "CREDITS_TOTAL": [10.0], "QUERY_COUNT": [0.0]})
    assert cost_per_query_peers(z).empty


def test_health_reads_real_size_recommendations_output():
    # Review #2 (top risk): lock the column CONTRACT. Pipe a raw warehouse profile
    # through the REAL size_recommendations, then warehouse_health — so a rename in
    # sizing.py (which would silently zero a penalty via safe_float(None)=0) fails here,
    # not just in a hand-built fixture.
    from app.logic.sizing import size_recommendations
    profile = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_REAL", "CREDITS_TOTAL": 500.0, "QUERY_COUNT": 5000.0,
        "ACTIVE_QUERY_DAYS": 8.0, "P95_ELAPSED_SEC": 240.0,
        "QUEUED_SEC": 40 * 60 * 10, "SPILL_REMOTE_GB": 6.0 * 10, "IDLE_PCT": 10.0,
    }])
    sized = size_recommendations(profile, credit_rate_usd=3.0, window_days=10)
    # sanity: the derived per-day columns the chip reads actually exist + are right.
    assert float(sized.iloc[0]["QUEUED_MIN_PER_DAY"]) == 40.0
    assert float(sized.iloc[0]["SPILL_GB_PER_DAY"]) == 6.0
    h = warehouse_health(sized).iloc[0]
    assert h["SCORE"] < 60                       # penalties actually fired end-to-end
    assert "queued" in h["WHY"] and "spill" in h["WHY"] and "runtime" in h["WHY"]


def test_peers_small_fleet_surfaces_the_multiple_even_when_z_is_zero():
    # <5 warehouses -> robust z is all-zero (no flag), but the priciest must still sort
    # to the top by its multiple-of-median (review #4).
    rows = [{"WAREHOUSE_NAME": "A", "COMPANY": "X", "CREDITS_TOTAL": 10.0, "QUERY_COUNT": 1000.0},
            {"WAREHOUSE_NAME": "B", "COMPANY": "X", "CREDITS_TOTAL": 12.0, "QUERY_COUNT": 1000.0},
            {"WAREHOUSE_NAME": "PRICEY", "COMPANY": "X", "CREDITS_TOTAL": 90.0, "QUERY_COUNT": 1000.0}]
    peers = cost_per_query_peers(pd.DataFrame(rows), rate_usd=3.0)
    assert (peers["Z_SCORE"] == 0.0).all()       # too few to z-score
    assert peers.iloc[0]["WAREHOUSE_NAME"] == "PRICEY"   # still surfaced by the multiple
    assert peers.iloc[0]["MULT_OF_MEDIAN"] >= 5.0


def test_wave3_panels_are_wired():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "idle_waste_summary(" in opt and "Idle credit waste" in opt
    ops = _src("app/ui/pages/operations.py")
    assert "warehouse_health(_sized)" in ops and "cost_per_query_peers(_prof.df" in ops
    assert "Cost-per-query outliers (vs the fleet)" in ops
