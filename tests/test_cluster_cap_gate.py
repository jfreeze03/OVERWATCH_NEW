"""Next-Fifty #38 remainder: the cluster-cap gate on the add-a-cluster advice.

The scale-out advice used to tell every queue-heavy multi-cluster warehouse to "raise MAX_CLUSTER_COUNT to
N+1" (and prefill that ALTER) without checking that N clusters were ever used. Cost ▸ Optimization & Savings
▸ Idle & sizing now has a toggled, cached live read of each multi-cluster warehouse's hourly peak
QUERY_HISTORY.CLUSTER_NUMBER (insights_sql.warehouse_cluster_use, >= 35 days so a month-end is inside), the
pure sizing.cluster_use_summary judges it against the CURRENT SHOW MAX_CLUSTER_COUNT, and the verdict is
gated on it:

  * hours at the cap > 0  -> "raise MAX_CLUSTER_COUNT to N+1" (+ the prefill), with the evidence;
  * checked, never at cap -> RECOMMEND_BELOW_CAP (size up or split; $0; no cluster statement);
  * not checked / failed  -> the text says the cap was not checked; nothing is prefilled.

Single-cluster, ECONOMY and unknown-range rows keep their v4.603 text byte for byte. The probe numbers in
these tests are the owner's 2026-09-29 W1c grid (14 days). No scale-in lever exists (closed as $0).
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

import pandas as pd
import pytest

from app.data import canary, insights_sql
from app.logic import sizing
from app.logic.sizing import (
    CAP_NO_QUERIES,
    CAP_NOT_CHECKED,
    CAP_NOT_REACHED,
    CAP_REACHED,
    CLUSTER_CHECK_MAX_DAYS,
    CLUSTER_CHECK_MIN_DAYS,
    RECOMMEND_BELOW_CAP,
    RECOMMEND_DOWN,
    RECOMMEND_SCALE_OUT,
    RECOMMEND_SIZE_UP,
    RECOMMEND_SUSPEND,
    RECOMMEND_UP,
    UP_VERDICTS,
    cluster_cap_state,
    cluster_check_days,
    cluster_check_targets,
    cluster_use_summary,
    scale_out_plan,
    size_recommendations,
    sizing_summary,
    with_cluster_use,
)
from tests._source import read

sqlglot = pytest.importorskip("sqlglot")

_Q45 = 7 * 45 * 60          # 45 min/day of overload queueing over a 7-day window (as test_sizing_scale_split)
_SPILL = 9.0                # 1.29 GB/day remote spill over 7 days

# The owner's W1c probe (2026-09-29, 14 days), as the histogram warehouse_cluster_use returns:
# {peak cluster in the hour: hours with that peak}.
_W1C = {
    "WH_TRXS_TRANSFORM": ({1: 72, 2: 40, 3: 55, 4: 57}, 4),
    "WH_ALFA_TRANSFORM_PRD": ({1: 203, 2: 20, 3: 9, 4: 43}, 4),
    "WH_ALFA_LOAD_PRD": ({1: 300, 2: 20, 3: 3}, 4),
    "WH_TRXS_QUERY": ({1: 132, 2: 2, 3: 1}, 3),
}

# v4.603 texts of the branches the gate must not touch (captured from HEAD 709bed57 before the change).
_LEAD = ("Concurrency pressure: 45m/day overload queueing, remote spill under 1 GB/day. "
         "Add a cluster rather than a bigger size. ")
_V4603 = {
    "UNKNOWN": _LEAD + ("Multi-cluster needs Enterprise edition and MAX_CLUSTER_COUNT > 1 (the current setting is "
                        "unknown); otherwise move the concurrent workload to its own warehouse."),
    "SINGLE": _LEAD + ("Single-cluster today (MAX_CLUSTER_COUNT = 1): raise MAX_CLUSTER_COUNT to 2 or more "
                       "(multi-cluster needs Enterprise edition), or move the concurrent workload to its own "
                       "warehouse."),
    "ECON": _LEAD + ("Already multi-cluster (up to 3) on ECONOMY, which waits for sustained load before "
                     "starting a cluster — try SCALING_POLICY = STANDARD before raising the maximum."),
}
_EDITION_UNSEEN = ("Multi-cluster needs Enterprise edition or higher — if the ALTER fails, move the concurrent "
                   "workload to its own warehouse instead.")


def _hist(*names: str) -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": n, "PEAK_CLUSTER": float(p), "HOUR_COUNT": float(c)}
                         for n in names for p, c in _W1C[n][0].items()])


def _settings(**maxes: float) -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": n, "MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": float(m)}
                         for n, m in maxes.items()])


def _wh(name, queued_sec=float(_Q45), spill=0.0, p95=5.0, idle=0.0, active_days=7, **extra):
    row = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 1000,
           "ACTIVE_QUERY_DAYS": active_days, "P95_ELAPSED_SEC": p95, "QUEUED_SEC": queued_sec,
           "SPILL_REMOTE_GB": spill, "IDLE_PCT": idle}
    row.update(extra)
    return row


def _state(state: str, mx: float, peak: float = 3.0, at_cap: float = 57.0, days: float = 35.0) -> dict:
    """The columns with_cluster_use carries for each cluster-cap state."""
    if state == CAP_NOT_CHECKED:
        return {}
    if state == CAP_NO_QUERIES:
        return {"CLUSTER_CHECK_DAYS": days, "ACTIVE_HOUR_COUNT": 0.0, "PEAK_CLUSTERS": math.nan,
                "P95_PEAK_CLUSTERS": math.nan, "AT_CAP_HOUR_COUNT": 0.0}
    if state == CAP_NOT_REACHED:
        return {"CLUSTER_CHECK_DAYS": days, "ACTIVE_HOUR_COUNT": 323.0, "PEAK_CLUSTERS": min(peak, mx - 1),
                "P95_PEAK_CLUSTERS": 2.0, "AT_CAP_HOUR_COUNT": 0.0}
    return {"CLUSTER_CHECK_DAYS": days, "ACTIVE_HOUR_COUNT": 224.0, "PEAK_CLUSTERS": mx,
            "P95_PEAK_CLUSTERS": mx, "AT_CAP_HOUR_COUNT": at_cap}


def _sized(*rows) -> pd.DataFrame:
    return size_recommendations(pd.DataFrame(list(rows)), 3.68, 7).set_index("WAREHOUSE_NAME")


# ---------------------------------------------------------------------------
# 7.1 / 7.2 the check window
# ---------------------------------------------------------------------------

def test_cluster_check_days_covers_the_window_min_35_max_90():
    today = date(2026, 9, 30)
    assert (CLUSTER_CHECK_MIN_DAYS, CLUSTER_CHECK_MAX_DAYS) == (35, 90)
    assert [cluster_check_days(d, None, today) for d in (7, 30, 60, 365)] == [35, 35, 60, 90]
    assert cluster_check_days(0, None, today) == 35
    assert cluster_check_days(31, date(2026, 8, 1), today) == 60          # Last month: back to Aug 1
    assert cluster_check_days(29, date(2026, 9, 1), today) == 35          # current month
    assert cluster_check_days(90, date(2026, 1, 1), today) == 90          # current year: the live clamp
    assert CLUSTER_CHECK_MAX_DAYS == 90                                    # == data.common.bounded_days default


def test_any_35_day_window_holds_a_month_end():
    """The caption / CHANGELOG claim: a check of at least 35 days always has a month-end inside. The read is
    START_TIME >= now - N days, so the N-1 whole days before today are fully covered; for N = 35 some day
    among them is the last of its month, for every day of 2024-2027. A 30-day window does NOT guarantee it
    (read on Aug 31: whole days Aug 2..Aug 30, a partial Aug 1 and today — no month-end)."""
    def month_end(d: date) -> bool:
        return (d + timedelta(days=1)).day == 1

    def whole_days(today: date, n: int) -> list[date]:
        return [today - timedelta(days=k) for k in range(1, n)]

    day = date(2024, 1, 1)
    while day <= date(2027, 12, 31):
        assert any(month_end(d) for d in whole_days(day, CLUSTER_CHECK_MIN_DAYS)), day
        day += timedelta(days=1)
    assert not any(month_end(d) for d in whole_days(date(2026, 8, 31), 30))   # Aug 2..Aug 30


# ---------------------------------------------------------------------------
# 7.3 - 7.5 targets, the summary, carrying it onto the profile
# ---------------------------------------------------------------------------

def test_cluster_check_targets():
    frame = pd.DataFrame([
        {"WAREHOUSE_NAME": " wh_b ", "MAX_CLUSTER_COUNT": 4.0},
        {"WAREHOUSE_NAME": "WH_A", "MAX_CLUSTER_COUNT": "3"},
        {"WAREHOUSE_NAME": "wh_a", "MAX_CLUSTER_COUNT": 3.0},
        {"WAREHOUSE_NAME": "WH_SINGLE", "MAX_CLUSTER_COUNT": 1.0},
        {"WAREHOUSE_NAME": "WH_UNKNOWN", "MAX_CLUSTER_COUNT": math.nan},
    ])
    assert cluster_check_targets(frame) == ["WH_A", "WH_B"]
    assert cluster_check_targets(frame.drop(columns="MAX_CLUSTER_COUNT")) == []
    assert cluster_check_targets(pd.DataFrame()) == [] and cluster_check_targets(None) == []
    many = pd.DataFrame({"WAREHOUSE_NAME": [f"WH_{i:03d}" for i in range(150)], "MAX_CLUSTER_COUNT": 2.0})
    assert cluster_check_targets(many) == [f"WH_{i:03d}" for i in range(100)]
    # the page's cap and the builder's cap are the same number (== the sizing profile's LIMIT 100)
    assert sizing.CLUSTER_CHECK_MAX_WAREHOUSES == insights_sql.CLUSTER_USE_MAX_WAREHOUSES == 100
    assert "LIMIT 100" in insights_sql.warehouse_sizing_profile(30, "ALL")


def test_cluster_use_summary_reproduces_the_W1c_probe():
    names = list(_W1C)
    frame = _settings(**{n: _W1C[n][1] for n in names}, WH_IDLE=3.0)
    targets = cluster_check_targets(frame)
    out = cluster_use_summary(_hist(*names), frame, targets)
    assert list(out.columns) == ["WAREHOUSE_NAME", "MIN_CLUSTER_COUNT", "MAX_CLUSTER_COUNT", "ACTIVE_HOUR_COUNT",
                                 "PEAK_CLUSTERS", "P95_PEAK_CLUSTERS", "AT_CAP_HOUR_COUNT", "CLUSTER_CAP"]
    got = out.set_index("WAREHOUSE_NAME")
    exp = {  # active, peak, p95, at-cap, label  (W1c: ACTIVE_HOURS / PEAK / P95 / HOURS_AT_MAX)
        "WH_TRXS_TRANSFORM": (224, 4, 4, 57, "Reached"),
        "WH_ALFA_TRANSFORM_PRD": (275, 4, 4, 43, "Reached"),
        "WH_ALFA_LOAD_PRD": (323, 3, 2, 0, "Not reached"),
        "WH_TRXS_QUERY": (135, 3, 1, 1, "Reached"),
    }
    for name, (active, peak, p95, at_cap, label) in exp.items():
        row = got.loc[name]
        assert (row["ACTIVE_HOUR_COUNT"], row["PEAK_CLUSTERS"], row["P95_PEAK_CLUSTERS"],
                row["AT_CAP_HOUR_COUNT"], row["CLUSTER_CAP"]) == (active, peak, p95, at_cap, label), name
    idle = got.loc["WH_IDLE"]                                  # a target with no rows
    assert idle["ACTIVE_HOUR_COUNT"] == 0 and idle["AT_CAP_HOUR_COUNT"] == 0 and idle["CLUSTER_CAP"] == "No queries"
    assert math.isnan(idle["PEAK_CLUSTERS"]) and math.isnan(idle["P95_PEAK_CLUSTERS"])
    # sorted by hours at cap (desc), then peak (NaN last)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_TRXS_TRANSFORM", "WH_ALFA_TRANSFORM_PRD", "WH_TRXS_QUERY",
                                           "WH_ALFA_LOAD_PRD", "WH_IDLE"]
    assert list(out["AT_CAP_HOUR_COUNT"]) == sorted(out["AT_CAP_HOUR_COUNT"], reverse=True)
    # a zero-row histogram is a valid answer: every target reads No queries
    for zero in (pd.DataFrame(columns=["WAREHOUSE_NAME", "PEAK_CLUSTER", "HOUR_COUNT"]), pd.DataFrame()):
        z = cluster_use_summary(zero, frame, targets)
        assert len(z) == len(targets) and set(z["CLUSTER_CAP"]) == {"No queries"}
        assert (z["ACTIVE_HOUR_COUNT"] == 0).all()
    # a since-lowered cap: a peak of 5 on a max-4 warehouse counts at the cap
    lowered = cluster_use_summary(pd.DataFrame([{"WAREHOUSE_NAME": "wh_low", "PEAK_CLUSTER": 5, "HOUR_COUNT": 2},
                                                {"WAREHOUSE_NAME": "WH_LOW", "PEAK_CLUSTER": 1, "HOUR_COUNT": 8}]),
                                  _settings(WH_LOW=4.0), ["WH_LOW"]).iloc[0]
    assert (lowered["AT_CAP_HOUR_COUNT"], lowered["PEAK_CLUSTERS"], lowered["CLUSTER_CAP"]) == (2, 5, "Reached")
    # None, or rows without the expected columns -> EMPTY (the page reads it as not checked)
    for bad in (None, pd.DataFrame([{"WAREHOUSE_NAME": "WH_TRXS_TRANSFORM", "PEAK": 4, "HOURS": 3}])):
        empty = cluster_use_summary(bad, frame, targets)
        assert empty.empty and list(empty.columns) == list(out.columns)
    # the frame's spelling is kept; MIN/MAX come from the SHOW-backed profile row
    spelled = cluster_use_summary(_hist("WH_TRXS_QUERY"), pd.DataFrame([
        {"WAREHOUSE_NAME": "wh_trxs_query", "MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 3.0}]),
        ["WH_TRXS_QUERY"]).iloc[0]
    assert spelled["WAREHOUSE_NAME"] == "wh_trxs_query" and spelled["MAX_CLUSTER_COUNT"] == 3.0


def test_with_cluster_use_marks_only_checked_rows():
    profile = pd.DataFrame([_wh("wh_trxs_transform", MAX_CLUSTER_COUNT=4.0),
                            _wh("WH_ALFA_LOAD_PRD", MAX_CLUSTER_COUNT=4.0),
                            _wh("WH_SINGLE", MAX_CLUSTER_COUNT=1.0)])
    summary = cluster_use_summary(_hist("WH_TRXS_TRANSFORM", "WH_ALFA_LOAD_PRD"), profile,
                                  cluster_check_targets(profile))
    out = with_cluster_use(profile, summary, 35).set_index("WAREHOUSE_NAME")
    assert out.loc["wh_trxs_transform", "CLUSTER_CHECK_DAYS"] == 35                 # case-insensitive match
    assert out.loc["wh_trxs_transform", "AT_CAP_HOUR_COUNT"] == 57
    assert out.loc["WH_ALFA_LOAD_PRD", "PEAK_CLUSTERS"] == 3
    single = out.loc["WH_SINGLE"]
    for col in ("CLUSTER_CHECK_DAYS", "ACTIVE_HOUR_COUNT", "PEAK_CLUSTERS", "P95_PEAK_CLUSTERS",
                "AT_CAP_HOUR_COUNT"):
        assert math.isnan(single[col]), col
    assert cluster_cap_state(single) == CAP_NOT_CHECKED
    assert cluster_cap_state(out.loc["wh_trxs_transform"]) == CAP_REACHED
    assert cluster_cap_state(out.loc["WH_ALFA_LOAD_PRD"]) == CAP_NOT_REACHED
    assert "CLUSTER_CHECK_DAYS" not in profile.columns                            # works on a copy
    empty = cluster_use_summary(None, profile, [])
    assert with_cluster_use(profile, empty, 35) is profile                         # nothing checked: unchanged
    assert cluster_cap_state(pd.Series({"CLUSTER_CHECK_DAYS": 35.0, "ACTIVE_HOUR_COUNT": 0.0})) == CAP_NO_QUERIES


# ---------------------------------------------------------------------------
# 7.6 - 7.8 the gate on the verdict
# ---------------------------------------------------------------------------

def test_the_gate_on_the_verdict():
    out = _sized(
        _wh("REACHED", MAX_CLUSTER_COUNT=4.0, SCALING_POLICY="STANDARD", **_state(CAP_REACHED, 4.0)),
        _wh("BELOW", MAX_CLUSTER_COUNT=4.0, SCALING_POLICY="STANDARD", **_state(CAP_NOT_REACHED, 4.0)),
        _wh("UNCHECKED", MAX_CLUSTER_COUNT=4.0, SCALING_POLICY="STANDARD"),
        _wh("NOQ", MAX_CLUSTER_COUNT=4.0, SCALING_POLICY="STANDARD", **_state(CAP_NO_QUERIES, 4.0)),
        _wh("UNKNOWN"),
        _wh("SINGLE", MAX_CLUSTER_COUNT=1.0, SCALING_POLICY="STANDARD"),
        _wh("ECON", MAX_CLUSTER_COUNT=3.0, SCALING_POLICY="ECONOMY", **_state(CAP_NOT_REACHED, 3.0)),
        _wh("TEN_BELOW", MAX_CLUSTER_COUNT=10.0, **_state(CAP_NOT_REACHED, 10.0)),
        _wh("TEN_REACHED", MAX_CLUSTER_COUNT=10.0, **_state(CAP_REACHED, 10.0)),
        _wh("TEN_UNCHECKED", MAX_CLUSTER_COUNT=10.0),
        _wh("BELOW_LONG", p95=240.0, MAX_CLUSTER_COUNT=4.0, **_state(CAP_NOT_REACHED, 4.0)),
    )
    rec, why = out["RECOMMENDATION"], out["RATIONALE"]
    assert rec["REACHED"] == RECOMMEND_SCALE_OUT
    assert ("queries reached cluster 4 of 4 in 57 hours of the last 35 days: raise MAX_CLUSTER_COUNT to 5"
            in why["REACHED"])
    assert rec["BELOW"] == RECOMMEND_BELOW_CAP
    assert "no query ran above cluster 3 of 4" in why["BELOW"]
    assert "A higher MAX_CLUSTER_COUNT would not help" in why["BELOW"]
    assert "raise MAX_CLUSTER_COUNT to" not in why["BELOW"] and "in the last 35 days" in why["BELOW"]
    for name in ("UNCHECKED", "NOQ"):
        assert rec[name] == RECOMMEND_SCALE_OUT
        assert "raise MAX_CLUSTER_COUNT to" not in why[name]
        assert "Add a cluster rather than a bigger size" not in why[name]
    assert "was not checked" in why["UNCHECKED"]
    assert "found no query with a cluster number" in why["NOQ"] and "last 35 days" in why["NOQ"]
    # untouched branches: byte-identical to v4.603 (ECONOMY even with not-reached columns on it)
    for name in ("UNKNOWN", "SINGLE", "ECON"):
        assert (rec[name], why[name]) == (RECOMMEND_SCALE_OUT, _V4603[name]), name
    # at the generator's cap: never reached -> below-cap; reached or unchecked -> the v4.603 split text
    assert rec["TEN_BELOW"] == RECOMMEND_BELOW_CAP and "no query ran above cluster 3 of 10" in why["TEN_BELOW"]
    for name in ("TEN_REACHED", "TEN_UNCHECKED"):
        assert why[name] == _LEAD + "Already at 10 clusters — split the workload across warehouses."
    # a long peak-day p95 on a below-cap row is context, humanized, never raw seconds
    assert rec["BELOW_LONG"] == RECOMMEND_BELOW_CAP
    assert ("Peak-day p95 is 4m — long queries holding the slots point to a size-up"
            in why["BELOW_LONG"]) and "240" not in why["BELOW_LONG"]
    # spill still routes to size-up first, whatever the cluster check says
    spill = _sized(_wh("S", spill=_SPILL, MAX_CLUSTER_COUNT=4.0, **_state(CAP_NOT_REACHED, 4.0)))
    assert spill.loc["S", "RECOMMENDATION"] == RECOMMEND_SIZE_UP
    # singular hour
    one = _sized(_wh("Q", MAX_CLUSTER_COUNT=3.0, **_state(CAP_REACHED, 3.0, at_cap=1.0)))
    assert "reached cluster 3 of 3 in 1 hour of the last 35 days" in one.loc["Q", "RATIONALE"]


@pytest.mark.parametrize("policy", ["STANDARD", ""])
def test_raise_text_only_with_hours_at_cap(policy):
    """The invariant: "raise MAX_CLUSTER_COUNT to N+1" (N > 1) appears only when AT_CAP_HOUR_COUNT > 0."""
    for mx in range(2, 10):
        for state in (CAP_REACHED, CAP_NOT_REACHED, CAP_NO_QUERIES, CAP_NOT_CHECKED):
            extra = {"MAX_CLUSTER_COUNT": float(mx), **_state(state, float(mx))}
            if policy:
                extra["SCALING_POLICY"] = policy
            row = _sized(_wh("W", **extra)).loc["W"]
            said = f"raise MAX_CLUSTER_COUNT to {mx + 1}" in row["RATIONALE"]
            assert said == (state == CAP_REACHED), (mx, state, row["RATIONALE"])
            assert said == (_state(state, float(mx)).get("AT_CAP_HOUR_COUNT", 0.0) > 0)
            plan = scale_out_plan(row)
            assert plan["prefill"] == (state == CAP_REACHED) and plan["max"] == (mx + 1 if said else mx)


def test_below_cap_books_nothing_and_counts_as_pressure():
    assert RECOMMEND_BELOW_CAP in UP_VERDICTS
    assert {RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP, RECOMMEND_UP, RECOMMEND_BELOW_CAP} == UP_VERDICTS
    df = pd.DataFrame([
        _wh("BELOW", MAX_CLUSTER_COUNT=4.0, **_state(CAP_NOT_REACHED, 4.0)),
        _wh("REACHED", MAX_CLUSTER_COUNT=4.0, **_state(CAP_REACHED, 4.0)),
        _wh("UNCHECKED", MAX_CLUSTER_COUNT=4.0, SCALING_POLICY="STANDARD"),
        _wh("ECON", MAX_CLUSTER_COUNT=3.0, SCALING_POLICY="ECONOMY"),
        _wh("TEN", MAX_CLUSTER_COUNT=10.0),
        _wh("SINGLE", MAX_CLUSTER_COUNT=1.0),
        _wh("S", queued_sec=0.0, spill=_SPILL),
        _wh("IDLE", queued_sec=0.0, idle=80.0),
        _wh("DOWN", queued_sec=0.0, p95=3.0, idle=40.0),
    ])
    out = size_recommendations(df, 3.68, 7)
    below = out[out["RECOMMENDATION"] == RECOMMEND_BELOW_CAP]
    assert len(below) == 1 and bool(below["ACTIONABLE"].iloc[0])
    assert float(below["POTENTIAL_MONTHLY_SAVING_USD"].iloc[0]) == 0.0
    order = list(out["RECOMMENDATION"])
    assert order.index(RECOMMEND_BELOW_CAP) < order.index(RECOMMEND_SUSPEND) < order.index(RECOMMEND_DOWN)
    s = sizing_summary(out)
    assert s["below_cap"] == 1
    assert s["scale_out"] == 5                         # REACHED, UNCHECKED, ECON, TEN, SINGLE — not BELOW
    assert s["up"] == 7                                # + BELOW + S (size up)
    # only the unchecked multi-cluster non-ECONOMY rows below the generator cap: the rows whose text says so
    assert s["cap_unchecked"] == 1
    unchecked_texts = out.loc[out["RATIONALE"].str.contains("was not checked"), "WAREHOUSE_NAME"].tolist()
    assert unchecked_texts == ["UNCHECKED"]
    empty = sizing_summary(pd.DataFrame())
    assert empty["below_cap"] == empty["cap_unchecked"] == 0
    legacy = pd.DataFrame({"RECOMMENDATION": [RECOMMEND_SCALE_OUT], "POTENTIAL_MONTHLY_SAVING_USD": [0.0]})
    assert sizing_summary(legacy)["cap_unchecked"] == 0 and sizing_summary(legacy)["scale_out"] == 1
    no_policy = size_recommendations(pd.DataFrame([_wh("U", MAX_CLUSTER_COUNT=4.0)]), 3.68, 7)
    assert sizing_summary(no_policy)["cap_unchecked"] == 1


# ---------------------------------------------------------------------------
# 7.9 / 7.10 the review-only prefill and its caption
# ---------------------------------------------------------------------------

def test_scale_out_plan_prefills_only_a_reached_cap():
    reached = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 4.0,
                                        **_state(CAP_REACHED, 4.0)}))
    assert (reached["prefill"], reached["min"], reached["max"], reached["cap"]) == (True, 1, 5, CAP_REACHED)
    assert reached["note"] == ("Raises MAX_CLUSTER_COUNT 4 → 5: queries reached cluster 4 of 4 in 57 hours of the "
                               "last 35 days. MIN stays 1 so the extra cluster runs only while queries queue. "
                               + _EDITION_UNSEEN)
    notes = {
        CAP_NOT_CHECKED: ("Whether queries ever reach cluster 4 of 4 was not checked, so no MAX_CLUSTER_COUNT "
                          "change is prefilled."),
        CAP_NO_QUERIES: ("The cluster-cap check found no query with a cluster number on this warehouse in the "
                         "last 35 days, so no MAX_CLUSTER_COUNT change is prefilled."),
        CAP_NOT_REACHED: ("Queries never reached cluster 4 of 4 in the last 35 days, so no MAX_CLUSTER_COUNT "
                          "change is prefilled."),
    }
    for state, note in notes.items():
        plan = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 4.0,
                                         **_state(state, 4.0)}))
        assert (plan["prefill"], plan["max"], plan["cap"], plan["note"]) == (False, 4, state, note), state
        assert plan["known"] and not plan["at_cap"] and not plan["policy_to_standard"]
    single = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 1.0}))
    assert (single["prefill"], single["min"], single["max"], single["cap"]) == (True, 1, 2, "")
    assert single["note"] == ("Raises MAX_CLUSTER_COUNT 1 → 2; MIN stays 1 so the extra cluster runs only while "
                              "queries queue. " + _EDITION_UNSEEN)                    # byte-identical to v4.603
    econ = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 3.0, "SCALING_POLICY": "ECONOMY",
                                     **_state(CAP_NOT_REACHED, 3.0)}))
    assert econ["policy_to_standard"] and not econ["prefill"] and econ["max"] == 3
    unknown = scale_out_plan(pd.Series({"WAREHOUSE_NAME": "W"}))
    assert unknown["known"] is False and not unknown["prefill"]
    capped = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 10.0,
                                       **_state(CAP_REACHED, 10.0)}))
    assert capped["at_cap"] and not capped["prefill"] and capped["max"] == 10


def test_scale_out_caption_for_gated_plans():
    from app.ui.pages.cost_parts.optimize import _scale_out_caption

    for state in (CAP_NOT_CHECKED, CAP_NO_QUERIES, CAP_NOT_REACHED):
        plan = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 4.0,
                                         **_state(state, 4.0)}))
        cap = _scale_out_caption(plan)
        assert cap == (plan["note"] + " No scale-out statement is generated here."
                       + " The resize below is the size-up route."), state
        assert "run it from" not in cap
    reached = _scale_out_caption(scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 4.0,
                                                           **_state(CAP_REACHED, 4.0)})))
    assert "run it from Operations ▸ Emergency ▸ Cluster range (audited)." in reached
    assert "A wider cluster range adds credits while queries queue, so no saving is booked" in reached


# ---------------------------------------------------------------------------
# 7.11 / 7.12 the SQL builder and its canary
# ---------------------------------------------------------------------------

def test_warehouse_cluster_use_builder_shape():
    from test_p4_filter_matrix import _escapes_a_literal

    sql = insights_sql.warehouse_cluster_use(["wh_b", "WH_A", "wh_a"])
    tree = sqlglot.parse_one(sql, read="snowflake")
    assert tree.named_selects == ["WAREHOUSE_NAME", "PEAK_CLUSTER", "HOUR_COUNT"]
    for frag in ("MAX(q.CLUSTER_NUMBER) AS PEAK_CLUSTER", "DATE_TRUNC('hour', q.START_TIME)",
                 "q.CLUSTER_NUMBER IS NOT NULL", "UPPER(q.WAREHOUSE_NAME) IN ('WH_A', 'WH_B')",
                 "DATEADD('day', -35, CURRENT_TIMESTAMP())", "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q"):
        assert frag in sql, frag
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in insights_sql.warehouse_cluster_use(["WH_A"], 400)
    assert "DATEADD('day', -60, CURRENT_TIMESTAMP())" in insights_sql.warehouse_cluster_use(["WH_A"], 60)
    none = insights_sql.warehouse_cluster_use(())
    assert "IN (NULL)" in none and sqlglot.parse_one(none, read="snowflake") is not None
    hostile = insights_sql.warehouse_cluster_use(["WH'X ZZINJZZ"])
    assert "'WH''X ZZINJZZ'" in hostile and not _escapes_a_literal(hostile)
    assert not re.search(r"\bLIMIT\b", sql) and "COMPANY_FOR_WAREHOUSE" not in sql
    many = insights_sql.warehouse_cluster_use([f"WH_{i:03d}" for i in range(150)])
    assert many.count("'WH_") == insights_sql.CLUSTER_USE_MAX_WAREHOUSES


def test_canary_registration():
    entries = dict(canary.CANARIES)
    assert entries["insights.warehouse_cluster_use"]() == insights_sql.warehouse_cluster_use(("WH_ALFA_ADMIN",), 1)
    # CLUSTER_NUMBER is a standard QUERY_HISTORY column (probe W1c): a missing column must FAIL, never be a gap
    assert "insights.warehouse_cluster_use" not in canary.EXPECTED_GAPS


# ---------------------------------------------------------------------------
# 7.13 / 7.14 the page wiring (source level) and the Operations help
# ---------------------------------------------------------------------------

def _body(src: str, header: str) -> str:
    return src.split(header, 1)[1].split("\ndef ", 1)[0]


def _flat(src: str) -> str:
    """Adjacent string literals joined (the implicit concatenation Python does), so a lock reads the
    sentence a viewer reads, not the line wrapping."""
    return re.sub(r'"\s*\n\s*f?"', "", src)


def test_optimize_wiring():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    chk = _body(opt, "def _cluster_cap_check(")
    assert '"""Fragment:' not in chk[:400]                  # a plain function, not a fragment
    assert chk.index('key="sizing_cluster_check"') < chk.index("insights_sql.warehouse_cluster_use(")
    for needed in ('tier="historical"', "max_rows=0", "guard(res", "with_cluster_use(",
                   "cluster_check_days(", 'toggle_cost_hint("cluster_use")', 'key=f"cluster_use_{company}_{chk_days}"',
                   'empty_state("unavailable"', "cluster_use_summary(res.df, sizing_df, targets)"):
        assert needed in chk, needed
    for banned in ("probe=True", "st.info(", "st.success("):
        assert banned not in chk, banned
    # the toggle's label and help stay static: the window and the count live in the caption
    toggle = chk.split('st.toggle("Check cluster use (multi-cluster warehouses)"', 1)[1].split("):", 1)[0]
    assert "{" not in toggle
    tab = _body(opt, "def _optimization_tab(")
    assert tab.index("_cluster_cap_check(") < tab.index("sized = size_recommendations(_sizing_df")
    assert tab.index("_sizing_df = with_warehouse_settings(prof_res.df, _whs_df)") < tab.index("_cluster_cap_check(")
    assert 'elif _so["prefill"]:' in tab and 'elif _so["known"] and not _so["at_cap"]:' not in tab
    assert "RECOMMEND_BELOW_CAP" in tab and "st.caption(_BELOW_CAP_CAPTION)" in tab
    assert tab.index("RECOMMEND_BELOW_CAP") < tab.index('st.selectbox("Resize to"')   # the size-up route stays
    assert '"PEAK_CLUSTERS", "AT_CAP_HOUR_COUNT"' in tab
    assert "summary['below_cap']" in tab and "summary['cap_unchecked']" in tab
    assert "stays queued" not in opt and "closed in #38 as a $0 lever" in opt
    assert opt.count("ACCOUNT_USAGE") <= 6
    assert read("app/ui/pages/operations.py").count("ACCOUNT_USAGE") == 42


def test_operations_help_says_the_cap_is_not_checked():
    ops = read("app/ui/pages/operations.py")
    body = _flat(_body(ops, "def _wh_sizing_efficiency"))
    assert ("Sustained overload queueing without remote spill (concurrency). This count does not check whether a "
            "multi-cluster warehouse ever reaches its MAX_CLUSTER_COUNT: raise the maximum (multi-cluster needs "
            "Enterprise edition) only where it does; otherwise size up or split the workload. Cost Intelligence ▸ "
            "Optimization & Savings ▸ Idle & sizing checks it per warehouse (Check cluster use).") in body
    assert "raise MAX_CLUSTER_COUNT (multi-cluster needs Enterprise edition) or split the workload." not in body
    # the v4.603 wrapped spelling is gone from the raw source too
    assert ('raise "\n                     "MAX_CLUSTER_COUNT (multi-cluster needs Enterprise edition) or split the '
            'workload."') not in ops


# ---------------------------------------------------------------------------
# 7.15 the new columns are counts, never durations
# ---------------------------------------------------------------------------

def test_cluster_use_columns_are_counts_not_durations():
    from app.ui.components import _COUNT_SUFFIXES, _duration_unit_for_column

    for col in ("ACTIVE_HOUR_COUNT", "AT_CAP_HOUR_COUNT", "PEAK_CLUSTERS", "P95_PEAK_CLUSTERS", "HOUR_COUNT",
                "CLUSTER_CHECK_DAYS"):
        assert _duration_unit_for_column(col) is None, col
    for col in ("ACTIVE_HOUR_COUNT", "AT_CAP_HOUR_COUNT", "HOUR_COUNT"):
        assert col.endswith(_COUNT_SUFFIXES), col
