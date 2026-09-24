"""Next-Fifty #16 (v4.588.0): Operations ▸ Sizing and Cost ▸ Optimize route warehouse sizing through
ONE shared SHOW-WAREHOUSES mapping (insights.with_warehouse_settings), so an X-Small warehouse is never
a "Size down candidate" on Operations while Optimize correctly refuses it."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.insights import with_auto_suspend_settings, with_warehouse_settings
from app.logic.sizing import RECOMMEND_CADENCE, RECOMMEND_DOWN, normalize_size, size_recommendations

_ROOT = Path(__file__).resolve().parents[1]


def _profile(name: str = "WH_XS") -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": name, "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 500,
                          "ACTIVE_QUERY_DAYS": 10.0, "P95_ELAPSED_SEC": 5.0, "QUEUED_SEC": 0.0,
                          "SPILL_REMOTE_GB": 0.0, "IDLE_PCT": 35.0}])


def _show(size: str) -> pd.DataFrame:
    return pd.DataFrame([{"name": "WH_XS", "size": size, "auto_suspend": 60,
                          "min_cluster_count": 1, "max_cluster_count": 3, "scaling_policy": "standard"}])


def test_with_warehouse_settings_maps_size_and_cluster_config():
    prof = pd.DataFrame({"WAREHOUSE_NAME": ["wh_xs", "WH_MISSING"]})
    out = with_warehouse_settings(prof, _show("X-Small"))
    xs, miss = out.iloc[0], out.iloc[1]
    assert xs["CURRENT_SIZE"] == "X-Small"
    assert xs["MAX_CLUSTER_COUNT"] == 3 and xs["MIN_CLUSTER_COUNT"] == 1
    assert xs["SCALING_POLICY"] == "STANDARD"
    assert bool(xs["AUTO_SUSPEND_KNOWN"]) is True
    assert pd.isna(miss["CURRENT_SIZE"]) and bool(miss["AUTO_SUSPEND_KNOWN"]) is False
    # SHOW without a size column adds no CURRENT_SIZE; an empty SHOW frame == auto-suspend only
    only_as = with_warehouse_settings(prof, pd.DataFrame([{"name": "WH_XS", "auto_suspend": 60}]))
    assert "CURRENT_SIZE" not in only_as.columns
    pd.testing.assert_frame_equal(with_warehouse_settings(prof, pd.DataFrame()),
                                  with_auto_suspend_settings(prof, pd.DataFrame()))


def test_xsmall_verdict_identical_on_both_pages():
    xs = size_recommendations(with_warehouse_settings(_profile(), _show("X-Small")), 3.68, 30)
    assert xs.iloc[0]["RECOMMENDATION"] == RECOMMEND_CADENCE
    assert float(xs.iloc[0]["POTENTIAL_MONTHLY_SAVING_USD"]) == 0.0
    med = size_recommendations(with_warehouse_settings(_profile(), _show("Medium")), 3.68, 30)
    assert med.iloc[0]["RECOMMENDATION"] == RECOMMEND_DOWN


def test_both_pages_route_sizing_through_the_shared_helper():
    ops = (_ROOT / "app/ui/pages/operations.py").read_text(encoding="utf-8")
    body = ops.split("def _wh_sizing_efficiency", 1)[1].split("\ndef ", 1)[0]
    assert "with_warehouse_settings(" in body and "with_auto_suspend_settings(" not in body
    opt = (_ROOT / "app/ui/pages/cost_parts/optimize.py").read_text(encoding="utf-8")
    assert "with_warehouse_settings(" in opt
    assert "_size_map" not in opt                    # the inline mapping retired into the helper


def test_normalize_size_tolerates_missing():
    assert normalize_size(pd.NA) == ""
    assert normalize_size(float("nan")) == ""
    assert normalize_size(None) == ""
    assert normalize_size("X-Small") == "XSMALL"
