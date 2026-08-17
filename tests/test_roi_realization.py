"""Decision Studio flagship: the ROI / realization story is a first-class section
(#40/#31/#19). Verified savings run-rate (by month) + realized savings by lever,
derived from the SAVINGS_LEDGER the app already books."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic.actions import savings_by_lever, savings_by_month

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _ledger(rows):
    cols = ["STATE", "ESTIMATED_USD", "VERIFIED_USD", "VERIFIED_AT", "FINDING_TYPE"]
    return pd.DataFrame(rows, columns=cols)


def test_savings_by_month_run_rate_verified_only():
    df = _ledger([
        ("VERIFIED", 100.0, 90.0, "2026-06-15", "warehouse_idle"),
        ("VERIFIED", 50.0, 60.0, "2026-06-20", "retention"),
        ("VERIFIED", 200.0, 180.0, "2026-07-03", "warehouse_idle"),
        ("ESTIMATED", 999.0, None, None, "retention"),          # excluded (not verified)
        ("VERIFIED", 10.0, 12.0, None, "x"),                    # excluded (no VERIFIED_AT)
    ])
    m = savings_by_month(df)
    assert list(m["MONTH"]) == ["2026-06", "2026-07"]           # oldest-first, time-ordered
    assert float(m[m["MONTH"] == "2026-06"]["VERIFIED_USD"].iloc[0]) == 150.0   # 90 + 60
    assert float(m[m["MONTH"] == "2026-07"]["VERIFIED_USD"].iloc[0]) == 180.0


def test_savings_by_lever_ranks_and_computes_realization():
    df = _ledger([
        ("VERIFIED", 100.0, 90.0, "2026-06-15", "warehouse_idle"),
        ("VERIFIED", 200.0, 180.0, "2026-07-03", "warehouse_idle"),
        ("VERIFIED", 50.0, 60.0, "2026-06-20", "retention"),
        ("ESTIMATED", 999.0, None, None, "retention"),          # excluded
    ])
    lev = savings_by_lever(df)
    assert list(lev["LEVER"]) == ["warehouse_idle", "retention"]   # by verified $ desc
    wi = lev[lev["LEVER"] == "warehouse_idle"].iloc[0]
    assert float(wi["VERIFIED_USD"]) == 270.0 and int(wi["ITEMS"]) == 2
    assert float(wi["REALIZATION_PCT"]) == 90.0                 # 270 / 300
    ret = lev[lev["LEVER"] == "retention"].iloc[0]
    assert float(ret["REALIZATION_PCT"]) == 120.0              # 60 / 50 (beat the estimate)


def test_month_and_lever_safe_on_empty():
    assert savings_by_month(None).empty
    assert savings_by_month(pd.DataFrame()).empty
    assert savings_by_lever(_ledger([("ESTIMATED", 5.0, None, None, "x")])).empty  # nothing verified


def test_ledger_builder_carries_finding_type():
    sql = mart_sql.savings_ledger()
    assert "FINDING_TYPE" in sql and "unclassified" in sql


def test_roi_is_a_first_class_decision_studio_section():
    page = _src("app/ui/pages/decision_studio.py")
    # ROI is FIRST in the section list (the flagship landing) and dispatched.
    assert '["ROI", "Portfolio"' in page
    assert 'if section == "ROI":' in page and "_roi(company)" in page
    ds = _src("app/ui/decision_studio.py")
    assert "def _roi(company: str)" in ds
    assert "Return on OVERWATCH" in ds
    assert "savings_by_month(" in ds and "savings_by_lever(" in ds
    # the old buried realization block in _scenarios now points at the ROI section.
    assert ds.count("Realization — the savings track record") == 0
