"""Bug-hunt round 33: forecast / runway edge cases.

Surface = month-end projection + contract runway/pacing. Yield: 4 distinct fixes,
1 finding refuted on deeper ground-truth ([6]) + 3 refuted earlier — a floor signal
for this well-worked money-math surface.

[1] MED — month_end_projection dropped COMPLETE days that were MISSING from the frame.
    fact_daily_spend GROUPs BY DAY with no date spine, so an ingest-lagged (or genuinely
    idle) day has no row; it was counted in neither mtd_complete nor `add` (which starts at
    today), so the projection read low by that day. Fix: fill the missing COMPLETE days at
    the baseline mean — but ONLY days inside the loaded coverage (>= the earliest row; days
    before the frame's first row are un-loaded history, never fabricated) and ONLY when the
    window is dense (majority of covered days present, so a sparse/idle account is not handed
    a fabricated month).
[2]/[4] LOW — contract_exhaustion anchored EXHAUST_DATE on session-tz CURRENT_DATE() (UTC under
    SiS), drifting a day from FACT_METERING_DAILY.DAY's account calendar near midnight. EXHAUST_DATE's
    anchor is now account_today_sql(). The DAILY_BURN window deliberately STAYS session-tz: it must
    byte-match the V064 COST_CONTRACT_BREACH paging alert (test_rec20_alert_matches_app_mart_window),
    and realigning both would take an owner-applied migration to alter the alert proc (deferred).
[3] MED — with CONTRACT_CREDITS set but CONTRACT_START_DATE unset, TOTAL summed credits while
    CONSUMED fell back to ~today (=0), fabricating a healthy green runway on the always-on
    Overview/Brief bars. Fixed: TOTAL=0 when the start is unconfigured -> contract_runway()
    returns None -> the bars render nothing (matches the Contract page's own start gate).
[5] LOW — plan_scenarios: a near-idle account (tiny burn) x a large remaining balance yields a
    huge days_left; anchor + timedelta(days=int(days_left)) raised OverflowError. Fixed with a
    ~10y horizon cap -> ">10y".
[6] REFUTED — contract_pace term_days = (end - start).days looked like an off-by-one vs the
    inclusive elapsed_days. Ground-truth: the app treats CONTRACT_END_DATE as an EXCLUSIVE
    boundary (test_contract_pace_hand pins "term 1/1->4/11 = 100 days"), and the function is
    internally consistent with it. Changing it would break the documented convention. Locked
    below so a future "fix" can't silently flip it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.data import mart_sql
from app.logic.contract_planner import plan_scenarios
from app.logic.forecast import contract_pace, month_end_projection

_TODAY = date(2026, 7, 20)   # 11 days remain after today (Jul 21-31); 31-day month


def _flat_ending_yesterday(n: int, usd: float = 100.0) -> pd.DataFrame:
    """n consecutive complete days at `usd`, ending the day BEFORE _TODAY (no today row)."""
    return pd.DataFrame([{"DAY": _TODAY - timedelta(days=i), "USD": usd} for i in range(1, n + 1)])


# --- [1] month-end gap fill ------------------------------------------------
def test_round33_fills_a_missing_complete_day_inside_coverage():
    dense = _flat_ending_yesterday(40)                       # 06-10 .. 07-19, all $100
    gap = dense[dense["DAY"] != date(2026, 7, 10)].copy()    # drop one mid-July complete day
    p_dense = month_end_projection(dense, _TODAY, engine="linear").projected_usd
    p_gap = month_end_projection(gap, _TODAY, engine="linear").projected_usd
    # the dropped $100 day is filled back at the baseline mean ($100), so the projection is
    # unchanged; WITHOUT the fix it would read ~$100 low (the day counted nowhere).
    assert p_dense == 3100.0
    assert p_gap == p_dense


def test_round33_never_fabricates_pre_window_days():
    # 14-day window ending 07-19; the month's 07-01..07-05 are BEFORE the earliest row
    # (un-loaded history), not gaps. They must not be filled: projected = MTD + forward add.
    frame = _flat_ending_yesterday(14)                       # 07-06 .. 07-19
    p = month_end_projection(frame, _TODAY, engine="linear").projected_usd
    # 1400 complete-MTD (07-06..07-19) + 100 x 12 (today + 11 remaining) = 2600.
    # A naive fill of the 5 pre-window days would have added $500 -> 3100.
    assert p == 2600.0


def test_round33_density_guard_blocks_sparse_account():
    # Only every 3rd day present across the window -> 6 of 19 covered July days -> below the
    # 50% density floor -> gaps are NOT filled (a genuinely idle account keeps its real shape).
    sparse = pd.DataFrame([{"DAY": _TODAY - timedelta(days=i), "USD": 100.0}
                           for i in range(3, 60, 3)])
    p = month_end_projection(sparse, _TODAY, engine="linear").projected_usd
    # 6 present July complete days x $100 = 600 MTD + 100 x 12 forward = 1800; a fabricated
    # fill of the 13 missing covered days would have added $1300 -> 3100.
    assert p == 1800.0


# --- [2]/[4] + [3] contract_exhaustion -------------------------------------
def test_round33_contract_exhaustion_account_tz_and_start_gate():
    sql = mart_sql.contract_exhaustion()
    # [3]: TOTAL is gated on a configured CONTRACT_START_DATE (unset -> 0 -> no runway bar).
    assert "IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0," in sql
    # [2]/[4]: EXHAUST_DATE's displayed anchor rides the account clock.
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS EXHAUST_DATE" in sql
    # ...but the DAILY_BURN window STAYS session-tz so it byte-matches the V064 alert (invariant
    # in test_rec20_alert_matches_app_mart_window). Realigning it needs an owner migration.
    assert "DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())" in sql
    assert "AND DATEADD('day', -1, CURRENT_DATE())" in sql


# --- [5] plan_scenarios overflow guard -------------------------------------
def test_round33_plan_scenarios_caps_the_exhaustion_horizon():
    # Near-idle burn x a large balance: days_left ~ 1e12 -> would overflow date arithmetic.
    rows = plan_scenarios(daily_burn_usd=0.001, term_months=12, buffer_pct=10.0,
                          remaining_usd=1_000_000_000, today=date(2026, 1, 1))
    assert rows[1]["GROWTH"] == "+0%"
    assert rows[1]["CURRENT_CONTRACT_EXHAUSTED"] == ">10y"   # capped, no OverflowError


def test_round33_plan_scenarios_still_dates_a_normal_contract():
    rows = plan_scenarios(daily_burn_usd=100.0, term_months=12, buffer_pct=10.0,
                          remaining_usd=3000.0, today=date(2026, 1, 1))
    assert rows[1]["GROWTH"] == "+0%"
    assert rows[1]["CURRENT_CONTRACT_EXHAUSTED"] == "2026-01-31"   # 3000 / 100 = 30 days


# --- [6] refuted: keep the EXCLUSIVE contract-end convention ----------------
def test_round33_contract_pace_keeps_exclusive_end_convention():
    # 1/1 -> 4/11 is a 100-day term (end EXCLUSIVE); day 50 inclusive, half consumed -> 50%.
    # This is the app's documented convention; [6]'s "+1 inclusive" would break it.
    pace = contract_pace(500, 1000, date(2026, 1, 1), date(2026, 4, 11), date(2026, 2, 19))
    assert pace["time_share"] == 50.0
    assert pace["pace_ratio"] == 1.0
