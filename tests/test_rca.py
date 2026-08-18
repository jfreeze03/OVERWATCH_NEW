"""Ranked root-cause synthesis — the intelligence behind 'incidents that investigate
themselves'. Deterministic: all timestamps are passed in, no wall-clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.logic.rca import (
    candidates_from_anomalies,
    candidates_from_changes,
    candidates_from_grants,
    candidates_from_tasks,
    rank_root_causes,
    rca_summary,
)

_ROOT = Path(__file__).resolve().parents[1]
_ONSET = datetime(2026, 8, 18, 14, 0, 0)   # incident onset


def _cand(kind="object_change", title="x", when=None, entity="WH_A", magnitude=0.5):
    return {"kind": kind, "title": title, "when": when, "entity": entity,
            "magnitude": magnitude, "magnitude_text": "", "changed_by": "", "evidence": {}}


# ---------------------------------------------------------------- proximity ----

def test_a_change_just_before_onset_outranks_a_day_old_one():
    close = _cand(title="close change", when=_ONSET - timedelta(minutes=10), magnitude=0.6)
    far = _cand(title="old change", when=_ONSET - timedelta(hours=30), magnitude=0.6)
    ranked = rank_root_causes([far, close], _ONSET, entity_name="WH_A")
    assert ranked[0]["title"] == "close change" and ranked[0]["score"] > ranked[1]["score"]


def test_a_change_after_onset_is_weak_evidence_of_cause():
    # a cause precedes its effect; a change 2h AFTER onset should score low on timing.
    after = _cand(title="after", when=_ONSET + timedelta(hours=2), magnitude=1.0, entity="WH_A")
    before = _cand(title="before", when=_ONSET - timedelta(minutes=20), magnitude=0.5, entity="WH_A")
    ranked = rank_root_causes([after, before], _ONSET, entity_name="WH_A")
    assert ranked[0]["title"] == "before"
    assert "AFTER onset" in next(h for h in ranked if h["title"] == "after")["lead_text"]


def test_beyond_the_lead_window_is_not_a_candidate_trigger():
    stale = _cand(when=_ONSET - timedelta(hours=60), magnitude=1.0, entity="WH_A")
    ranked = rank_root_causes([stale], _ONSET, entity_name="WH_A")
    # proximity 0 beyond 48h -> only magnitude+match contribute; lands LOW.
    assert ranked[0]["band"] == "LOW"


# ---------------------------------------------------------------- match + magnitude ----

def test_entity_match_and_magnitude_lift_the_score():
    onmatch = _cand(title="on entity", when=_ONSET - timedelta(minutes=15), entity="WH_A", magnitude=1.0)
    offmatch = _cand(title="other entity", when=_ONSET - timedelta(minutes=15), entity="WH_Z", magnitude=1.0)
    ranked = rank_root_causes([offmatch, onmatch], _ONSET, entity_name="WH_A")
    assert ranked[0]["title"] == "on entity"
    top = ranked[0]
    assert top["band"] == "HIGH" and "entity match 100%" in top["why"]


def test_families_give_a_partial_match_boost():
    r = rank_root_causes([_cand(when=_ONSET - timedelta(minutes=5), entity="WH_ETL", magnitude=0.8)],
                         _ONSET, entity_name="WH_OTHER", families={"WH_ETL"})
    assert "entity match 60%" in r[0]["why"]


# ---------------------------------------------------------------- adapters ----

def test_changes_adapter_flags_regressed_as_top_magnitude():
    df = pd.DataFrame([
        {"ENTITY": "WH_A", "CHANGE": "ALTER WAREHOUSE resize", "CHANGED_AT": _ONSET - timedelta(minutes=8),
         "VERDICT": "REGRESSED", "CHANGED_BY": "svc_deploy", "WAREHOUSE_NAME": "WH_A"},
        {"ENTITY": "DB.T", "CHANGE": "CREATE TABLE", "CHANGED_AT": _ONSET - timedelta(hours=1),
         "VERDICT": "BENIGN"},
    ])
    cands = candidates_from_changes(df)
    assert cands[0]["kind"] == "warehouse_change" and cands[0]["magnitude"] == 1.0
    assert cands[1]["kind"] == "object_change" and cands[1]["magnitude"] < 1.0


def test_tasks_adapter_scores_root_above_cascade():
    df = pd.DataFrame([
        {"TASK_NAME": "LOAD_A", "DATABASE_NAME": "PRD", "ROLE_IN_GRAPH": "Root cause",
         "ERROR_FAMILY": "permission", "QUERY_START_TIME": _ONSET - timedelta(minutes=5)},
        {"TASK_NAME": "LOAD_B", "DATABASE_NAME": "PRD", "ROLE_IN_GRAPH": "Cascade",
         "ERROR_FAMILY": "upstream", "QUERY_START_TIME": _ONSET - timedelta(minutes=3)},
    ])
    cands = candidates_from_tasks(df)
    root = next(c for c in cands if "Root" in c["title"])
    casc = next(c for c in cands if "Cascad" in c["title"])
    assert root["magnitude"] == 1.0 and casc["magnitude"] < root["magnitude"]


def test_anomaly_and_grant_adapters_normalize():
    an = candidates_from_anomalies([{"label": "WH_A", "value": 900.0, "z": 6.0, "day": "2026-08-18"}])
    assert an[0]["kind"] == "spend_anomaly" and 0 < an[0]["magnitude"] <= 1.0 and "spike" in an[0]["title"]
    gr = candidates_from_grants(pd.DataFrame([
        {"ACTION": "grant", "GRANTEE": "ANALYST", "PRIVILEGE": "SELECT", "GRANTED_AT": _ONSET}]))
    assert gr[0]["kind"] == "grant_change" and "SELECT" in gr[0]["title"]


# ---------------------------------------------------------------- end to end ----

def test_realistic_incident_ranks_the_deploy_first():
    # A regressed warehouse resize 12 min before onset should beat a small far-off anomaly.
    changes = candidates_from_changes(pd.DataFrame([
        {"ENTITY": "WH_ETL", "WAREHOUSE_NAME": "WH_ETL", "CHANGE": "resize XL->4XL",
         "CHANGED_AT": _ONSET - timedelta(minutes=12), "VERDICT": "REGRESSED", "CHANGED_BY": "tf_deploy"}]))
    anoms = candidates_from_anomalies([{"label": "WH_OTHER", "value": 120.0, "z": 3.6,
                                        "day": (_ONSET - timedelta(hours=20)).date().isoformat()}])
    ranked = rank_root_causes(changes + anoms, _ONSET, entity_name="WH_ETL", families={"WH_ETL"})
    top = ranked[0]
    assert top["kind"] == "warehouse_change" and top["band"] == "HIGH"
    s = rca_summary(ranked)
    assert s["has_lead"] and "Most likely cause" in s["headline"] and "resize" in s["headline"]


def test_no_signals_is_an_honest_no_lead():
    assert rank_root_causes([], _ONSET) == []
    s = rca_summary([])
    assert s["has_lead"] is False and "No clear trigger" in s["headline"]


def test_rca_is_wired_into_control_room():
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    assert "rank_root_causes(" in cr and "Auto-investigation" in cr
