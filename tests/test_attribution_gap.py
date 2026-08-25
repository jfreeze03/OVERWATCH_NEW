"""Billed-vs-attributed gap panel (F12): pure logic + wiring.

attribution_gap / attribution_gap_trend (app/logic/cost_coverage.py) split the
already-loaded metering frame into company-attributable warehouse spend vs the
unattributed gap. App-only — no SQL builder, no migration.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.cost_coverage import attribution_gap, attribution_gap_trend

_ROOT = Path(__file__).resolve().parents[1]


def _frame() -> pd.DataFrame:
    # billed 39000; attributed (Warehouse + reader) 34000; gap 5000 -> coverage 87.2%
    return pd.DataFrame([
        {"DAY": "2026-08-01", "CATEGORY": "Warehouse", "USD": 20000.0},
        {"DAY": "2026-08-01", "CATEGORY": "Warehouse (reader)", "USD": 0.0},
        {"DAY": "2026-08-01", "CATEGORY": "Serverless", "USD": 2000.0},
        {"DAY": "2026-08-02", "CATEGORY": "Warehouse", "USD": 14000.0},
        {"DAY": "2026-08-02", "CATEGORY": "Serverless", "USD": 1200.0},
        {"DAY": "2026-08-02", "CATEGORY": "AI / Cortex", "USD": 1800.0},
    ])


def test_gap_totals_coverage_and_breakdown():
    summary, breakdown = attribution_gap(_frame())
    assert summary["billed_usd"] == 39000.0
    assert summary["attributed_usd"] == 34000.0
    assert summary["gap_usd"] == 5000.0
    assert round(summary["coverage_pct"], 1) == 87.2
    # one row per non-warehouse category, largest first; shares of the gap sum to 100
    assert list(breakdown["CATEGORY"]) == ["Serverless", "AI / Cortex"]
    assert breakdown.iloc[0]["GAP_USD"] == 3200.0
    assert round(breakdown["SHARE_PCT"].sum(), 0) == 100.0


def test_gap_empty_and_zero_no_divzero():
    summary, breakdown = attribution_gap(pd.DataFrame())
    assert summary == {"billed_usd": 0.0, "attributed_usd": 0.0, "gap_usd": 0.0, "coverage_pct": 0.0}
    assert breakdown.empty
    z = pd.DataFrame([{"CATEGORY": "Warehouse", "USD": 0.0},
                      {"CATEGORY": "Serverless", "USD": 0.0}])
    assert attribution_gap(z)[0]["coverage_pct"] == 0.0


def test_only_own_warehouse_is_full_coverage():
    f = pd.DataFrame([{"CATEGORY": "Warehouse", "USD": 100.0},
                      {"CATEGORY": "Warehouse", "USD": 50.0}])
    summary, breakdown = attribution_gap(f)
    assert summary["coverage_pct"] == 100.0 and summary["gap_usd"] == 0.0
    assert breakdown.empty


def test_reader_metering_falls_into_the_gap():
    # reader-account metering has no company key here (COMPANY_FOR_WAREHOUSE resolves
    # only this account's warehouses) -> unattributed, matching the drill-coverage
    # table which also marks reader non-drillable (rec #43).
    f = pd.DataFrame([{"CATEGORY": "Warehouse", "USD": 100.0},
                      {"CATEGORY": "Warehouse (reader)", "USD": 50.0},
                      {"CATEGORY": "Serverless", "USD": 50.0}])
    summary, breakdown = attribution_gap(f)
    assert summary["attributed_usd"] == 100.0          # own warehouse only
    assert summary["gap_usd"] == 100.0                 # reader 50 + serverless 50
    assert summary["coverage_pct"] == 50.0
    assert set(breakdown["CATEGORY"]) == {"Warehouse (reader)", "Serverless"}


def test_gap_trend_per_day_and_guard():
    trend = attribution_gap_trend(_frame())
    assert list(trend["DAY"]) == ["2026-08-01", "2026-08-02"]
    row1 = trend[trend["DAY"] == "2026-08-01"].iloc[0]
    assert row1["BILLED_USD"] == 22000.0 and row1["GAP_USD"] == 2000.0
    assert round(row1["GAP_PCT"], 2) == 9.09     # 2000 / 22000
    assert attribution_gap_trend(pd.DataFrame()).empty


def test_gap_trend_zero_billed_day_no_divzero():
    f = pd.DataFrame([{"DAY": "2026-08-03", "CATEGORY": "Serverless", "USD": 0.0}])
    assert attribution_gap_trend(f).iloc[0]["GAP_PCT"] == 0.0


def test_gap_panel_wired_into_spend():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "attribution_gap(" in src and "Billed vs attributed" in src
    assert "attribution_gap_trend(" in src
