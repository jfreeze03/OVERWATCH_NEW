"""Per-warehouse cloud-services elevation drill (owner 2026-08-20): the
"Why is it elevated?" compile-heavy families + CS-by-statement-type now scope to a
warehouse selected in the ratio table, so a DBA can see (and target) the specific
queries driving THAT warehouse's cloud-services pressure."""

from __future__ import annotations

from pathlib import Path

from app.data import cost_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_compile_heavy_families_scopes_to_warehouse():
    sql = cost_sql.compile_heavy_families(30, "ALFA", warehouse="WH_ALFA_TRANSFORM_PRD")
    assert "WAREHOUSE_NAME = 'WH_ALFA_TRANSFORM_PRD'" in sql
    # scoped drill relaxes the sample floor (one warehouse has fewer runs per family)
    assert "HAVING COUNT(*) >= 5" in cost_sql.compile_heavy_families(30, "ALFA", warehouse="X", min_runs=5)


def test_compile_heavy_families_account_wide_unchanged():
    # default (no warehouse) keeps the 20-run floor and adds no warehouse predicate.
    sql = cost_sql.compile_heavy_families(30, "ALFA")
    assert "HAVING COUNT(*) >= 20" in sql
    assert "WAREHOUSE_NAME =" not in sql


def test_scoped_drill_drops_redundant_company_predicate():
    # An exact warehouse is a complete scope. The hardcoded company predicate must be
    # dropped when scoping, else a warehouse mapped to a company via the runtime
    # COMPANY_SCOPE table (but not the hardcoded tuple/prefix) is zeroed -> false-empty
    # for a warehouse the ratio table just flagged ELEVATED.
    unscoped = cost_sql.compile_heavy_families(30, "ALFA")
    scoped = cost_sql.compile_heavy_families(30, "ALFA", warehouse="WH_TRXS_X")
    assert "WH!_ALFA!_%" in unscoped and "WH!_ALFA!_%" not in scoped   # company prefix dropped
    assert "WAREHOUSE_NAME = 'WH_TRXS_X'" in scoped
    # same for the CS-by-type builder
    assert "WH!_ALFA!_%" not in cost_sql.cs_by_query_type(30, "ALFA", warehouse="WH_TRXS_X")


def test_cs_by_query_type_scopes_to_warehouse():
    sql = cost_sql.cs_by_query_type(30, "ALFA", warehouse="WH_ALFA_LOAD_PRD")
    assert "WAREHOUSE_NAME = 'WH_ALFA_LOAD_PRD'" in sql
    assert "WAREHOUSE_NAME =" not in cost_sql.cs_by_query_type(30, "ALFA")   # unscoped: no filter


def test_warehouse_name_is_quote_safe():
    # sql_literal escapes an embedded quote (defence-in-depth; real names won't have one).
    sql = cost_sql.compile_heavy_families(30, "ALL", warehouse="WH'X")
    assert "WAREHOUSE_NAME = 'WH''X'" in sql


def test_drill_wired_into_spend_page():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    # ratio table is selectable and drives a per-warehouse scoped read
    assert 'selectable_table(\n            csr.df, key=f"csr_sel_' in src
    assert "compile_heavy_families(days, company, warehouse=_sel_wh, min_runs=5, bounds=bounds)" in src
    assert "cs_by_query_type(days, company, warehouse=_sel_wh, bounds=bounds)" in src
    # bounds guard on the sticky selection + the account-wide fallback both survive
    assert "0 <= int(_wh_sel) < len(csr.df)" in src
    assert "family_compile_heavy(days, company, bounds=bounds)" in src
