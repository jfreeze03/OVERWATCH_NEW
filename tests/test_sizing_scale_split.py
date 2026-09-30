"""Next-Fifty #38: the merged "Size up / add cluster" sizing verdict is split by the kind of
pressure — overload queueing without remote spill is CONCURRENCY (add a cluster / scale out), remote
spill is PER-QUERY memory (size up; with queueing too, size up first). App-only: no migration (the
cluster config rides on the profile via insights.with_warehouse_settings).

v4.604 (#38 remainder): on a multi-cluster warehouse the "raise MAX_CLUSTER_COUNT to N+1" advice and
prefill are GATED on the cluster-cap check (sizing.with_cluster_use, fed by the toggled
insights_sql.warehouse_cluster_use read), so the multi-cluster rows below carry the reached-cap columns;
their unchecked twins lock the "was not checked" side. tests/test_cluster_cap_gate.py covers the gate."""

from __future__ import annotations

import pandas as pd

from app.logic import remediation
from app.logic.insights import multi_cluster_evident, with_warehouse_settings
from app.logic.sizing import (
    CLUSTER_RANGE_CAP,
    LONG_P95_SEC,
    RECOMMEND_BELOW_CAP,
    RECOMMEND_DOWN,
    RECOMMEND_OBSERVE,
    RECOMMEND_SCALE_OUT,
    RECOMMEND_SIZE_UP,
    RECOMMEND_SUSPEND,
    RECOMMEND_UP,
    UP_VERDICTS,
    scale_out_plan,
    size_recommendations,
    sizing_summary,
)
from app.logic.wh_health import LONG_P95_SEC as HEALTH_LONG_P95_SEC
from tests._source import page_source, read

_Q45 = 7 * 45 * 60          # 45 min/day of overload queueing over a 7-day window
_SPILL = 9.0                # 1.29 GB/day remote spill over 7 days
# v4.604 (#38 remainder): a cluster-cap check that saw queries at the current maximum in 5 of 100 hours
_REACHED = {"CLUSTER_CHECK_DAYS": 35.0, "ACTIVE_HOUR_COUNT": 100.0, "PEAK_CLUSTERS": 3.0,
            "AT_CAP_HOUR_COUNT": 5.0}


def _wh(name, queued_sec=0.0, spill=0.0, p95=5.0, idle=0.0, active_days=7, **extra):
    row = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 1000,
           "ACTIVE_QUERY_DAYS": active_days, "P95_ELAPSED_SEC": p95, "QUEUED_SEC": queued_sec,
           "SPILL_REMOTE_GB": spill, "IDLE_PCT": idle}
    row.update(extra)
    return row


def _sized(*rows):
    return size_recommendations(pd.DataFrame(list(rows)), 3.68, 7).set_index("WAREHOUSE_NAME")


def test_queue_only_is_scale_out_spill_is_size_up_both_is_size_up_first():
    out = _sized(_wh("Q", queued_sec=_Q45), _wh("S", spill=_SPILL), _wh("B", queued_sec=_Q45, spill=_SPILL))
    assert out.loc["Q", "RECOMMENDATION"] == RECOMMEND_SCALE_OUT
    assert out.loc["S", "RECOMMENDATION"] == RECOMMEND_SIZE_UP
    assert out.loc["B", "RECOMMENDATION"] == RECOMMEND_SIZE_UP
    assert "if queueing persists after the resize, add a cluster" in out.loc["B", "RATIONALE"]
    assert "another cluster does not help a single spilling query" in out.loc["S", "RATIONALE"]


def test_the_legacy_merged_verdict_is_never_emitted_but_still_counts():
    out = size_recommendations(pd.DataFrame([_wh("Q", queued_sec=_Q45), _wh("S", spill=_SPILL)]), 3.68, 7)
    assert RECOMMEND_UP not in set(out["RECOMMENDATION"])
    assert {RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP, RECOMMEND_UP, RECOMMEND_BELOW_CAP} == UP_VERDICTS
    legacy = pd.DataFrame({"RECOMMENDATION": [RECOMMEND_UP], "POTENTIAL_MONTHLY_SAVING_USD": [0.0]})
    assert sizing_summary(legacy)["up"] == 1          # an externally built frame still counts


def test_both_up_verdicts_are_actionable_first_and_book_no_saving():
    df = pd.DataFrame([_wh("Q", queued_sec=_Q45), _wh("S", spill=_SPILL), _wh("IDLE", idle=80.0),
                       _wh("DOWN", p95=3.0, idle=40.0)])
    out = size_recommendations(df, 3.68, 7)
    order = list(out["RECOMMENDATION"])
    assert set(order[:2]) == {RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP}
    assert order.index(RECOMMEND_SUSPEND) < order.index(RECOMMEND_DOWN)
    up = out[out["RECOMMENDATION"].isin(UP_VERDICTS)]
    assert up["ACTIONABLE"].all() and (up["POTENTIAL_MONTHLY_SAVING_USD"] == 0).all()


def test_summary_splits_and_keeps_the_up_total():
    out = size_recommendations(pd.DataFrame([_wh("Q", queued_sec=_Q45), _wh("Q2", queued_sec=_Q45),
                                             _wh("S", spill=_SPILL)]), 3.68, 7)
    s = sizing_summary(out)
    assert (s["scale_out"], s["size_up"], s["up"]) == (2, 1, 3)
    empty = sizing_summary(pd.DataFrame())
    assert empty["scale_out"] == empty["size_up"] == empty["up"] == 0


def test_episodic_pressure_is_still_withheld():
    out = _sized(_wh("Q", queued_sec=_Q45, active_days=1), _wh("S", spill=_SPILL, active_days=1))
    assert set(out["RECOMMENDATION"]) == {RECOMMEND_OBSERVE}


def test_scale_out_rationale_reads_the_cluster_config():
    out = _sized(
        _wh("UNKNOWN", queued_sec=_Q45),
        _wh("SINGLE", queued_sec=_Q45, MAX_CLUSTER_COUNT=1.0, SCALING_POLICY="STANDARD"),
        _wh("MULTI", queued_sec=_Q45, MAX_CLUSTER_COUNT=3.0, SCALING_POLICY="STANDARD", **_REACHED),
        _wh("MULTI_UNCHECKED", queued_sec=_Q45, MAX_CLUSTER_COUNT=3.0, SCALING_POLICY="STANDARD"),
        _wh("ECON", queued_sec=_Q45, MAX_CLUSTER_COUNT=3.0, SCALING_POLICY="ECONOMY"),
        _wh("CAPPED", queued_sec=_Q45, MAX_CLUSTER_COUNT=float(CLUSTER_RANGE_CAP)),
    )
    assert "Enterprise edition" in out.loc["UNKNOWN", "RATIONALE"]
    assert "unknown" in out.loc["UNKNOWN", "RATIONALE"]
    assert "MAX_CLUSTER_COUNT = 1" in out.loc["SINGLE", "RATIONALE"]
    assert "raise MAX_CLUSTER_COUNT to 4" in out.loc["MULTI", "RATIONALE"]
    assert "reached cluster 3 of 3 in 5 hours of the last 35 days" in out.loc["MULTI", "RATIONALE"]
    assert "was not checked" in out.loc["MULTI_UNCHECKED", "RATIONALE"]
    assert "raise MAX_CLUSTER_COUNT to" not in out.loc["MULTI_UNCHECKED", "RATIONALE"]
    assert "SCALING_POLICY = STANDARD" in out.loc["ECON", "RATIONALE"]
    assert "split the workload" in out.loc["CAPPED", "RATIONALE"]
    assert (out["RECOMMENDATION"] == RECOMMEND_SCALE_OUT).all()


def test_rationale_humanizes_durations_and_long_p95_is_context_not_routing():
    out = _sized(_wh("Q", queued_sec=_Q45, p95=240.0))
    row = out.loc["Q"]
    assert row["RECOMMENDATION"] == RECOMMEND_SCALE_OUT      # a long p95 never re-routes
    assert "45m/day overload queueing" in row["RATIONALE"]
    assert "Peak-day p95 is 4m" in row["RATIONALE"] and "240" not in row["RATIONALE"]
    assert LONG_P95_SEC == HEALTH_LONG_P95_SEC               # one "long runtime" bar app-wide


def test_size_up_names_the_next_size_or_the_ladder_top():
    out = size_recommendations(with_warehouse_settings(
        pd.DataFrame([_wh("W_MED", spill=_SPILL), _wh("W_TOP", spill=_SPILL)]),
        pd.DataFrame([{"name": "W_MED", "size": "Medium"}, {"name": "W_TOP", "size": "6X-Large"}])),
        3.68, 7).set_index("WAREHOUSE_NAME")
    assert "Next size: LARGE" in out.loc["W_MED", "RATIONALE"]
    assert "largest size" in out.loc["W_TOP", "RATIONALE"]


def test_scale_out_plan_prefill():
    unknown = scale_out_plan(pd.Series({"WAREHOUSE_NAME": "W"}))
    assert unknown["known"] is False and "Enterprise edition" in unknown["note"]
    single = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 1.0}))
    assert (single["min"], single["max"], single["policy_to_standard"]) == (1, 2, False)
    econ = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 3.0,
                                     "SCALING_POLICY": "ECONOMY"}))
    assert econ["policy_to_standard"] is True and econ["max"] == 3
    capped = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 2.0, "MAX_CLUSTER_COUNT": 10.0}))
    assert capped["at_cap"] is True and capped["min"] == 2
    # MIN is never raised: a MIN > 1 bills clusters around the clock
    kept = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 2.0, "MAX_CLUSTER_COUNT": 4.0, **_REACHED}))
    assert (kept["min"], kept["max"], kept["prefill"]) == (2, 5, True)
    # v4.604: the same warehouse, cap NOT checked -> no prefill, the range stays as it is
    unchecked = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 2.0, "MAX_CLUSTER_COUNT": 4.0}))
    assert (unchecked["min"], unchecked["max"], unchecked["prefill"]) == (2, 4, False)
    seen = scale_out_plan(pd.Series({"MAX_CLUSTER_COUNT": 1.0}), multi_cluster_seen=True)
    assert "already runs multi-cluster" in seen["note"]


def test_prefill_cap_matches_the_generator_clamp():
    assert CLUSTER_RANGE_CAP == 10
    assert "MAX_CLUSTER_COUNT = 10;" in remediation.cluster_range_fix("W", 1, 99)
    plan = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 9.0, **_REACHED}))
    assert plan["max"] == 10 and not plan["at_cap"] and plan["prefill"]


def test_multi_cluster_evident():
    assert multi_cluster_evident(pd.DataFrame([{"name": "A", "max_cluster_count": 1},
                                               {"name": "B", "max_cluster_count": "3"}])) is True
    assert multi_cluster_evident(pd.DataFrame([{"name": "A", "max_cluster_count": 1}])) is False
    assert multi_cluster_evident(pd.DataFrame([{"name": "A"}])) is False
    assert multi_cluster_evident(pd.DataFrame()) is False and multi_cluster_evident(None) is False


def test_pages_render_the_split():
    ops = page_source("operations")
    body = ops.split("def _wh_sizing_efficiency", 1)[1].split("\ndef ", 1)[0]
    assert '"label": "Size up / add cluster"' not in body
    assert "_sum['scale_out']" in body and "_sum['size_up']" in body
    opt = read("app/ui/pages/cost_parts/optimize.py")
    assert "size-up / add-cluster" not in opt
    assert "summary['scale_out']" in opt and "summary['size_up']" in opt
    assert "scale_out_plan(" in opt and "remediation.cluster_range_fix(" in opt
    assert "remediation.scaling_policy_fix(" in opt


def test_scale_out_caption_names_the_lever_that_builds_the_shown_sql():
    """Review C19: the SCALING_POLICY = 'STANDARD' prefill routes to Emergency ▸ Scaling policy (the
    Cluster range lever only builds MIN/MAX_CLUSTER_COUNT); no lever is named when no SQL is shown."""
    from app.ui.pages.cost_parts.optimize import _scale_out_caption

    econ = scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 3.0,
                                     "SCALING_POLICY": "ECONOMY"}))
    cap = _scale_out_caption(econ)
    assert cap.startswith(econ["note"]) and "Operations ▸ Emergency ▸ Scaling policy (audited)" in cap
    assert "Cluster range" not in cap and "wider cluster range" not in cap
    single = _scale_out_caption(scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 1.0, "MAX_CLUSTER_COUNT": 1.0})))
    assert "Operations ▸ Emergency ▸ Cluster range (audited)" in single and "Scaling policy" not in single
    for plan in (scale_out_plan(pd.Series({"MIN_CLUSTER_COUNT": 2.0, "MAX_CLUSTER_COUNT": 10.0})),
                 scale_out_plan(pd.Series({"WAREHOUSE_NAME": "W"}))):
        text = _scale_out_caption(plan)
        assert "run it from" not in text and "No scale-out statement is generated here." in text
    for text in (cap, single):
        assert "size-up alternative" in text
    # the named levers exist on Operations ▸ Emergency and build exactly the statements the pane shows
    ops = page_source("operations")
    assert '"Cluster range", "Scaling policy",' in ops
    assert 'elif action == "Scaling policy" and wh:' in ops and 'st.radio("Policy", ["ECONOMY", "STANDARD"]' in ops
    assert "stmt = remediation.scaling_policy_fix(wh, pol)" in ops
    assert "stmt = remediation.cluster_range_fix(wh, int(lo), int(hi))" in ops
    # the pane routes through the helper; the old unconditional Cluster-range sentence is gone
    opt = read("app/ui/pages/cost_parts/optimize.py")
    assert "st.caption(_scale_out_caption(_so))" in opt
    assert 'st.caption(_so["note"] + " A wider cluster range' not in opt
