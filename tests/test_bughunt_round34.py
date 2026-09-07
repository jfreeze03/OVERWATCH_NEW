"""Bug-hunt round 34: warehouse efficiency / rightsizing / idle & auto-suspend advisory.

Multi-agent adversarial sweep (6 finder dimensions -> per-finder refute -> completeness critic).
8 fixes shipped; the deep coverage/twin items (stale-mart denominator, METERED_HOURS twin) are
deferred to a focused follow-up (shared served_days/run_mart_first serving-contract surgery).

#1  [LOW]  sizing ladder stopped at 4X-Large: 5X/6X refused, 4XL '+1' clamped silent -> extend ladder.
#2  [MED]  alert closed-loop "Tighten auto-suspend to 60s" had NO current-setting guard -> could RAISE
           an already-30s timer (the A3 hazard). Shared remediation.tighten_suspend_plan now gates both
           the alert responder and the Optimize Remediation tab.
MISSED2 [MED] size_recommendations emitted RECOMMEND_DOWN + a booked saving on a warehouse already at
           the smallest size (no target below XSMALL) -> gate DOWN off the floor when size is known.
#4  [MED]  expensive_patterns_usd ignored 'Last month' bounds while its sibling panels honored them.
#6  [MED]  idle headline help claimed equality with a tile that was consolidated away.
#7  [MED]  what-if 'Now' tile labeled a never-suspend warehouse '0s suspend' (inverts the meaning).
#8  [LOW]  idle advisor showed '$0 actionable' with no headline warning when SHOW WAREHOUSES failed.
#9  [LOW]  steering coverage in [99.95,100) painted a GREEN success box around the shortfall verdict
           (branch read RAW coverage, color read ROUNDED). One source of truth now.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import insights_sql
from app.logic import remediation
from app.logic.sizing import (
    RECOMMEND_CADENCE,
    RECOMMEND_DOWN,
    normalize_size,
    shifted_size,
    simulate_scenario,
    size_recommendations,
)
from app.logic.steering import steering_plan

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- #1 size ladder through 6X-Large ---------------------------------------
def test_round34_size_ladder_reaches_6xlarge():
    assert normalize_size("5X-Large") == "5XLARGE"
    assert normalize_size("6X-Large") == "6XLARGE"
    assert normalize_size("6X") == "6XLARGE"
    # 4XL '+1' now lands on a real upsize instead of clamping to itself
    assert shifted_size("4X-Large", 1) == "5XLARGE"
    assert shifted_size("6X-Large", -1) == "5XLARGE"
    # the simulator prices the two priciest sizes instead of refusing them
    sim = simulate_scenario(size="6X-Large", credits_window=1000.0, idle_credits_window=200.0,
                            window_days=30, rate_usd=1.0, size_delta=-1)
    assert sim["ok"] and sim["size_now"] == "6XLARGE" and sim["size_new"] == "5XLARGE"


# --- MISSED2 no size-down on the smallest size -----------------------------
def _down_candidate_row(**over):
    row = {"WAREHOUSE_NAME": "WH_X", "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 500,
           "ACTIVE_QUERY_DAYS": 10.0, "P95_ELAPSED_SEC": 5.0, "QUEUED_SEC": 0.0,
           "SPILL_REMOTE_GB": 0.0, "IDLE_PCT": 35.0}
    row.update(over)
    return row


def test_round34_size_down_suppressed_on_smallest_size():
    base = _down_candidate_row()
    # unknown size -> fail-open, still a DOWN candidate (unchanged behavior)
    out_unknown = size_recommendations(pd.DataFrame([base]), 3.68, 30)
    assert out_unknown.iloc[0]["RECOMMENDATION"] == RECOMMEND_DOWN
    # known smallest size -> DOWN is refused (no target below XSMALL) and no saving booked
    out_xs = size_recommendations(pd.DataFrame([dict(base, CURRENT_SIZE="X-Small")]), 3.68, 30)
    assert out_xs.iloc[0]["RECOMMENDATION"] == RECOMMEND_CADENCE
    assert float(out_xs.iloc[0]["POTENTIAL_MONTHLY_SAVING_USD"]) == 0.0
    # a mid-ladder size still gets the DOWN candidate
    out_med = size_recommendations(pd.DataFrame([dict(base, CURRENT_SIZE="MEDIUM")]), 3.68, 30)
    assert out_med.iloc[0]["RECOMMENDATION"] == RECOMMEND_DOWN


# --- #2 shared tighten guard (A3) ------------------------------------------
def test_round34_tighten_suspend_plan_never_raises_a_tight_timer():
    # already tight (30s) -> no statement, informational
    p = remediation.tighten_suspend_plan("WH", 30, known=True, target=60)
    assert p["stmt"] == "" and p["level"] == "info"
    # unknown setting -> no statement, warn (verification required)
    p = remediation.tighten_suspend_plan("WH", None, known=False, target=60)
    assert p["stmt"] == "" and p["level"] == "warning"
    # long timer -> tighten toward the target
    p = remediation.tighten_suspend_plan("WH", 600, known=True, target=60)
    assert p["level"] == "none" and "SET AUTO_SUSPEND = 60;" in p["stmt"]
    # never-suspend (0) -> enabling the timer is a real saving
    p = remediation.tighten_suspend_plan("WH", 0, known=True, target=60)
    assert p["level"] == "none" and "SET AUTO_SUSPEND = 60;" in p["stmt"]
    # exactly at target -> already tight, no raise
    assert remediation.tighten_suspend_plan("WH", 60, known=True, target=60)["stmt"] == ""


def test_round34_alerts_and_optimize_share_the_tighten_guard():
    alerts = _read("app/ui/pages/alerts.py")
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    # both surfaces now route the tighten fix through the ONE shared guard
    assert "remediation.tighten_suspend_plan(" in alerts
    assert "remediation.tighten_suspend_plan(" in optimize
    # the alert path no longer builds a blind auto_suspend_fix(wh_inline, 60)
    assert "remediation.auto_suspend_fix(wh_inline, 60)" not in alerts


# --- #4 expensive_patterns_usd honors Last-month bounds --------------------
def test_round34_expensive_patterns_honors_last_month_bounds():
    trailing = insights_sql.expensive_patterns_usd(31, "ALL", 30)
    bounded = insights_sql.expensive_patterns_usd(
        31, "ALL", 30, bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    # trailing branch stays byte-identical (rolling window, CURRENT_TIMESTAMP)
    assert "DATEADD('day', -31, CURRENT_TIMESTAMP())" in trailing
    # bounded branch scans the exact prior calendar month
    assert "START_TIME >= '2026-08-01'" in bounded
    assert "START_TIME < '2026-09-01'" in bounded
    assert "DATEADD('day', -31, CURRENT_TIMESTAMP())" not in bounded
    assert bounded != trailing


# --- #9 steering coverage: text and color read coverage the same way -------
def test_round34_steering_coverage_rounds_once_no_false_green():
    # raw coverage 99.96% (covered 9.996/day vs needed 10.0/day) used to take the shortfall
    # ("else") verdict while the rounded 100.0 painted the box green. Now both read the rounded value.
    plan = steering_plan(projected_term_credits=4650, contract_credits=1000,
                         days_remaining=365, rate_usd=1.0,
                         levers_monthly_usd={"lever": 299.88})
    assert plan["needed_per_day_usd"] == 10.0
    assert plan["coverage_pct"] == 100.0            # rounds to 100.0
    assert "enough on paper" in plan["verdict"]     # ...and the >=100 branch was taken (green agrees)
    # a genuine shortfall still reads as a shortfall (warning), no rounding into green
    short = steering_plan(projected_term_credits=4650, contract_credits=1000,
                          days_remaining=365, rate_usd=1.0,
                          levers_monthly_usd={"lever": 150.0})
    assert short["coverage_pct"] < 100 and "reach" in short["verdict"]


# --- #6 / #7 / #8 presentation source-locks --------------------------------
def test_round34_presentation_fixes():
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    # #6: the false "SAME figure as 'Idle spend' inside Idle & sizing" cross-ref is gone
    assert "SAME figure as 'Idle spend'" not in optimize
    # #7: the what-if 'Now' tile labels a never-suspend warehouse honestly
    assert '"never suspends" if live_suspend <= 0' in optimize
    # #8: a headline warning distinguishes '$0 actionable' from 'settings unverified'
    assert "settings could not be verified" in optimize
