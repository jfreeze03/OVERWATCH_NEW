"""Second logic-layer bug hunt (v4.345.0) — locks for the three confirmed fixes.

All behavioral (the logic layer is pure): a release-compare verdict that silenced
from-zero regressions, an RCA ranking that let a LOW-capped untimed candidate headline
above a HIGH cause, and a ROI projection that collapsed distinct blank-key actions.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.logic.decision import scenario_projection
from app.logic.insights import compare_release_periods
from app.logic.rca import rank_root_causes, rca_summary


def _verdicts(res):
    rows = res if isinstance(res, list) else res.to_dict("records")
    return {r["Metric"]: r["Verdict"] for r in rows}


def test_release_compare_judges_from_zero_regressions():
    # a lower-is-better metric introduced from a clean (0) baseline is a real
    # regression, not "n/a" — else Operations shows a green "no regression" banner on
    # a clean->broken deploy.
    df = pd.DataFrame({"PERIOD": ["BEFORE", "AFTER"], "QUERY_COUNT": [1000, 1000],
                       "FAILED_COUNT": [0, 80], "P95_ELAPSED_SEC": [5, 5],
                       "QUEUED_SEC": [0, 300], "SPILL_REMOTE_GB": [0, 10]})
    v = _verdicts(compare_release_periods(df))
    assert v["Failure %"] == "Worse"
    assert v["Queued (s/query)"] == "Worse" and v["Remote spill (GB/query)"] == "Worse"


def test_release_compare_from_zero_no_change_is_flat_and_improvement_to_zero_is_better():
    # 0 -> 0 is Flat (no change), not "n/a"; and a lower-better metric going N -> 0 is
    # still Better (the pre-existing clean-baseline-improvement path is unchanged).
    flat = pd.DataFrame({"PERIOD": ["BEFORE", "AFTER"], "QUERY_COUNT": [10, 10],
                         "FAILED_COUNT": [0, 0], "P95_ELAPSED_SEC": [1, 1],
                         "QUEUED_SEC": [0, 0], "SPILL_REMOTE_GB": [0, 0]})
    assert _verdicts(compare_release_periods(flat))["Remote spill (GB/query)"] == "Flat"
    better = pd.DataFrame({"PERIOD": ["BEFORE", "AFTER"], "QUERY_COUNT": [10, 10],
                          "FAILED_COUNT": [5, 0], "P95_ELAPSED_SEC": [1, 1],
                          "QUEUED_SEC": [300, 0], "SPILL_REMOTE_GB": [10, 0]})
    assert _verdicts(compare_release_periods(better))["Queued (s/query)"] == "Better"


def test_rca_high_timed_cause_outranks_untimed_low_even_at_higher_raw_score():
    onset = datetime(2026, 8, 29, 12, 0)
    timed_high = {"kind": "warehouse_change", "title": "resize",
                  "when": datetime(2026, 8, 29, 10, 0), "entity": "ETL",
                  "magnitude": 0.35, "evidence": {}}
    untimed_low = {"kind": "grant", "title": "grant", "when": None, "entity": "ETL",
                   "magnitude": 1.0, "evidence": {}}   # higher raw score, but LOW-capped
    ranked = rank_root_causes([timed_high, untimed_low], onset, entity_name="ETL")
    assert ranked[0]["band"] == "HIGH" and ranked[0]["title"] == "resize"
    assert ranked[1]["band"] == "LOW"          # the untimed candidate is demoted
    assert rca_summary(ranked)["has_lead"]     # and the HIGH cause headlines


def test_scenario_projection_does_not_collapse_blank_key_actions():
    # distinct actions that share only an entity TYPE (blank key) must count as
    # separate candidates, not collapse to the single highest-estimate row.
    df = pd.DataFrame([
        {"ACTION_ID": "A1", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 1000,
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": ""},
        {"ACTION_ID": "A2", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 500,
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": ""},
        {"ACTION_ID": "A3", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 300,
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "   "},   # whitespace-only
    ])
    p = scenario_projection(df, adoption_pct=100, realization_pct=100, confidence_floor=0.0)
    assert p["candidates"] == 3.0 and p["gross_estimate"] == 1800.0


def test_scenario_projection_still_dedupes_real_shared_entity_keys():
    # the real dedup still holds: two actions on the SAME (type,key) keep the largest.
    df = pd.DataFrame([
        {"ACTION_ID": "A1", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 1000,
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "WH_A"},
        {"ACTION_ID": "A2", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 500,
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "WH_A"},
    ])
    p = scenario_projection(df, adoption_pct=100, realization_pct=100, confidence_floor=0.0)
    assert p["candidates"] == 1.0 and p["gross_estimate"] == 1000.0
