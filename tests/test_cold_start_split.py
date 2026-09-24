"""Next-Fifty #17 (v4.588.0): a warehouse resuming from suspend (PROVISIONING wait) is a cold start, not
concurrency starvation — sizing up buys nothing, so it must never be labelled "Concurrency starvation"
or told to size up. The QOIE fingerprint builder, the per-query drill and the live warehouse_pressure
fallback now split overload from provisioning; advise() names the dominant component while keeping the
combined wait's points (QOP / SQL_QOP / OOS and the ranking are byte-stable)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import insights_sql, ops_sql
from app.logic import query_opt
from app.logic.query_advisor import Finding, advise
from app.logic.query_opt import score_opportunities

_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_QUEUED = ("Spent 7s queued (of 10.0s total) — either concurrency (add a cluster or size up for "
                  "parallelism) or warehouse resume overhead (lengthen AUTO_SUSPEND / keep it warm).")


def _codes(findings):
    return [f.code for f in findings]


def _fp_row(**kw):
    base = {"FINGERPRINT": "fp", "SAMPLE_TEXT": "select ...", "QUERY_TYPE": "SELECT",
            "WAREHOUSE_NAME": "WH", "WAREHOUSE_SIZE": "MEDIUM", "RUNS": 100,
            "TOTAL_EXEC_SEC": 1000.0, "ELAPSED_SEC": 10.0, "COMPILE_SEC": 0.5,
            "EXECUTION_SEC": 9.0, "QUEUED_SEC": 0.0, "GB_SCANNED": 5.0, "CACHE_PCT": 10.0,
            "LOCAL_SPILL_GB": 0.0, "REMOTE_SPILL_GB": 0.0, "ROWS_PRODUCED": 100.0,
            "PARTITIONS_SCANNED": 10.0, "PARTITIONS_TOTAL": 1000.0}
    base.update(kw)
    return base


def test_builders_split_overload_from_provisioning():
    fp = ops_sql.query_opportunity_fingerprints(30)
    for alias in ("AS QUEUED_OVERLOAD_SEC", "AS QUEUED_PROVISIONING_SEC", "AS QUEUED_SEC", "AS QUEUED_RUN_PCT"):
        assert alias in fp                      # split added; combined gate + score inputs kept
    live = ops_sql.warehouse_pressure(7, "ALFA")
    detail = insights_sql.query_detail("0123456789abcdef")
    for sql in (live, detail):
        assert "AS QUEUED_OVERLOAD_SEC" in sql and "AS QUEUED_PROVISIONING_SEC" in sql
    # the live fallback keeps the combined QUEUED_SEC (mart/live column contract)
    assert ("SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000.0 "
            "AS QUEUED_SEC") in live


def test_cold_start_when_provisioning_dominates():
    split, _ = advise({"ELAPSED_SEC": 10, "QUEUED_SEC": 7,
                       "QUEUED_OVERLOAD_SEC": 1, "QUEUED_PROVISIONING_SEC": 6})
    legacy, _ = advise({"ELAPSED_SEC": 10, "QUEUED_SEC": 7})
    assert "cold_start" in _codes(split) and "queued" not in _codes(split)
    cold = next(f for f in split if f.code == "cold_start")
    assert "won't help" in cold.detail and "size up for parallelism" not in cold.detail
    assert cold.points == next(f for f in legacy if f.code == "queued").points   # QOP-stable


def test_overload_dominant_stays_queued_with_the_cluster_fix():
    findings, _ = advise({"ELAPSED_SEC": 10, "QUEUED_SEC": 7,
                          "QUEUED_OVERLOAD_SEC": 6, "QUEUED_PROVISIONING_SEC": 1})
    q = next(f for f in findings if f.code == "queued")
    assert "MAX_CLUSTER_COUNT" in q.detail and "cold_start" not in _codes(findings)


def test_split_unknown_keeps_the_legacy_wording_byte_for_byte():
    findings, _ = advise({"ELAPSED_SEC": 10, "QUEUED_SEC": 7})
    assert next(f for f in findings if f.code == "queued").detail == _LEGACY_QUEUED


def test_cold_start_pathology_when_the_sql_is_clean():
    row = _fp_row(QUEUED_SEC=25.0, QUEUED_OVERLOAD_SEC=2.0, QUEUED_PROVISIONING_SEC=23.0, ELAPSED_SEC=28.0)
    scored, _ = score_opportunities(pd.DataFrame([row]))
    assert scored.iloc[0]["PATHOLOGY"] == "Cold-start wait"
    assert scored.iloc[0]["SQL_QOP"] == 0                       # a capacity code, never "bad SQL"


def test_dirty_sql_leads_with_its_sql_driver_not_the_cold_start():
    lead = query_opt._lead_finding(
        [Finding("cold_start", "warn", "Cold-start wait", "resume", 15),
         Finding("local_spill", "warn", "Spill", "spill", 12)], 20)
    assert lead.code == "local_spill"


def test_qoie_kpi_counts_cold_start_separately():
    ops = (_ROOT / "app/ui/pages/operations.py").read_text(encoding="utf-8")
    assert '_scored["PATHOLOGY"] == "Cold-start wait"' in ops
    assert '"label": "Cold-start wait"' in ops
    assert "Size or split the warehouse" not in ops             # the size-up nudge is gone


# --- adversarial review: the fingerprint AVGs are time-weighted, so decide from per-run dominance -------
def _bimodal(prov_runs: float, over_runs: float) -> dict:
    # 100 runs: 90 cold starts (1.5s resume, 1s exec), 9 warm, 1 overload storm (300s queue)
    return _fp_row(ELAPSED_SEC=5.35, EXECUTION_SEC=1.0, COMPILE_SEC=0.0, QUEUED_SEC=4.35,
                   QUEUED_OVERLOAD_SEC=3.0, QUEUED_PROVISIONING_SEC=1.35, QUEUED_RUN_PCT=0.91,
                   PROVISIONING_QUEUED_RUN_PCT=prov_runs, OVERLOAD_QUEUED_RUN_PCT=over_runs)


def test_one_overload_storm_does_not_relabel_a_cold_start_fingerprint():
    f, _ = advise(_bimodal(0.90, 0.01))
    assert "cold_start" in _codes(f) and "queued" not in _codes(f)
    scored, _ = score_opportunities(pd.DataFrame([_bimodal(0.90, 0.01)]))
    assert scored.iloc[0]["PATHOLOGY"] == "Cold-start wait"


def test_mixed_fingerprint_keeps_the_hedged_wording():
    f = [x for x in advise(_bimodal(0.46, 0.45))[0] if x.code in ("queued", "cold_start")]
    assert _codes(f) == ["queued"] and "either concurrency" in f[0].detail


def test_overload_dominant_fingerprint_names_overload():
    f = [x for x in advise(_bimodal(0.05, 0.86))[0] if x.code in ("queued", "cold_start")]
    assert _codes(f) == ["queued"] and "OVERLOAD" in f[0].detail


def test_builder_emits_the_per_run_dominance_shares():
    fp = ops_sql.query_opportunity_fingerprints(30)
    assert "AS PROVISIONING_QUEUED_RUN_PCT" in fp and "AS OVERLOAD_QUEUED_RUN_PCT" in fp
