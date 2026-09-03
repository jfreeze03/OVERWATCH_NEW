"""Regression locks for bug-hunt round 21 — fresh-angle findings (empty-vs-zero, cross-filter, sort-label).

ETL-EMPTY   etl_tag_coverage is a bare aggregate (no GROUP BY) → always one row, all-NULL when no
            compute is attributed in scope/window; safe_float rendered that NaN as a measured "0%"
            / "$0.00" governance failure. The coverage KPIs now gate on a positive TOTAL_CREDITS
            and otherwise show an honest no-data note like the sibling per-pipeline board.
PATTERN-SCOPE The "Repeated patterns" rollup reads MART_PATTERN_COST_DAILY (QUERY_HASH+COMPANY grain
            only) so it drops the Database/Schema/Warehouse/User filters the section banner declares
            applied; now discloses that, mirroring the twin panel on the Optimization tab.
PROC-FILTER The "$/call leaderboard" (procedure_costs_usd) dropped the Warehouse/User filters its
            sibling measured_query_costs applies; the builder now honors them too.
PROC-SORT   The proc runtime-regression table was labeled "p95 growth desc" but is ordered severity-
            then-score (a "faster but failing" proc can top it); relabeled "worst first".
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- PROC-FILTER: procedure_costs_usd narrows by warehouse/user like its sibling (behavioral) --
def test_procedure_costs_honor_warehouse_and_user_filters():
    filtered = insights_sql.procedure_costs_usd(
        30, "ALFA", warehouse_contains="ETLWH", user_contains="svcacct")
    assert "ETLWH" in filtered                       # warehouse-contains literal injected
    assert "svcacct" in filtered                     # user-contains literal injected
    assert "USER_NAME" in filtered                   # USER_NAME appears ONLY via the new filter
    plain = insights_sql.procedure_costs_usd(30, "ALFA")
    assert "ETLWH" not in plain and "svcacct" not in plain
    assert "USER_NAME" not in plain                  # no user predicate when unfiltered
    # db/schema filters still work (unchanged)
    dbf = insights_sql.procedure_costs_usd(30, "ALFA", "ALFA_EDW_PRD", "OVERWATCH")
    assert "ALFA_EDW_PRD" in dbf and "OVERWATCH" in dbf


def test_unit_costs_threads_warehouse_user_into_the_proc_leaderboard():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    # both the batch job and the serial fallback pass the two filters
    assert src.count('warehouse_contains=f["warehouse_contains"], user_contains=f["user_contains"]') >= 2


# --- ETL-EMPTY: the coverage KPI shows a no-data note instead of a fabricated 0% -------------
def test_etl_tag_coverage_kpi_gates_on_measured_compute():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert '_cov_total = safe_float(c0.get("TOTAL_CREDITS"), default=float("nan"))' in src
    assert "if c0 is not None and _cov_total > 0:" in src
    assert "No measured ETL compute in this window/scope" in src
    # the old unconditional guard that rendered the NaN row as 0% is gone
    assert "if cov.ok and not cov.empty:\n            c0 = cov.df.iloc[0]\n            kpi_row([" not in src


# --- PATTERN-SCOPE: the fingerprint rollup discloses it drops the object/wh/user filters ------
def test_repeated_patterns_panel_discloses_dropped_filters():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert "_pc_dropped = [n for n, v in" in src
    assert "this parameterized-hash rollup has no object/warehouse/user" in src


# --- PROC-SORT: the proc-regression table's ordinal label matches its true order --------------
def test_proc_regression_sort_label_is_honest():
    src = _src("app/ui/pages/operations.py")
    assert 'sort_label="worst first"' in src
    assert 'sort_label="p95 growth desc"' not in src
