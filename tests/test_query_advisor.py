"""Per-query optimization advisor — pure logic (app/logic/query_advisor.py)."""

from __future__ import annotations

from app.logic.query_advisor import _CAP, advise


def _row(**over: float) -> dict:
    base = {
        "WAREHOUSE_SIZE": "MEDIUM", "ELAPSED_SEC": 30.0, "REMOTE_SPILL_GB": 0.0,
        "LOCAL_SPILL_GB": 0.0, "GB_SCANNED": 1.0, "CACHE_PCT": 90.0,
        "COMPILE_SEC": 0.2, "QUEUED_SEC": 0.0, "PARTITIONS_SCANNED": 5.0,
        "PARTITIONS_TOTAL": 1000.0, "ROWS_PRODUCED": 100.0,
    }
    base.update(over)
    return base


def _codes(row: dict) -> set[str]:
    return {f.code for f in advise(row)[0]}


# ================================================= clean / empty =============

def test_clean_query_has_no_findings():
    findings, score = advise(_row())
    assert findings == [] and score == 0


def test_missing_columns_never_crash():
    for r in ({}, {"ELAPSED_SEC": 0.0}, {"GB_SCANNED": None}, {"REMOTE_SPILL_GB": "x"}):
        _findings, score = advise(r)
        assert isinstance(score, int) and 0 <= score <= 100


# ================================================= individual rules ==========

def test_remote_spill_fires_and_leads():
    findings, score = advise(_row(REMOTE_SPILL_GB=8.0))
    assert findings[0].code == "remote_spill" and findings[0].severity == "bad"
    assert score > 0 and "ran out of memory" in findings[0].detail


def test_local_spill_only_when_no_remote():
    assert "local_spill" in _codes(_row(LOCAL_SPILL_GB=3.0))
    # remote spill present -> local is subsumed, not double-reported
    codes = _codes(_row(REMOTE_SPILL_GB=2.0, LOCAL_SPILL_GB=3.0))
    assert "remote_spill" in codes and "local_spill" not in codes


def test_poor_pruning_needs_both_partition_floor_and_ratio():
    assert "poor_pruning" in _codes(_row(PARTITIONS_TOTAL=1000.0, PARTITIONS_SCANNED=950.0))
    # high ratio but too few partitions -> not meaningful
    assert "poor_pruning" not in _codes(_row(PARTITIONS_TOTAL=50.0, PARTITIONS_SCANNED=48.0))
    # enough partitions but good pruning -> no finding
    assert "poor_pruning" not in _codes(_row(PARTITIONS_TOTAL=1000.0, PARTITIONS_SCANNED=100.0))


def test_large_scan_fires_on_size_alone_matching_triage():
    # review: the triage ELSE branch labels any >50GB scan "Large cold scan" with NO
    # cache gate, so the advisor must too — else the two surfaces contradict.
    assert "cold_scan" in _codes(_row(GB_SCANNED=200.0, CACHE_PCT=5.0))
    assert "cold_scan" in _codes(_row(GB_SCANNED=200.0, CACHE_PCT=90.0))   # warm: still a large scan
    assert "cold_scan" not in _codes(_row(GB_SCANNED=10.0, CACHE_PCT=5.0))  # small: no


def test_compile_bound_needs_fraction_and_min_elapsed():
    assert "compile_bound" in _codes(_row(ELAPSED_SEC=100.0, COMPILE_SEC=70.0))
    # trivially short query: high compile fraction but immaterial
    assert "compile_bound" not in _codes(_row(ELAPSED_SEC=0.5, COMPILE_SEC=0.4))


def test_queued_needs_fraction():
    assert "queued" in _codes(_row(ELAPSED_SEC=100.0, QUEUED_SEC=70.0))
    assert "queued" not in _codes(_row(ELAPSED_SEC=100.0, QUEUED_SEC=20.0))


def test_zero_result_expensive():
    assert "zero_result" in _codes(_row(GB_SCANNED=80.0, CACHE_PCT=10.0, ROWS_PRODUCED=0.0))
    assert "zero_result" not in _codes(_row(GB_SCANNED=80.0, CACHE_PCT=10.0, ROWS_PRODUCED=5.0))


# ================================================= score composition ========

def test_spill_outranks_pruning_outranks_cold_scan():
    spill = advise(_row(REMOTE_SPILL_GB=3.0))[1]
    prune = advise(_row(PARTITIONS_TOTAL=1000.0, PARTITIONS_SCANNED=950.0))[1]
    cold = advise(_row(GB_SCANNED=200.0, CACHE_PCT=5.0))[1]
    assert spill > prune > cold > 0


def test_multi_signal_scores_higher_and_caps_at_100():
    one = advise(_row(REMOTE_SPILL_GB=3.0))[1]
    score = advise(_row(REMOTE_SPILL_GB=8.0, GB_SCANNED=300.0, CACHE_PCT=2.0,
                        PARTITIONS_TOTAL=1000.0, PARTITIONS_SCANNED=990.0,
                        ELAPSED_SEC=100.0, COMPILE_SEC=80.0))[1]
    assert score > one
    assert score <= 100


def test_no_single_driver_saturates_the_score():
    # an enormous remote spill is still capped at its per-driver ceiling
    findings, _ = advise(_row(REMOTE_SPILL_GB=10_000.0))
    spill = next(f for f in findings if f.code == "remote_spill")
    assert spill.points == _CAP["remote_spill"]


def test_query_optimization_prompt_grounds_and_bounds():
    from app.logic.ai_prompts import MAX_PROMPT_CHARS, query_optimization_prompt
    row = _row(REMOTE_SPILL_GB=8.0, QUERY_TEXT="SELECT * FROM BIG_TABLE")
    findings, _ = advise(row)
    p = query_optimization_prompt(row, findings)
    assert len(p) <= MAX_PROMPT_CHARS
    assert "SELECT * FROM BIG_TABLE" in p        # the actual query text is embedded
    assert "Remote spill" in p                    # the deterministic finding is embedded
    assert "not invent" in p.lower()              # the anti-hallucination instruction
    # an enormous query text is truncated to the cap, never overflows
    big = _row(REMOTE_SPILL_GB=8.0, QUERY_TEXT="X" * 20000)
    assert len(query_optimization_prompt(big, advise(big)[0])) <= MAX_PROMPT_CHARS


def test_findings_are_sorted_by_impact():
    findings, _ = advise(_row(REMOTE_SPILL_GB=8.0, ELAPSED_SEC=100.0, COMPILE_SEC=80.0,
                              GB_SCANNED=200.0, CACHE_PCT=2.0))
    pts = [f.points for f in findings]
    assert pts == sorted(pts, reverse=True)
