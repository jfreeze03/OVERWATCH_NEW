"""Regression locks for the comprehensive bug-hunt fixes (v4.416.0).

The Last-month rollout left several scope reads trailing and the Cortex live-fallback
projection anchored on today. These pin the fixes:
- the Cortex user pandas fold slices the BOUNDED month, not a trailing span;
- the per-user 30d projection divisor anchors on the window END (not today), so a
  last-month view mid-current-month does not understate burn / suppress budget breaches;
- the AI-phrasing 'grounded numbers unchanged' promise is enforced;
- cortex_complete routes a NULL result to the empty-answer path (not the literal "None");
- the Cost Spend/optimize lower-panel builders honor bounds.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.logic import cortex
from app.logic.date_windows import window_bounds

_ROOT = Path(__file__).resolve().parents[1]
_AUG = window_bounds("LAST_MONTH", date(2026, 9, 17))   # (2026-08-01, 2026-09-01)


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _user_daily() -> pd.DataFrame:
    # rows spanning Jul, Aug and Sep for one user
    days = ["2026-07-20", "2026-08-05", "2026-08-31", "2026-09-02"]
    return pd.DataFrame({
        "USAGE_DATE": pd.to_datetime(days),
        "USER_NAME": ["bob"] * 4, "EMAIL": ["b@x"] * 4,
        "FIRST_NAME": ["B"] * 4, "LAST_NAME": ["Y"] * 4, "SOURCE": ["CLI"] * 4,
        "REQUESTS": [10, 10, 10, 10], "CREDITS": [5.0, 5.0, 5.0, 5.0], "TOKENS": [1, 1, 1, 1],
        "FIRST_TS": pd.to_datetime(days), "LAST_TS": pd.to_datetime(days),
    })


def test_cortex_fold_slices_the_bounded_month_not_a_trailing_span(monkeypatch):
    monkeypatch.setattr(cortex, "account_today", lambda: date(2026, 9, 17))
    # trailing (no bounds): with days=span the cutoff is today-31 -> Aug17..today, which
    # INCLUDES the Sep row and DROPS early-August — the wrong set for "August".
    trailing = cortex.daily_from_user_daily(_user_daily(), 31)
    assert pd.Timestamp("2026-09-02") in set(trailing["DAY"])       # leaks current month
    # bounded: exactly the two August rows, no July, no September
    bounded = cortex.daily_from_user_daily(_user_daily(), 31, bounds=_AUG)
    got = set(pd.to_datetime(bounded["DAY"]))
    assert got == {pd.Timestamp("2026-08-05"), pd.Timestamp("2026-08-31")}


def test_cortex_projection_divisor_anchors_on_window_end_not_today(monkeypatch):
    monkeypatch.setattr(cortex, "account_today", lambda: date(2026, 9, 17))
    # a user whose first August usage is Aug 25 has 7 observable in-window days (Aug 25-31),
    # NOT (today - Aug 25) ~= 24 days. effective_window_days must return 7 under bounds.
    rollup = pd.DataFrame({"USER_NAME": ["bob"], "FIRST_USAGE": [pd.Timestamp("2026-08-25", tz="UTC")],
                           "TOTAL_CREDITS": [40.0], "AVG_DAILY_CREDITS": [40.0 / 7],
                           "CREDITS_PER_REQUEST": [1.0], "TOTAL_REQUESTS": [40]})
    assert cortex.effective_window_days(rollup, 31, bounds=_AUG) == 7
    # anchoring on today would have understated the divisor to ~24 days
    assert cortex.effective_window_days(rollup, 31) > 7
    # the enriched per-user projection uses the 7-day divisor -> higher, honest burn
    enriched_lm = cortex.enrich_user_rollup(rollup, 2.20, 31, bounds=_AUG)
    enriched_tr = cortex.enrich_user_rollup(rollup, 2.20, 31)
    assert float(enriched_lm["OBSERVABLE_DAYS"].iloc[0]) == 7
    assert float(enriched_lm["PROJECTED_30D_USD"].iloc[0]) > float(enriched_tr["PROJECTED_30D_USD"].iloc[0])


def test_ai_phrasing_numeric_guard_rejects_drifted_numbers():
    from app.ui.pages.ask import _numbers_preserved
    grounded = "Over the last 90d, LOADER is the top spender: 120 credits (45% of spend)."
    assert _numbers_preserved(grounded, "LOADER led at 120 credits, about 45% of spend.")
    assert _numbers_preserved(grounded, "LOADER dominated with 1,20 -> no")  # only reused digits
    # a fabricated/changed number is rejected (the phrasing would be discarded)
    assert not _numbers_preserved(grounded, "LOADER spent 130 credits (45%).")
    assert not _numbers_preserved(grounded, "LOADER was 99% of spend.")


def test_cortex_complete_coalesces_null_to_empty_answer():
    # a SQL NULL must not render as the literal answer "None"
    src = _src("app/core/ai.py")
    assert 'str(rows[0]["ANSWER"] or "") if rows else ""' in src
    assert 'str(rows[0]["ANSWER"])' not in src   # the un-coalesced form is gone


def test_spend_and_optimize_lower_panels_thread_bounds():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    for call in ("org_all_in_window_usd(days, bounds=bounds)",
                 "transfer_egress_priced(days, bounds=bounds)",
                 "marketplace_paid_usage(days, bounds=bounds)",
                 "compute_pool_usage(days, bounds=bounds)",
                 "cs_by_query_type(days, company, bounds=bounds)",
                 "cloud_svc_top_shapes(days, company, wh_arg, bounds=bounds)",
                 "app_cost_mart(days, company, bounds=bounds)"):
        assert call in spend, call
    for call in ("idle_warehouse_analysis(days, company, bounds=bounds)",
                 "warehouse_sizing_profile(days, company, bounds=bounds)",
                 "warehouse_hourly_activity(days, company, bounds=bounds)"):
        assert call in opt, call


def test_overview_vs_prior_delta_labels_prior_month_under_last_month():
    ov = _src("app/ui/pages/overview.py")
    assert 'vs prior month' in ov and "_ov_bounds is not None" in ov
