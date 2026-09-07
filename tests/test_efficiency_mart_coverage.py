"""Deferred efficiency-mart follow-ups from bug-hunt round 34.

BUG 1 (MED) — a stale/young MART_WAREHOUSE_EFFICIENCY_DAILY was accepted and its idle/sizing
projections divided by the FULL requested window, not the days actually covered (served_days stamped
int(days) on the mart leg; the pre-aggregated readers carry no DAY column). Fix: eff_idle_analysis /
eff_sizing_profile now emit COVERED_DAYS = COUNT(DISTINCT DAY) over the window, and served_days honors
it — so a loader-lagged or young mart reports an honest run-rate + window label instead of understating.

BUG 2 (MED) — the live idle twin counted METERED_HOURS = COUNT(*) (all metering rows) while the mart
counted BILLED_HOURS = COUNT_IF(CREDITS_USED > 0); the divergence moved idle_advisor's credits_per_hour
resume-tail denominator between the mart-cold and mart-warm legs. Fix: the live twin now counts billed
hours too (and gates IDLE_HOURS the same way, making it consistent with IDLE_CREDITS).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.data import insights_sql, mart27_sql
from app.ui.components import served_days


class _Res:
    """Minimal run_mart_first-style result carrying a df (with .attrs) for served_days."""

    def __init__(self, df):
        self.df = df


# --- BUG 1: COVERED_DAYS + served_days honors it ---------------------------
def test_eff_readers_emit_covered_days_window_scalar():
    for sql in (mart27_sql.eff_idle_analysis(365, "ALL"),
                mart27_sql.eff_sizing_profile(365, "ALL")):
        assert "COUNT(DISTINCT DAY)" in sql
        assert "AS COVERED_DAYS" in sql
    # bounded 'Last month' still emits the covered-days scalar (over the bounded window)
    bounded = mart27_sql.eff_idle_analysis(31, "ALL", bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    assert "AS COVERED_DAYS" in bounded
    assert "2026-08-01" in bounded


def test_served_days_prefers_the_marts_own_covered_span():
    # mart leg: stamped effective days = full ask (7), but the mart only covers 5 DISTINCT days
    stale = pd.DataFrame({"WAREHOUSE_NAME": ["A"], "IDLE_CREDITS": [100.0], "COVERED_DAYS": [5]})
    stale.attrs["_ow_effective_days"] = 7        # what _mark_served stamps on the mart leg
    stale.attrs["_ow_served_live"] = False
    assert served_days(_Res(stale), 7) == 5      # honors the real covered span, not the stamp

    # young mart: 120 of a 365-day ask
    young = pd.DataFrame({"WAREHOUSE_NAME": ["A"], "IDLE_CREDITS": [1200.0], "COVERED_DAYS": [120]})
    young.attrs["_ow_effective_days"] = 365
    assert served_days(_Res(young), 365) == 120

    # covered span can never exceed the requested window (belt-and-suspenders cap)
    over = pd.DataFrame({"WAREHOUSE_NAME": ["A"], "COVERED_DAYS": [400]})
    assert served_days(_Res(over), 90) == 90


def test_served_days_falls_back_when_no_covered_days_column():
    # the live twin carries no COVERED_DAYS -> honest clamp path, unchanged behavior
    from app.config import clamp_days
    live = pd.DataFrame({"WAREHOUSE_NAME": ["A"], "IDLE_CREDITS": [10.0]})
    live.attrs["_ow_served_live"] = True
    assert served_days(_Res(live), 365) == clamp_days(365)
    # a plain frame (not through run_mart_first) -> requested days, no wrong clamp
    plain = pd.DataFrame({"WAREHOUSE_NAME": ["A"]})
    assert served_days(_Res(plain), 30) == 30


def test_covered_days_denominator_corrects_the_monthly_projection():
    # end-to-end intent: 100 idle credits accrued over 5 covered days projects at the 5-day
    # run-rate (x30/5), NOT the 7-day ask (x30/7) — the ~29% understatement is gone.
    from app.logic.insights import idle_waste_summary
    df = pd.DataFrame({"WAREHOUSE_NAME": ["A"], "IDLE_CREDITS": [100.0],
                       "TOTAL_CREDITS": [400.0], "COVERED_DAYS": [5]})
    df.attrs["_ow_effective_days"] = 7
    covered = served_days(_Res(df), 7)
    summ = idle_waste_summary(df, credit_rate_usd=1.0, window_days=covered)
    # idle_usd = 100; projected monthly = 100 / 5 * 30 = 600 (would have been 100/7*30 ~= 429)
    assert summ["PROJECTED_MONTHLY_USD"] == 600.0


# --- BUG 2: live twin billed-hours alignment -------------------------------
def test_live_idle_twin_counts_billed_hours_like_the_mart():
    live = insights_sql.idle_warehouse_analysis(30, "ALL")
    # METERED_HOURS is billed hours (matches mart BILLED_HOURS = COUNT_IF(CREDITS_USED > 0)),
    # no longer COUNT(*) over every metering row
    assert "COUNT_IF(COALESCE(M.CREDITS_USED, 0) > 0) AS METERED_HOURS" in live
    assert "COUNT(*) AS METERED_HOURS" not in live
    # IDLE_HOURS is gated on billed hours too, so it agrees with IDLE_CREDITS' billed-idle basis
    assert "IFF(Q.HOUR_TS IS NULL AND COALESCE(M.CREDITS_USED, 0) > 0, 1, 0)" in live
    # the mart twin's billed-hours definition is unchanged (the alignment target)
    mart = mart27_sql.eff_idle_analysis(30, "ALL")
    assert "SUM(BILLED_HOURS) AS METERED_HOURS" in mart
