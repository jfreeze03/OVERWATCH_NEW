"""Round-1 bug-hunt regression locks (new sections: cost-governance, QOIE, ledger).

Each test pins one verified-real defect the R1 adversarial hunt found so it can't
silently reopen. Pure-function fixes only; the UI-render fixes (KPI counts, NaN blanking,
operator-board single render) are exercised via their logic/SQL seams here.
"""

from __future__ import annotations

import pandas as pd

from app.data import ops_sql
from app.logic import query_opt
from app.logic.monitors import unmonitored_warehouses
from app.logic.query_advisor import Finding, advise


# --- #5: advise remote-spill floor (fleet fingerprint feeds AVG spill) ------
def test_advise_remote_spill_floor_gates_the_fingerprint_grain():
    row = {"ELAPSED_SEC": 10.0, "REMOTE_SPILL_GB": 0.04}   # a rare 1-in-N spill, averaged
    # per-QUERY drill (default floor 0.0): any real spill = memory exhaustion -> fires
    fired, _ = advise(row)
    assert any(f.code == "remote_spill" for f in fired)
    # per-FINGERPRINT (AVG) path floors it: a 0.04 GB average is not memory exhaustion
    floored, _ = advise(row, remote_spill_floor_gb=0.5)
    assert not any(f.code == "remote_spill" for f in floored)


def test_score_opportunities_does_not_flag_tiny_avg_spill():
    assert query_opt._FINGERPRINT_REMOTE_SPILL_FLOOR_GB == 0.5
    df = pd.DataFrame([{
        "FINGERPRINT": "h1", "SAMPLE_TEXT": "select 1", "QUERY_TYPE": "SELECT",
        "WAREHOUSE_NAME": "WH_ALFA_QUERY", "RUNS": 25, "TOTAL_EXEC_SEC": 100.0,
        "ELAPSED_SEC": 4.0, "REMOTE_SPILL_GB": 0.04, "LAST_SEEN": None,
    }])
    scored, _ = query_opt.score_opportunities(df)
    assert scored.iloc[0]["PATHOLOGY"] != "Spill (memory)"
    assert int(scored.iloc[0]["QOP"]) == 0        # nothing else fires -> clean


# --- #6: lead finding = the fix to take (not the max-points queue row) -------
def test_lead_finding_prefers_sql_driver_on_queued_top_but_dirty():
    q = Finding("queued", "warn", "Queue", "add a cluster / size up", 15)
    s = Finding("local_spill", "warn", "Spill", "trim the working set", 12)
    # queued is findings[0] (higher points) but the SQL underneath is dirty (sql_qop>15)
    assert query_opt._lead_finding([q, s], sql_qop=20).code == "local_spill"
    # clean SQL under a queue top -> genuinely a capacity problem, lead with the queue
    assert query_opt._lead_finding([q, s], sql_qop=10).code == "queued"
    # a non-queue top just leads with itself
    assert query_opt._lead_finding([s, q], sql_qop=5).code == "local_spill"
    assert query_opt._lead_finding([], sql_qop=0) is None


# --- #2: unmonitored_warehouses scopes to the tab's company ------------------
def test_unmonitored_warehouses_company_scope():
    wh = pd.DataFrame({"name": ["WH_ALFA_ADMIN", "WH_OTHER_TENANT"],
                       "size": ["X-Small", "Small"],
                       "resource_monitor": ["null", "null"]})
    mon = pd.DataFrame()
    # ALL: both uncapped warehouses listed
    all_out = unmonitored_warehouses(wh, mon, {})
    assert set(all_out["WAREHOUSE_NAME"]) == {"WH_ALFA_ADMIN", "WH_OTHER_TENANT"}
    # ALFA: the non-ALFA warehouse is dropped (no cross-tenant leak)
    alfa_out = unmonitored_warehouses(wh, mon, {}, company="ALFA")
    assert set(alfa_out["WAREHOUSE_NAME"]) == {"WH_ALFA_ADMIN"}


def test_unmonitored_warehouses_trexis_scope_casing():
    # guards the mixed-case bug: classify_warehouse returns 'Trexis', so the compare must
    # NOT upper-case the company (else every Trexis wh is wrongly dropped under Trexis scope).
    from app.companies import TREXIS_WAREHOUSES
    if not TREXIS_WAREHOUSES:
        return
    trx = sorted(TREXIS_WAREHOUSES)[0]
    wh = pd.DataFrame({"name": ["WH_ALFA_ADMIN", trx], "size": ["X", "X"],
                       "resource_monitor": ["null", "null"]})
    out = unmonitored_warehouses(wh, pd.DataFrame(), {}, company="Trexis")
    assert set(out["WAREHOUSE_NAME"]) == {trx}


# --- #7 / #8: pruning-quality ceiling + poor_pruning_queries honors bounds ----
def test_table_pruning_candidates_has_quality_ceiling():
    sql = ops_sql.table_pruning_candidates(30)
    # only genuinely poorly-pruned tables (efficiency < 0.5) count as clustering candidates
    assert ("SUM(PARTITIONS_PRUNED)\n       / GREATEST(SUM(PARTITIONS_SCANNED) "
            "+ SUM(PARTITIONS_PRUNED), 1) < 0.5") in sql


def test_poor_pruning_queries_threads_bounds():
    from datetime import date
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = ops_sql.poor_pruning_queries(31, bounds=b)
    trailing = ops_sql.poor_pruning_queries(31)
    assert bounded != trailing                       # bounds actually changes the window
    assert "2026-08-01" in bounded and "2026-09-01" in bounded
