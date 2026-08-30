"""Cost-layer bug-hunt locks (2026-08-30).

App-side fixes shipped in v4.353.0 (no migration this round):
  - savings_by_lever realization % restricts the numerator to estimate-carrying verified
    items (matches ledger_totals) so a zero-estimate verified item can't inflate it past 100%
  - the MTD storage KPI gets the same account-level loader-gap watermark backfill the prior
    month has, so an under-loaded month-to-date doesn't understate $ and skew MoM negative
  - the year-end projection flags thin trailing history instead of extrapolating a firm total
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.actions import savings_by_lever

_ROOT = Path(__file__).resolve().parents[1]


def _ver(est, ver, lever):
    return {"STATE": "VERIFIED", "ESTIMATED_USD": est, "VERIFIED_USD": ver, "FINDING_TYPE": lever}


def test_savings_by_lever_realization_excludes_zero_estimate_from_numerator():
    # A carried an estimate ($100 -> $80); B was booked with no estimate ($0 -> $50). The
    # realization % must be 80% (80/100), NOT 130% ((80+50)/100) — a zero-estimate verified
    # item adds its $ to the numerator with nothing in the denominator otherwise.
    df = pd.DataFrame([_ver(100.0, 80.0, "resize"), _ver(0.0, 50.0, "resize")])
    lev = savings_by_lever(df)
    row = lev[lev["LEVER"] == "resize"].iloc[0]
    assert float(row["VERIFIED_USD"]) == 130.0          # the $ column keeps the full verified total
    assert int(row["ITEMS"]) == 2
    assert float(row["REALIZATION_PCT"]) == 80.0        # numerator restricted to estimate-carrying items


def test_savings_by_lever_all_zero_estimate_realization_is_null():
    # a lever with only zero-estimate verified items has no meaningful realization ratio
    df = pd.DataFrame([_ver(0.0, 40.0, "tag_only")])
    lev = savings_by_lever(df)
    row = lev[lev["LEVER"] == "tag_only"].iloc[0]
    assert float(row["VERIFIED_USD"]) == 40.0
    assert pd.isna(row["REALIZATION_PCT"])              # 0 denominator -> NaN, not a fake %


def test_mtd_storage_has_the_prior_paths_loader_gap_backfill():
    spend = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    body = spend.split("Storage MTD (daily avg)", 1)[0]
    # the account-level watermark rescale is applied to mtd_tib before MoM is computed
    assert "mtd_tib *= expected_days / covered" in body
    assert "if expected_days > 0 and 0 < covered < expected_days:" in body


def test_year_projection_flags_thin_trailing_history():
    contract = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    proj = contract.split("def _year_projection_strip", 1)[1].split("\ndef ", 1)[0]
    assert "_MIN_PROJECTION_DAYS = 7" in proj
    assert "_thin = _hist_days < _MIN_PROJECTION_DAYS" in proj
    assert "Low confidence" in proj
