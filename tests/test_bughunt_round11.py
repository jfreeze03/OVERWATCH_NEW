"""Regression locks for the round-11 bug hunt (v4.426.0)."""

from __future__ import annotations

from pathlib import Path

from app.data import cost_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- MC-1 (MED): live warehouse builders scope company by the COMPANY_SCOPE-aware UDF ---
def test_live_warehouse_builders_scope_by_company_udf_not_name_pattern():
    # A COMPANY_SCOPE-mapped warehouse (e.g. an operator maps COMPUTE_WH -> ALFA) must be
    # included in the ALFA view; the name-pattern warehouse_clause dropped it, disagreeing
    # with the COMPANY label and the mart path. Every live warehouse builder now filters via
    # COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) = '<company>'.
    for name, sql in {
        "warehouse_daily_credits": cost_sql.warehouse_daily_credits(30, "ALFA"),
        "warehouse_window_vs_prior": cost_sql.warehouse_window_vs_prior(30, "ALFA"),
        "hourly_credits": cost_sql.hourly_credits(24, "ALFA"),
        "cloud_services_ratio_by_warehouse": cost_sql.cloud_services_ratio_by_warehouse(30, "ALFA"),
        "qas_roi": cost_sql.qas_roi(30, "ALFA"),
        "allocated_attribution": cost_sql.allocated_attribution(30, "USER_NAME", "ALFA"),
        "compile_heavy_families": cost_sql.compile_heavy_families(30, "ALFA"),
        "cs_by_query_type": cost_sql.cs_by_query_type(30, "ALFA"),
    }.items():
        assert "COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) = 'ALFA'" in sql, name
        assert "WH!_ALFA" not in sql, f"{name} still uses the name-pattern scope"
    # ALL is a no-op company filter (behaviour unchanged from before the fix)
    assert "COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) =" not in cost_sql.warehouse_daily_credits(30, "ALL")
    # source: warehouse_clause(company) must be gone from the whole module
    assert "warehouse_clause(company)" not in _src("app/data/cost_sql.py")


# --- NAV-1 (LOW): a failed live db-inventory read no longer evicts a valid live-only DB ----
def test_db_scope_eviction_is_guarded_on_failed_inventory():
    m = _src("app/main.py")
    assert "if (_inv.ok and not _inv.empty) or not classify_databases([_cur_db], _company):" in m
    assert 'db_options = ["", _cur_db, *_opts]' in m       # valid live-only DB kept selectable


# --- CR-1 / CR-2 (LOW): router docs describe priority-dominates, not hit-count-wins ------
def test_router_docs_describe_priority_dominates():
    r = _src("app/logic/ask/router.py")
    assert "the most keyword hits wins" not in r            # stale docstring gone
    assert "HIGHEST-PRIORITY answerer wins" in r
    # the _score inline comment now calls it the tie-breaker, not the primary rank key
    assert "Rank strong candidates by total keyword hits" not in r
    assert "only the\n    # TIE-BREAKER among equal-priority strong candidates" in r
