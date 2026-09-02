"""App-wide close-out of the MC-1 company-scope class (v4.441.0).

Every company-scope FILTER in the data builders now runs through the
COMPANY_SCOPE-aware UDFs (COMPANY_FOR_WAREHOUSE / COMPANY_FOR_DATABASE) via the
canonical ``companies.warehouse_company_scope`` / ``companies.database_company_scope``
helpers — the SAME axis the marts FILTER by and the boards LABEL by. The
name-pattern clauses (``warehouse_clause`` / ``database_clause``) test membership
by NAME PATTERN and silently ignore an operator's COMPANY_SCOPE mapping, so a
warehouse/database mapped in COMPANY_SCOPE but off the seeded name pattern used
to scope one way on the mart/label side and a different way on any name-pattern
board (the MC-1 class: round 11 cost_sql, round 16 insights_sql, here the rest).

``role_clause`` is deliberately NOT converted: COMPANY_FOR_ROLE does not read
COMPANY_SCOPE (role grain has no operator mapping — role names are the only
company signal there), so the name-pattern IS the authoritative role axis.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app import companies
from app.data import (
    app_cost_sql,
    chargeback_sql,
    cost_sql,
    etl_sql,
    graph_sql,
    insights_sql,
    mart27_sql,
    ops_sql,
    security_sql,
)

_ROOT = Path(__file__).resolve().parents[1]
_DATA = _ROOT / "app" / "data"


# --- class guard: NO name-pattern company-FILTER call survives in app/data ----------
def test_no_name_pattern_company_filter_calls_remain():
    """The whole point of the sweep: not one data builder may FILTER company by the
    name-pattern clauses. A regression here re-opens the MC-1 divergence for any
    COMPANY_SCOPE-mapped-but-off-pattern warehouse/database."""
    offenders = []
    for path in sorted(_DATA.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.name}: {needle}"
            for needle in ("companies.warehouse_clause(company",
                           "companies.database_clause(company")
            if needle in text
        )
    assert not offenders, "name-pattern company FILTER still present: " + "; ".join(offenders)


# --- canonical helper contract ------------------------------------------------------
def test_warehouse_company_scope_form():
    assert companies.warehouse_company_scope("ALL") == ""
    assert companies.warehouse_company_scope("", "q.WH") == ""  # empty -> ALL -> no filter
    assert (companies.warehouse_company_scope("ALFA")
            == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) = 'ALFA'")
    assert (companies.warehouse_company_scope("Trexis", "q.WAREHOUSE_NAME")
            == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) = 'Trexis'")


def test_database_company_scope_form():
    assert companies.database_company_scope("ALL") == ""
    assert (companies.database_company_scope("Trexis", "t.TABLE_CATALOG")
            == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(t.TABLE_CATALOG) = 'Trexis'")


def test_company_scope_helpers_are_injection_safe():
    """An out-of-domain company value is fail-closed: a quote-doubled string literal,
    never an executable break-out (matches the established _wh_company_scope contract)."""
    for fn in (companies.warehouse_company_scope, companies.database_company_scope):
        out = fn("ALFA'; DROP TABLE X--")
        assert "'ALFA''; DROP TABLE X--'" in out       # doubled quote = escaped
        assert "= 'ALFA'; DROP" not in out              # NOT an unescaped break-out


# --- every converted builder carries the UDF axis + parses --------------------------
def test_converted_builders_carry_udf_axis_and_parse():
    wh = "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE"
    db = "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE"
    cases = [
        # (label, sql, expected UDF substring)
        ("ops.warehouse_concurrency_peaks", ops_sql.warehouse_concurrency_peaks(30, "ALFA"), wh),
        ("ops.task_runs", ops_sql.task_runs(30, "Trexis"), db),
        ("ops.copy_load_failures", ops_sql.copy_load_failures(30, "ALFA"), db),
        ("chargeback.role_share_within_warehouse",
         chargeback_sql.role_share_within_warehouse(30, "Trexis"), wh),
        ("chargeback.role_department_map_join",
         chargeback_sql.role_department_map_join(30, "ALFA"), wh),
        ("security.recent_ddl_changes_fact",
         security_sql.recent_ddl_changes_fact(7, "ALFA"), db),
        ("security.untagged_objects", security_sql.untagged_objects("Trexis"), db),
        ("cost.replication_by_database", cost_sql.replication_by_database(30, "ALFA"), db),
        ("insights.storage_waste", insights_sql.storage_waste("ALFA"), db),
        ("mart27.task_nodes", mart27_sql.task_nodes(30, "Trexis"), db),
        ("graph.graph_daily_costs", graph_sql.graph_daily_costs(30, "ALFA"), db),
        ("app_cost.app_cost_live", app_cost_sql.app_cost_live(30, "ALFA"), wh),
        ("etl.etl_cost_by_pipeline", etl_sql.etl_cost_by_pipeline(30, "Trexis"), wh),
    ]
    for label, sql, needle in cases:
        assert needle in sql, f"{label}: missing {needle}"
        sqlglot.parse_one(sql, read="snowflake")


def test_all_scope_adds_no_company_filter():
    """A pure-filter builder under ALL must not gain any COMPANY_FOR_* predicate."""
    assert "COMPANY_FOR_WAREHOUSE" not in ops_sql.warehouse_concurrency_peaks(30, "ALL")
    assert "COMPANY_FOR_DATABASE" not in ops_sql.task_runs(30, "ALL")
