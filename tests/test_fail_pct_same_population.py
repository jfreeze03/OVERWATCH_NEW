"""R1-083: the fix queue's failure rate divides the family mart's FAILS by the same family's runs.

FAILS comes from MART_QUERY_FAMILY_DAILY (every QUERY_HISTORY row) but FAIL_PCT divided it by
MART_PATTERN_COST_DAILY's RUNS (warehouse-attributed runs only), so the rate read high: past 100% (the
evidence line said "150.0% fail"), or across the 2% "Stabilize failures" gate on a 1% family. That
diagnosis is priced (PRICED_USD_MO) and Track writes the price to ACTION_QUEUE.ESTIMATED_USD.
"""

from __future__ import annotations

import math

import pandas as pd

from app.logic.decision import prioritize_workloads
from app.logic.fix_queue import diagnose_workloads, track_items


def _row(fp: str, **kw) -> dict:
    base = {"FINGERPRINT": fp, "RUNS": 600, "FAILS": 0, "CREDITS": 100.0, "ACTIVE_DAYS": 30, "DATABASES": 1,
            "USERS": 2, "TOTAL_ELAPSED_HOURS": 1.0, "AVG_CACHE_PCT": 50.0, "P95_SEC": 30.0,
            "QUERY_PREVIEW": "SELECT 1", "TOTAL_ELAPSED_SEC": 6000.0, "FAMILY_RUNS": 600, "TOTAL_EXEC_SEC": 5900.0,
            "TOTAL_COMPILE_SEC": 60.0, "GB_SCANNED": 1.0, "WAREHOUSES": 1, "COMPILE_RUN_PCT": 0.0,
            "COMPILE_DOMINANT_RUN_PCT": 0.0, "TOP_COMPANY": "ALFA", "TOP_DATABASE": "DB1", "OW_SELF": False}
    base.update(kw)
    return base


def _queue(*rows: dict) -> pd.DataFrame:
    return diagnose_workloads(prioritize_workloads(pd.DataFrame(rows), 3.0, 30)).set_index("FINGERPRINT")


def test_fail_pct_is_fails_over_family_runs_and_the_price_stays_below_observed_cost():
    out = _queue(_row("thin", RUNS=10, FAILS=15, FAMILY_RUNS=40, CREDITS=50.0))
    r = out.loc["thin"]
    assert r["FAIL_PCT"] == 37.5                                # 15 / 40, never 15 / 10 = 150%
    assert "150" not in r["EVIDENCE"] and "37.5% fail" in r["EVIDENCE"]
    assert r["DIAGNOSIS"] == "Stabilize failures"
    assert math.isclose(r["PRICED_USD_MO"], 56.25) and r["PRICED_USD_MO"] < r["IMPACT_USD_30D"]
    (item,) = track_items(out.reset_index().loc[[0]], "ALFA")
    assert math.isclose(item["ESTIMATED_USD"], 56.25)


def test_a_one_percent_family_is_not_stabilize_failures():
    # 10 of 1000 family runs failed (1%); the pattern mart saw 100 runs -> the old cross-population 10%
    r = _queue(_row("one_pct", RUNS=100, FAILS=10, FAMILY_RUNS=1000)).loc["one_pct"]
    assert r["FAIL_PCT"] == 1.0
    assert r["NEXT_MOVE"] != "Stabilize failures" and r["DIAGNOSIS"] != "Stabilize failures"
    assert math.isnan(r["PRICED_USD_MO"])


def test_runs_is_the_fallback_denominator_and_the_rate_is_bounded():
    out = prioritize_workloads(pd.DataFrame([
        _row("no_family", RUNS=50, FAILS=5, FAMILY_RUNS=float("nan")),
        _row("zero_family", RUNS=50, FAILS=5, FAMILY_RUNS=0),
        _row("over", RUNS=10, FAILS=15, FAMILY_RUNS=float("nan")),
    ]), 3.0, 30).set_index("FINGERPRINT")
    assert out.loc["no_family", "FAIL_PCT"] == 10.0 and out.loc["zero_family", "FAIL_PCT"] == 10.0
    assert out.loc["over", "FAIL_PCT"] == 100.0
    # the non-advisor workload_portfolio shape has no FAMILY_RUNS column at all
    shape = {k: v for k, v in _row("p", RUNS=20, FAILS=1).items() if k != "FAMILY_RUNS"}
    plain = prioritize_workloads(pd.DataFrame([shape]), 3.0, 30)
    assert plain.iloc[0]["FAIL_PCT"] == 5.0
