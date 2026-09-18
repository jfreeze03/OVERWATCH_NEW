"""Locks for the Query Optimization Intelligence engine (Slice 1).

Builder ops_sql.query_opportunity_fingerprints (fingerprint-grain QUERY_HISTORY) +
logic query_opt.score_opportunities (QOP via the reused advisor, SQL-vs-concurrency
split, pathology, OOS impact ranking, confidence). The load-bearing property: a
moderately-bad query run very often outranks a catastrophic one run once (OOS), which a
plain QOP/duration ranking gets wrong.
"""

from __future__ import annotations

import pandas as pd

from app.data import ops_sql
from app.logic.query_opt import score_opportunities


def test_builder_is_fingerprint_grain_with_the_advisor_columns():
    sql = ops_sql.query_opportunity_fingerprints(30)
    assert "QUERY_PARAMETERIZED_HASH AS FINGERPRINT" in sql
    assert "GROUP BY QUERY_PARAMETERIZED_HASH" in sql
    assert "COUNT(*) AS RUNS" in sql and "AS TOTAL_EXEC_SEC" in sql
    # the per-run columns the advisor reads must be present, by name
    for col in ("ELAPSED_SEC", "REMOTE_SPILL_GB", "LOCAL_SPILL_GB", "GB_SCANNED",
                "COMPILE_SEC", "QUEUED_SEC", "PARTITIONS_SCANNED", "PARTITIONS_TOTAL",
                "ROWS_PRODUCED", "WAREHOUSE_SIZE"):
        assert f"AS {col}" in sql or col in sql, col
    assert "ORDER BY TOTAL_EXEC_SEC DESC" in sql
    assert "EXECUTION_STATUS = 'SUCCESS'" in sql
    # Must share the QUERY_ID sibling's self-noise / CALL exclusions so the fingerprint
    # surface never ranks OVERWATCH's own queries, never double-counts CALL child compute in
    # the footprint, and never contradicts the triage table.
    sibling = ops_sql.query_optimization_triage(30)
    for clause in ("QUERY_TYPE <> 'CALL'", "NOT LIKE 'EXECUTE STREAMLIT%'",
                   "NOT LIKE '%OVERWATCH_APP%'", "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'"):
        assert clause in sql, f"fingerprint builder missing self-noise filter: {clause}"
        assert clause in sibling, f"triage sibling missing self-noise filter: {clause}"


def _row(**kw):
    base = {"FINGERPRINT": "fp", "SAMPLE_TEXT": "select ...", "QUERY_TYPE": "SELECT",
            "WAREHOUSE_NAME": "WH", "WAREHOUSE_SIZE": "MEDIUM", "RUNS": 100,
            "TOTAL_EXEC_SEC": 1000.0, "ELAPSED_SEC": 10.0, "COMPILE_SEC": 0.5,
            "EXECUTION_SEC": 9.0, "QUEUED_SEC": 0.0, "GB_SCANNED": 5.0, "CACHE_PCT": 10.0,
            "LOCAL_SPILL_GB": 0.0, "REMOTE_SPILL_GB": 0.0, "ROWS_PRODUCED": 100.0,
            "PARTITIONS_SCANNED": 10.0, "PARTITIONS_TOTAL": 1000.0}
    base.update(kw)
    return base


def _frame():
    return pd.DataFrame([
        # A: concurrency-starved — 25s queued of 28s, clean SQL (no spill, good pruning, small scan)
        _row(FINGERPRINT="A", ELAPSED_SEC=28.0, QUEUED_SEC=25.0, RUNS=200, TOTAL_EXEC_SEC=500.0),
        # B: compound — remote spill + poor pruning + large scan, run a fair bit
        _row(FINGERPRINT="B", REMOTE_SPILL_GB=10.0, GB_SCANNED=80.0, ELAPSED_SEC=83.0,
             PARTITIONS_SCANNED=950.0, PARTITIONS_TOTAL=1000.0, RUNS=50, TOTAL_EXEC_SEC=2000.0),
        # C: moderately bad (poor pruning), run HUGELY often -> biggest footprint
        _row(FINGERPRINT="C", PARTITIONS_SCANNED=880.0, PARTITIONS_TOTAL=1000.0,
             RUNS=20000, TOTAL_EXEC_SEC=20000.0),
        # D: catastrophic (big remote spill) but run ONCE -> tiny footprint
        _row(FINGERPRINT="D", REMOTE_SPILL_GB=20.0, ELAPSED_SEC=80.0, RUNS=1, TOTAL_EXEC_SEC=80.0),
    ])


def test_qop_pathology_and_sql_vs_concurrency_split():
    scored, breakdowns = score_opportunities(_frame())
    by = scored.set_index("FINGERPRINT")
    # A: queue dominates, SQL is clean -> concurrency starvation, SQL_QOP isolated to 0
    assert by.loc["A", "PATHOLOGY"] == "Concurrency starvation"
    assert by.loc["A", "SQL_QOP"] == 0 and by.loc["A", "QOP"] > 0
    # B: three independent SQL problems -> compound
    assert by.loc["B", "PATHOLOGY"] == "Compound inefficiency"
    assert by.loc["B", "QOP"] == 100          # capped sum of remote-spill + pruning + scan
    # D: single big remote spill -> spill pathology, SQL_QOP == QOP (no queue)
    assert by.loc["D", "PATHOLOGY"] == "Spill (memory)"
    assert by.loc["D", "SQL_QOP"] == by.loc["D", "QOP"]
    # the QOP breakdown is retained for the explainer
    assert breakdowns["B"] and all(len(t) == 3 for t in breakdowns["B"])


def test_oos_puts_moderate_but_frequent_above_catastrophic_but_rare():
    scored, _ = score_opportunities(_frame())
    by = scored.set_index("FINGERPRINT")
    # D is the WORST single query (QOP) but runs once; C is milder but runs 20k times
    assert by.loc["D", "QOP"] > by.loc["C", "QOP"]        # D is worse per-execution
    assert by.loc["C", "OOS"] > by.loc["D", "OOS"]        # ...but C is the bigger opportunity
    # ranking is by OOS, so C precedes D in the output order
    order = list(scored["FINGERPRINT"])
    assert order.index("C") < order.index("D")
    # Discriminator: OOS is inefficiency x footprint PERCENTILE, NOT a raw inefficiency x
    # footprint multiply. B is maxed-bad (QOP 100) on the 2nd-largest footprint; C is mild
    # (QOP ~22) on the LARGEST. Percentile-rank ranks B above C; a naive multiply would flip
    # them (C: 22x20000=440k > B: 100x2000=200k). This assert fails under the wrong formula.
    assert by.loc["B", "OOS"] > by.loc["C", "OOS"]


def test_queued_top_but_dirty_sql_is_not_mislabelled_concurrency_starvation():
    """A fingerprint whose top finding is queueing but which ALSO carries material SQL
    badness (sql_qop over the clean bar) must be named by its SQL, not written off as
    'Concurrency starvation' — the old _PATHOLOGY['queued'] fallback gave the exact opposite
    (and actively wrong) 'don't rewrite the query' advice."""
    # queued 15 (top) + local_spill 10 (2 GB) + zero_result 10 (25 GB scanned -> 0 rows):
    # each SQL finding is <= the queue points, but they SUM to sql_qop 20 (> the 15 clean bar).
    row = _row(FINGERPRINT="E", QUEUED_SEC=7.0, ELAPSED_SEC=13.0, LOCAL_SPILL_GB=2.0,
               GB_SCANNED=25.0, ROWS_PRODUCED=0.0, RUNS=50, TOTAL_EXEC_SEC=650.0)
    scored, _ = score_opportunities(pd.DataFrame([row]))
    r = scored.iloc[0]
    assert r["QOP"] == 35 and r["SQL_QOP"] == 20   # queue 15 + 2 SQL findings @10 each
    assert r["PATHOLOGY"] != "Concurrency starvation"
    assert r["PATHOLOGY"] == "Spill (memory)"      # named by the dominant SQL driver


def test_confidence_separate_from_severity_and_small_sample_penalised():
    scored, _ = score_opportunities(_frame())
    by = scored.set_index("FINGERPRINT")
    # D fires one finding on a single run -> lower confidence than multi-finding B
    assert by.loc["D", "CONFIDENCE"] < by.loc["B", "CONFIDENCE"]
    # confidence is not just severity: D has a high QOP but low confidence
    assert by.loc["D", "QOP"] >= 55 and by.loc["D", "CONFIDENCE"] <= 45


def test_empty_in_empty_out():
    scored, breakdowns = score_opportunities(pd.DataFrame())
    assert scored.empty and breakdowns == {}
    assert score_opportunities(None)[0].empty
