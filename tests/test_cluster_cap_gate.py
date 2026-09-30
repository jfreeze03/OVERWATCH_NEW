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
from tests._source import ROOT, read

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
    START_TIME >= midnight N days back (review r1 R1-7: it was now - N days, which left the first day
    partial), so the N whole days before today are fully covered; for N = 35 some day among them is the
    last of its month, for every day of 2024-2027 — even counting only N-1 of them. A 30-day window does
    NOT guarantee it (read on Aug 31: whole days Aug 1..Aug 30 and today — no month-end)."""
    def month_end(d: date) -> bool:
        return (d + timedelta(days=1)).day == 1

    def whole_days(today: date, n: int) -> list[date]:
        return [today - timedelta(days=k) for k in range(1, n + 1)]

    day = date(2024, 1, 1)
    while day <= date(2027, 12, 31):
        assert any(month_end(d) for d in whole_days(day, CLUSTER_CHECK_MIN_DAYS)[:-1]), day
        day += timedelta(days=1)
    assert not any(month_end(d) for d in whole_days(date(2026, 8, 31), 30))   # Aug 1..Aug 30


def test_a_calendar_window_is_read_from_its_first_midnight():
    """Review r1 R1-7: on Last month read Sep 30 the check is 60 days, and the builder's bound is the DATE 60
    days back in account time (Aug 1 00:00 Central) — the sizing window's first day in full, not from 14:00.
    The trailing profile anchors on a date too, so a 60/90-day trailing window is covered the same way."""
    today = date(2026, 9, 30)
    for start, served in ((date(2026, 8, 1), 31), (date(2026, 7, 2), 31)):      # 60 and 90 days back
        n = cluster_check_days(served, start, today)
        assert today - timedelta(days=n) == start
        sql = insights_sql.warehouse_cluster_use(["WH_A"], n)
        assert (f"q.START_TIME >= DATEADD('day', -{n}, CONVERT_TIMEZONE('America/Chicago', "
                "CURRENT_TIMESTAMP())::DATE)") in sql
    for served in (60, 90):
        sql = insights_sql.warehouse_cluster_use(["WH_A"], cluster_check_days(served, None, today))
        assert f"DATEADD('day', -{served}, CONVERT_TIMEZONE(" in sql and "CURRENT_TIMESTAMP())\n" not in sql
    # the docstring no longer over-claims: it names the midnight anchor
    assert "MIDNIGHT" in sizing.cluster_check_days.__doc__


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
    anchor = "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE"      # common.account_today_sql
    for frag in ("MAX(q.CLUSTER_NUMBER) AS PEAK_CLUSTER", "DATE_TRUNC('hour', q.START_TIME)",
                 "q.CLUSTER_NUMBER IS NOT NULL", "UPPER(q.WAREHOUSE_NAME) IN ('WH_A', 'WH_B')",
                 f"DATEADD('day', -35, {anchor})", "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q"):
        assert frag in sql, frag
    assert f"DATEADD('day', -90, {anchor})" in insights_sql.warehouse_cluster_use(["WH_A"], 400)
    assert f"DATEADD('day', -60, {anchor})" in insights_sql.warehouse_cluster_use(["WH_A"], 60)
    assert "-35, CURRENT_TIMESTAMP()" not in sql                  # review r1 R1-7: never now minus N days
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
    # review r1 R1-10: the range coverage is judged BEFORE "no multi-cluster warehouse" is said
    assert chk.index("cluster_range_coverage(sizing_df)") < chk.index("No warehouse in this profile has")
    assert 'empty_state("needs_setup"' in chk
    # review r1 R1-4: the resize picker's default follows the verdict; options == what resize_fix accepts
    assert ('_rs_idx, _rs_note = resize_picker_default(srow.get("RECOMMENDATION"), srow.get("CURRENT_SIZE"),'
            in tab)
    assert 'st.selectbox("Resize to", list(remediation.RESIZE_SIZES), index=_rs_idx,' in tab
    assert '["XSMALL", "SMALL", "MEDIUM", "LARGE"]' not in tab
    # review r1 R1-9: the evidence row's cluster columns carry the check window, and the CSV carries it too
    assert '"AT_CAP_HOUR_COUNT", "CLUSTER_CHECK_DAYS",' in tab
    assert 'cluster_check_label("Peak cluster", _cd)' in tab and 'cluster_check_label("Hours at cap", _cd)' in tab
    # review r2 R2-8: ... from the SELECTED row's check window, each with the not-the-sizing-window help (a
    # `_cd = None` or a dropped help passed every test; the AppTest twin reads the rendered labels too)
    evidence = tab.split('st.markdown("**Selected recommendation evidence**")', 1)[1].split("if sel_sz is not None", 1)[0]
    assert '_cd = srow.get("CLUSTER_CHECK_DAYS")' in evidence
    assert evidence.index('_cd = srow.get("CLUSTER_CHECK_DAYS")') < evidence.index('cluster_check_label("Peak cluster"')
    assert evidence.count("_cd = ") == 1 and evidence.count("help=_cd_help") == 2
    assert ('cluster_check_label("Peak cluster", _cd), format="%d", help=_cd_help),' in evidence
            and 'cluster_check_label("Hours at cap", _cd), format="%d", help=_cd_help),' in evidence)
    assert "not the \"\n                            \"sizing window of the other columns." in evidence


def test_operations_help_says_the_cap_is_not_checked():
    ops = read("app/ui/pages/operations.py")
    body = _flat(_body(ops, "def _wh_sizing_efficiency"))
    assert ("Sustained overload queueing without remote spill (concurrency). This count does not check whether a "
            "multi-cluster warehouse ever reaches its MAX_CLUSTER_COUNT: raise the maximum (multi-cluster needs "
            "Enterprise edition) only where it does; otherwise size up or split the workload. Cost Intelligence ▸ "
            "Optimization & Savings ▸ Idle & sizing checks it per warehouse (Check cluster use).") in body
    assert "raise MAX_CLUSTER_COUNT (multi-cluster needs Enterprise edition) or split the workload." not in body
    # review r1 R1-5: a VISIBLE caption under the table (the help above is a hover tooltip only)
    tbl = body.index("entity_nav_table(_sized[_cols]")
    note = body.index('st.caption(unchecked_cap_note(_sum["cap_unchecked"]))')
    assert tbl < note < body.index('st.caption("Health = 100')
    assert 'if _sum["cap_unchecked"]:' in body[tbl:note]
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


# ---------------------------------------------------------------------------
# v4.604.0 review r1: the pure helpers behind R1-4 / R1-5 / R1-9 / R1-10
# ---------------------------------------------------------------------------

def test_resize_picker_opens_on_a_size_up_for_pressure_verdicts():
    """R1-4: the below-cap pane calls the resize "the size-up route", so a capacity-pressure verdict opens
    the picker one size UP — never on XSMALL, which for a Small warehouse was a downsize that projected a
    saving and logged it."""
    from app.logic import remediation
    from app.logic.sizing import resize_picker_default

    opts = remediation.RESIZE_SIZES
    assert opts == ("XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE", "XXLARGE")
    for verdict in (RECOMMEND_BELOW_CAP, RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP, RECOMMEND_UP):
        assert resize_picker_default(verdict, "Small", opts) == (opts.index("MEDIUM"), ""), verdict
        assert resize_picker_default(verdict, "X-Small", opts) == (opts.index("SMALL"), "")
        assert resize_picker_default(verdict, "LARGE", opts) == (opts.index("XLARGE"), "")
        assert resize_picker_default(verdict, "X-Large", opts) == (opts.index("XXLARGE"), "")
    # every other verdict keeps the v4.603 default (first option, no note), whatever the size
    for verdict in (RECOMMEND_DOWN, RECOMMEND_SUSPEND, "Keep", ""):
        assert resize_picker_default(verdict, "Small", opts) == (0, "")
        assert resize_picker_default(verdict, None, opts) == (0, "")
    # the picker's XXLARGE spelling is the ladder's 2XLARGE, so an upsize to it is never "size unknown"
    assert sizing.normalize_size("XXLARGE") == "2XLARGE"
    assert sizing.normalize_size("2X-Large") == "2XLARGE"


def test_resize_picker_opens_on_nothing_where_no_size_up_is_offered():
    """Review r2 R2-2 / R2-7: under a capacity-pressure verdict with no size up to offer, the picker opened on a
    no-op (2X-Large), a DOWNSIZE that projected and logged a saving (3X-Large and up, onto XXLARGE), or XSMALL
    (unknown size). It now opens with NO size picked (index None) and says why, in the picker's spelling."""
    from app.logic import remediation
    from app.logic.sizing import RESIZE_PICK_PROMPT, picker_size_label, resize_picker_default

    opts = remediation.RESIZE_SIZES
    for verdict in (RECOMMEND_BELOW_CAP, RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP, RECOMMEND_UP):
        idx, note = resize_picker_default(verdict, "2X-Large", opts)
        assert idx is None and note == (
            "The next size up from XXLARGE is not offered here, so every option is this size (no change) or a "
            "downsize — none is the size-up route." + RESIZE_PICK_PROMPT), verdict
        assert "2XLARGE" not in note                          # the option reads XXLARGE (R2-2's label mismatch)
        for big in ("3X-Large", "4X-Large", "6X-Large"):
            idx, note = resize_picker_default(verdict, big, opts)
            assert idx is None and note == (
                f"This warehouse ({sizing.normalize_size(big)}) is larger than every size offered here, so every "
                "option is a downsize — none is the size-up route." + RESIZE_PICK_PROMPT), (verdict, big)
        for unknown in (None, "", "weird", float("nan")):
            idx, note = resize_picker_default(verdict, unknown, opts)
            assert idx is None and note.startswith("The current size of this warehouse is unknown"), unknown
            assert note.endswith(RESIZE_PICK_PROMPT)
    assert RESIZE_PICK_PROMPT == " The picker opens with no size picked: pick one to see the statement."
    assert [picker_size_label(s, opts) for s in ("2X-Large", "XXLARGE", "Small", "3X-Large", None)] == [
        "XXLARGE", "XXLARGE", "SMALL", "3XLARGE", ""]


def test_the_resize_pane_shows_nothing_until_a_size_is_picked():
    """Review r2 R2-2 (the floor leg skips the AppTest twin in tests/test_cluster_cap_shaped.py): with no size
    picked, the pane renders no statement, no saving caption and no Execute; the picker default can be None."""
    import ast

    tab = _body(read("app/ui/pages/cost_parts/optimize.py"), "def _optimization_tab(")
    assert ('target_size = st.selectbox("Resize to", list(remediation.RESIZE_SIZES), index=_rs_idx,\n'
            '                                           key=f"sizing_to_{srow[\'WAREHOUSE_NAME\']}_{_rs_cur}_{_rs_idx}",\n'
            '                                           placeholder="Pick a size")') in tab
    fn = next(n for n in ast.walk(ast.parse(read("app/ui/pages/cost_parts/optimize.py")))
              if isinstance(n, ast.FunctionDef) and n.name == "_optimization_tab")
    gates = [n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == "target_size is not None"]
    assert len(gates) == 1
    (gate,) = gates
    assert not gate.orelse
    inside = ast.unparse(gate)
    for needed in ("remediation.resize_fix(", "st.code(stmt_sz", "Projected saving", "Resizing UP",
                   "confirm_gate(", "Execute resize + log", "write_gate_open('sizing')", "REMEDIATION_LOG",
                   "SAVINGS_LEDGER", "stamp_write('sizing', ok)"):
        assert needed in inside, needed
    # nothing that shows or runs a resize sits outside the gate
    rest = tab.replace(tab[tab.index("if target_size is not None:"):tab.index("_whatif_panel(sized")], "")
    for banned in ("resize_fix(", "stmt_sz", "Execute resize", 'write_gate_open("sizing")'):
        assert banned not in rest, banned
    # the captions spell both sizes as the picker does (XXLARGE, never 2XLARGE beside an XXLARGE option)
    assert "_cur_label = picker_size_label(_cur_size, remediation.RESIZE_SIZES)" in tab
    assert "resizing {_cur_label} → \"\n" in tab and 'f"{target_size} (only idle-hour credits' in tab
    assert 'st.caption(f"Resizing UP {_cur_label} → {target_size} raises cost — no saving "' in tab


def _picker_script():
    """The Resize to picker alone, keyed as the page keys it (inputs through session state: AppTest.from_function
    runs this source in its own script)."""
    import streamlit as st

    from app.logic import remediation
    from app.logic.sizing import RECOMMEND_BELOW_CAP, normalize_size, resize_picker_default

    size = st.session_state["t_size"]
    idx, _note = resize_picker_default(RECOMMEND_BELOW_CAP, size, remediation.RESIZE_SIZES)
    cur = normalize_size(size) or "UNKNOWN"
    key = f"sizing_to_WH_LOW_{cur}_{idx}" if st.session_state["t_scoped"] else "sizing_to_WH_LOW"
    st.selectbox("Resize to", list(remediation.RESIZE_SIZES), index=idx, key=key, placeholder="Pick a size")


@pytest.mark.parametrize("scoped", [True, False])
def test_the_picker_key_carries_what_its_default_is_computed_from(scoped):
    """Review r3 R3-1, on every leg (the floor's streamlit 1.52.2 as well; the page-level twin is in
    tests/test_cluster_cap_shaped.py). Streamlit leaves `index` out of a keyed selectbox's identity, so the picker
    keyed on the warehouse alone (scoped=False) kept XXLARGE while the selected row's size went X-Large -> 2X-Large
    -> Large -> 3X-Large: beside the no-size-picked note it showed an ALTER, a saving and Execute. The page's key
    carries the current size and the default index, so each new default is a new widget."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_picker_script, default_timeout=30)
    at.session_state["t_scoped"] = scoped
    seen = []
    for size in ("X-Large", "2X-Large", "Large", "3X-Large"):
        at.session_state["t_size"] = size
        at.run()
        assert not at.exception
        seen.append(at.selectbox[0].value)
    assert seen == (["XXLARGE", None, "XLARGE", None] if scoped else ["XXLARGE"] * 4)
    tab = _body(read("app/ui/pages/cost_parts/optimize.py"), "def _optimization_tab(")
    assert '_rs_cur = normalize_size(srow.get("CURRENT_SIZE")) or "UNKNOWN"' in tab
    assert tab.index("_rs_cur = ") < tab.index('key=f"sizing_to_{srow[\'WAREHOUSE_NAME\']}_{_rs_cur}_{_rs_idx}"')
    # the optional half: a successful resize clears the typed name before the confirm input next renders
    assert ('if st.session_state.pop("_sizing_clear_confirm", False):\n'
            '                        st.session_state["sizing_confirm"] = ""\n'
            '                    if (confirm_gate(') in tab
    assert ('log_ui_event("remediation_exec", page=_PAGE)\n'
            '                            st.session_state["_sizing_clear_confirm"] = True\n') in tab


def test_cluster_range_coverage():
    """R1-10: an empty SHOW (no MAX_CLUSTER_COUNT column) and a SHOW that lists none of the profile (NaN on
    every row) are both (0, n): ranges unknown, not zero multi-cluster warehouses."""
    from app.logic.sizing import cluster_range_coverage

    names = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A", "wh_a ", "WH_B"]})
    assert cluster_range_coverage(names) == (0, 2)
    assert cluster_range_coverage(names.assign(MAX_CLUSTER_COUNT=math.nan)) == (0, 2)
    assert cluster_range_coverage(names.assign(MAX_CLUSTER_COUNT=[4.0, 4.0, math.nan])) == (1, 1)
    assert cluster_range_coverage(names.assign(MAX_CLUSTER_COUNT=[1.0, 1.0, 1.0])) == (2, 0)
    assert cluster_range_coverage(pd.DataFrame()) == (0, 0) and cluster_range_coverage(None) == (0, 0)


def test_a_partly_listed_profile_names_its_unlisted_warehouses():
    """Review r2 R2-3: the glossary said the partly-listed caption names the unknown warehouses; it only counted
    them. cluster_range_unknown gives the names (cluster_range_coverage's rule), capped with 'and N more'."""
    from app.logic.sizing import (
        UNKNOWN_RANGE_NAMES_CAP,
        cluster_range_coverage,
        cluster_range_unknown,
        unknown_range_sentence,
    )

    names = pd.DataFrame({"WAREHOUSE_NAME": ["wh_b ", "WH_A", "WH_B", "WH_C"]})
    assert cluster_range_unknown(names) == ["WH_A", "WH_B", "WH_C"]             # no MAX_CLUSTER_COUNT column
    frame = names.assign(MAX_CLUSTER_COUNT=[math.nan, 4.0, math.nan, 1.0])
    assert cluster_range_unknown(frame) == ["WH_B"] and cluster_range_coverage(frame) == (2, 1)
    assert cluster_range_unknown(pd.DataFrame()) == [] and cluster_range_unknown(None) == []
    assert unknown_range_sentence([]) == ""
    assert unknown_range_sentence(["WH_B"]) == (
        " 1 warehouse(s) in this profile are not in SHOW WAREHOUSES (WH_B), so their cluster range is unknown and "
        "they are not checked.")
    many = [f"WH_{i:02d}" for i in range(UNKNOWN_RANGE_NAMES_CAP + 2)]
    line = unknown_range_sentence(many)
    assert line.startswith(f" {len(many)} warehouse(s) in this profile are not in SHOW WAREHOUSES (WH_00, ")
    assert f"WH_{UNKNOWN_RANGE_NAMES_CAP - 1:02d} and 2 more)" in line and f"WH_{UNKNOWN_RANGE_NAMES_CAP:02d}" not in line


def test_cluster_check_label_names_the_check_window():
    """R1-9: the selected-row evidence mixes the sizing window with the cluster-cap check window."""
    from app.logic.sizing import cluster_check_label

    assert cluster_check_label("Hours at cap", 35.0) == "Hours at cap (last 35 days)"
    assert cluster_check_label("Peak cluster", 60) == "Peak cluster (last 60 days)"
    for none in (None, math.nan, 0, ""):
        assert cluster_check_label("Hours at cap", none) == "Hours at cap"


def test_unchecked_cap_note_names_the_check():
    """R1-5: the Operations table's visible disclosure."""
    from app.logic.sizing import CLUSTER_CAP_CHECK_PATH, unchecked_cap_note

    assert unchecked_cap_note(0) == "" and unchecked_cap_note(None) == ""
    note = unchecked_cap_note(3)
    assert note.startswith('3 "Add a cluster (scale out)" row(s) above are on a multi-cluster warehouse whose '
                           "cluster cap this page does not check")
    assert "a higher MAX_CLUSTER_COUNT helps only if its queries reach the current maximum" in note
    assert CLUSTER_CAP_CHECK_PATH in note and f'"{RECOMMEND_BELOW_CAP}"' in note


# ---------------------------------------------------------------------------
# v4.604.0 review r1 R1-11: the other add-a-cluster surfaces say the cap must be checked first
# ---------------------------------------------------------------------------

def test_the_query_advisor_gates_its_cluster_advice():
    from app.logic.query_advisor import advise
    from app.logic.sizing import CLUSTER_CAP_CHECK_PATH

    findings, _ = advise({"ELAPSED_SEC": 10, "QUEUED_SEC": 7, "QUEUED_OVERLOAD_SEC": 6,
                          "QUEUED_PROVISIONING_SEC": 1})
    detail = next(f for f in findings if f.code == "queued").detail
    assert "raise MAX_CLUSTER_COUNT (multi-cluster) or move this workload" not in detail
    assert ("On a multi-cluster warehouse, raise MAX_CLUSTER_COUNT only if its queries reach the current maximum "
            f"({CLUSTER_CAP_CHECK_PATH} checks it) — below the cap, a higher maximum does not help.") in detail
    assert "add a cluster or move this workload to its own warehouse" in detail


def test_the_no_dominant_cause_queue_finding_carries_the_cap_check():
    """Review r3 R3-2 / R3-7: the advisor's hedged fallback serves two row shapes. With no overload/provisioning
    split (the older shape) it keeps its v4.588 wording, byte-locked by tests/test_cold_start_split.py. With the
    split known but no cause dominating most queued runs -- the live case on the fingerprint grain, whose builder
    always emits the split -- it said "add a cluster" with no cap check, on Operations ▸ Queries and as the fix
    queue's First fix. It keeps the hedge and now carries the rule; the points (so QOP) are unchanged."""
    from app.logic.query_advisor import advise
    from app.logic.query_opt import score_opportunities
    from app.logic.sizing import CLUSTER_CAP_CHECK_PATH, CLUSTER_CAP_QUALIFIER

    # a current-shape fingerprint (the reviewers' repro): 25s of a 28s run queued, split 12/13, and half of the
    # queued runs each way, so neither share reaches FINGERPRINT_SPLIT_DOMINANT_SHARE
    row = {"FINGERPRINT": "fp", "SAMPLE_TEXT": "select ...", "QUERY_TYPE": "SELECT", "WAREHOUSE_NAME": "WH",
           "WAREHOUSE_SIZE": "MEDIUM", "RUNS": 100, "TOTAL_EXEC_SEC": 1000.0, "ELAPSED_SEC": 28.0,
           "COMPILE_SEC": 0.5, "EXECUTION_SEC": 2.5, "QUEUED_SEC": 25.0, "QUEUED_OVERLOAD_SEC": 12.0,
           "QUEUED_PROVISIONING_SEC": 13.0, "QUEUED_RUN_PCT": 0.9, "PROVISIONING_QUEUED_RUN_PCT": 0.5,
           "OVERLOAD_QUEUED_RUN_PCT": 0.5, "GB_SCANNED": 5.0, "CACHE_PCT": 10.0, "LOCAL_SPILL_GB": 0.0,
           "REMOTE_SPILL_GB": 0.0, "ROWS_PRODUCED": 100.0, "PARTITIONS_SCANNED": 10.0, "PARTITIONS_TOTAL": 1000.0}
    hedged = ("Spent 25s queued (of 28.0s total) — either concurrency (add a cluster or size up for parallelism; "
              + CLUSTER_CAP_QUALIFIER + ") or warehouse resume overhead (lengthen AUTO_SUSPEND / keep it warm).")
    findings, _ = advise(row)
    queued = [f for f in findings if f.code in ("queued", "cold_start")]
    assert [f.code for f in queued] == ["queued"] and queued[0].detail == hedged
    assert CLUSTER_CAP_CHECK_PATH in queued[0].detail and "either concurrency" in queued[0].detail
    no_split = {k: v for k, v in row.items() if k not in ("QUEUED_OVERLOAD_SEC", "QUEUED_PROVISIONING_SEC",
                                                          "PROVISIONING_QUEUED_RUN_PCT", "OVERLOAD_QUEUED_RUN_PCT")}
    legacy = next(f for f in advise(no_split)[0] if f.code == "queued")
    assert CLUSTER_CAP_CHECK_PATH not in legacy.detail                  # the byte-locked older-shape text
    assert queued[0].points == legacy.points                           # only the text moved: QOP byte-stable
    scored, _ = score_opportunities(pd.DataFrame([row]))
    assert scored.iloc[0]["PATHOLOGY"] == "Concurrency starvation"
    assert scored.iloc[0]["FIRST_ACTION"] == hedged


def test_the_size_up_follow_up_is_gated_like_the_scale_out_verdict():
    """R2-4 / R2-11: a spill + queueing warehouse reads "Size up", and its rationale's follow-up said "if queueing
    persists after the resize, add a cluster." with no cap check, even on Idle & sizing where the check runs.
    Checked and never at the cap -> split instead; reached -> add one, saying so; otherwise the rule itself."""
    from app.logic.sizing import CLUSTER_CAP_QUALIFIER

    std = {"SCALING_POLICY": "STANDARD", "MIN_CLUSTER_COUNT": 1.0}
    out = _sized(
        _wh("LOW", spill=_SPILL, MAX_CLUSTER_COUNT=4.0, **std, **_state(CAP_NOT_REACHED, 4.0)),
        _wh("SAT", spill=_SPILL, MAX_CLUSTER_COUNT=4.0, **std, **_state("reached", 4.0)),
        _wh("UNCHECKED", spill=_SPILL, MAX_CLUSTER_COUNT=4.0, **std),
        _wh("UNKNOWN", spill=_SPILL),
        _wh("TEN", spill=_SPILL, MAX_CLUSTER_COUNT=10.0, **std, **_state("reached", 10.0)),
        _wh("TEN_UNCHECKED", spill=_SPILL, MAX_CLUSTER_COUNT=10.0, **std),
        _wh("TEN_NO_QUERIES", spill=_SPILL, MAX_CLUSTER_COUNT=10.0, **std, **_state(CAP_NO_QUERIES, 10.0)))
    assert set(out["RECOMMENDATION"]) == {RECOMMEND_SIZE_UP}
    why = out["RATIONALE"]
    assert ("if queueing persists after the resize, split the workload: in the last 35 days no query ran above "
            "cluster 3 of 4, so a higher MAX_CLUSTER_COUNT would not help.") in why["LOW"]
    assert "add a cluster" not in why["LOW"]
    assert ("if queueing persists after the resize, add a cluster: queries reached cluster 4 of 4 in 57 hours of "
            "the last 35 days.") in why["SAT"]
    for name in ("UNCHECKED", "UNKNOWN"):
        assert f"if queueing persists after the resize, add a cluster ({CLUSTER_CAP_QUALIFIER})." in why[name]
    # review r3 R3-3 / R3-6: at the generator's cap of 10 it says split whatever the check shows (as documented)
    for name in ("TEN", "TEN_UNCHECKED", "TEN_NO_QUERIES"):
        assert ("if queueing persists after the resize, split the workload across warehouses (already at 10 "
                "clusters).") in why[name], name
        assert CLUSTER_CAP_QUALIFIER not in why[name] and "add a cluster" not in why[name], name


def test_the_operations_cluster_advice_is_gated():
    ops = _flat(read("app/ui/pages/operations.py"))
    assert "Add a cluster (multi-cluster) or split the workload; don't rewrite" not in ops
    assert ('bad SQL. Add a cluster or split the workload (" + CLUSTER_CAP_QUALIFIER + "); '
            "don't rewrite the query.") in ops
    assert '(add a cluster / split the workload; " + CLUSTER_CAP_QUALIFIER + ");' in ops
    assert 'before users feel it (" + CLUSTER_CAP_QUALIFIER + ").' in ops
    assert 'first; add a cluster only if the queue persists (" + CLUSTER_CAP_QUALIFIER + ").' in ops


# ---------------------------------------------------------------------------
# v4.604.0 review r2 R2-4 / R2-11: a sweep, not a list of named sites
# ---------------------------------------------------------------------------

# Advice to add a cluster or raise the cluster maximum ...
_ADD_CLUSTER_RE = re.compile(r"\badd (?:a |another |more )?clusters?\b"
                             r"|\braises? (?:the )?(?:MAX_CLUSTER_COUNT|maximum)\b", re.IGNORECASE)
# ... must carry the cap check in the SAME string expression: the shared qualifier / check path, or the check's
# own result ("queries reached cluster n of n", "reaches its MAX_CLUSTER_COUNT ... Check cluster use").
_CAP_CHECK_RE = re.compile(r"\{CLUSTER_CAP_QUALIFIER\}|\{CLUSTER_CAP_CHECK_PATH\}|reached cluster"
                           r"|reach(?:es|ing)? (?:the|its) (?:current maximum|MAX_CLUSTER_COUNT)|Check cluster use")
# Exact texts that NAME the verdict rather than advise it.
_ADD_CLUSTER_LABELS = {
    "Add a cluster (scale out)": "sizing.RECOMMEND_SCALE_OUT, the verdict label",
    "Size up / add cluster": "sizing.RECOMMEND_UP, the legacy merged label (never emitted)",
    "Add a cluster": "the Operations KPI label (its help carries the check)",
}
# (file, enclosing function, fragment): advice that may omit the qualifier, and why.
_ADD_CLUSTER_ALLOWED = {
    ("app/logic/query_advisor.py", "advise",
     "Spent {queued_sec}s queued (of {elapsed}s total) — either concurrency (add a cluster or size up for "
     "parallelism) or warehouse resume overhead (lengthen AUTO_SUSPEND / keep it warm)."):
        "the split-UNKNOWN fallback only (a row without QUEUED_OVERLOAD_SEC / QUEUED_PROVISIONING_SEC, the older row "
        "shape): tests/test_cold_start_split.py locks it byte for byte (Next-Fifty #17 kept the v4.588 wording for "
        "that shape). The split-known, no-dominant-cause text carries the qualifier (review r3 R3-2 / R3-7; "
        "test_the_no_dominant_cause_queue_finding_carries_the_cap_check)",
    ("app/logic/sizing.py", "_pressure_verdict", "{lead} Add a cluster rather than a bigger size. {how}{tail}"):
        "the scale-out verdict itself, after its cap gate: the not-checked / no-queries / never-reached branches "
        "return earlier, so `how` here is single-cluster, ECONOMY, the generator cap, a reached cap, or an "
        "unknown range (which says the current setting is unknown)",
    ("app/logic/sizing.py", "_pressure_verdict", "Single-cluster today (MAX_CLUSTER_COUNT = 1): raise "):
        "a single-cluster warehouse: every query runs on cluster 1 of 1, so its cap is reached by definition",
    ("app/logic/sizing.py", "scale_out_plan", "Raises MAX_CLUSTER_COUNT {cur_max} → {cur_max + 1}; MIN stays "):
        "the single-cluster (1 -> 2) prefill note; the multi-cluster prefill note says the cap was reached",
}


def _str_text(node) -> str:
    """The text a viewer reads from a string expression: literals joined, every non-literal as {its source}."""
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _str_text(node.left) + _str_text(node.right)
    if isinstance(node, ast.IfExp):
        return _str_text(node.body) + " | " + _str_text(node.orelse)
    return "{" + ast.unparse(node) + "}"


def _add_cluster_texts():
    """(file, enclosing function, text) for every string expression under app/ that advises adding a cluster:
    each literal climbed to its whole concatenation (f-string, +, a conditional part); docstrings skipped."""
    import ast

    out = []
    for py in sorted((ROOT / "app").rglob("*.py")):
        rel = py.relative_to(ROOT).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        tops = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if isinstance(parent.get(node), ast.Expr):
                continue                                   # a docstring / bare string statement
            top = node
            while True:
                up = parent.get(top)
                if isinstance(up, ast.JoinedStr) or (isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add)) \
                        or (isinstance(up, ast.IfExp) and top is not up.test):
                    top = up
                    continue
                break
            tops[id(top)] = top
        for top in tops.values():
            text = _str_text(top)
            if not _ADD_CLUSTER_RE.search(text):
                continue
            fn, up = "", parent.get(top)
            while up is not None and not fn:
                fn = up.name if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""
                up = parent.get(up)
            out.append((rel, fn, text))
    return out


def test_every_add_a_cluster_string_carries_the_cap_check():
    """R2-4 / R2-11: r1 fixed the add-a-cluster sites it named and locked only those, so the query advisor's
    fallback and the Operations Size up help slipped through. Every string expression in app/ (sizing.py
    included) that advises adding a cluster or raising the cluster maximum must carry the cap check, or be a
    verdict label, or be allowlisted with its reason."""
    texts = _add_cluster_texts()
    assert len(texts) >= 12, texts                        # the sweep sees the known sites (not vacuous)
    used = set()
    bad = []
    for rel, fn, text in texts:
        if _CAP_CHECK_RE.search(text) or text in _ADD_CLUSTER_LABELS:
            continue
        hit = [key for key in _ADD_CLUSTER_ALLOWED if key[0] == rel and key[1] == fn and key[2] in text]
        if hit:
            used.update(hit)
            continue
        bad.append((rel, fn, text))
    assert not bad, "\n".join(f"{r} {f}(): {t}" for r, f, t in bad)
    assert used == set(_ADD_CLUSTER_ALLOWED), set(_ADD_CLUSTER_ALLOWED) - used   # no stale exemption
    assert {t for _r, _f, t in texts} >= set(_ADD_CLUSTER_LABELS)
