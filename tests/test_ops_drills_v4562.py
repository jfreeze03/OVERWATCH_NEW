"""Locks for the Operations / Cost drill features (v4.562, owner asks 2026-09-21):

- Volume drops honors the scope-bar company/database/schema filter (was account-wide and mixed
  companies in the same table — the owner's "filtering issue").
- QAS ROI drills a clicked warehouse into its acceleration-ELIGIBLE queries ("the next question").
- The SP $/call leaderboard drills a clicked proc into its child-statement cost breakdown, rendered
  directly (the old click only prefilled a COLLAPSED trend expander, so it "did nothing").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import cost_sql, insights_sql, ops_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _parse(sql: str) -> None:
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, dialect="snowflake")


# --------------------------------------------------------------------------- Volume drops scope
def test_volume_deltas_all_stays_account_wide_and_keeps_its_guards():
    sql = ops_sql.volume_deltas()
    assert "COMPANY_FOR_DATABASE" not in sql            # ALL -> no company predicate
    assert "ALFA_EDW" not in sql
    # the existing steady-baseline + weekday-off guards must survive the scope refactor
    assert "AVG_ROWS >= 1000 AND DAYS_ACTIVE_7D >= 3" in sql
    assert "SAME_DOW_ROWS" in sql
    _parse(sql)


def test_volume_deltas_scoped_applies_company_db_schema():
    sql = ops_sql.volume_deltas("ALFA", "ALFA_EDW_PRD", "PUBLIC")
    assert "COMPANY_FOR_DATABASE(d.DATABASE_NAME)" in sql   # company by database, table grain
    assert "ALFA_EDW_PRD" in sql                            # exact database
    assert "d.SCHEMA_NAME ILIKE" in sql                     # schema contains
    _parse(sql)


def test_volume_deltas_scope_is_threaded_in_operations():
    src = _read("app/ui/pages/operations.py")
    assert "ops_sql.volume_deltas(company, database, schema_contains)" in src
    # the tab now takes + forwards schema_contains from the scope bar
    assert 'f["schema_contains"])' in src
    assert "def _pipeline_sla_tab(is_operator: bool, company: str" in src
    assert "schema_contains" in src.split("def _pipeline_sla_tab", 1)[1][:400]


# --------------------------------------------------------------------------- QAS eligible-query drill
def test_qas_eligible_queries_warehouse_exact_and_ranked():
    sql = cost_sql.qas_eligible_queries("WH_ALFA_QUERY", 60)
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_ACCELERATION_ELIGIBLE" in sql
    assert "WAREHOUSE_NAME = 'WH_ALFA_QUERY'" in sql
    assert "AS ELIGIBLE_SEC" in sql and "AS SCALE_FACTOR" in sql
    assert "ORDER BY ELIGIBLE_QUERY_ACCELERATION_TIME DESC" in sql
    _parse(sql)


def test_qas_eligible_queries_quotes_a_hostile_warehouse():
    sql = cost_sql.qas_eligible_queries("x' OR '1'='1", 30)
    assert "x'' OR ''1''=''1" in sql                        # stays inside the literal


def test_qas_drill_is_wired_in_optimize():
    src = _read("app/ui/pages/cost_parts/optimize.py")
    assert 'key="opt_qas_sel"' in src                       # the QAS table is now selectable
    assert "cost_sql.qas_eligible_queries(_qwh, days, bounds=bounds)" in src


# --------------------------------------------------------------------------- SP child-cost drill
def test_procedure_child_cost_breakdown_structure():
    sql = insights_sql.procedure_child_cost_breakdown(
        "DB_SP_PROD.SP_F_BILG_PMT", 30, "ALFA", "ALFA_EDW_PRD", "PUBLIC")
    # children aggregate by parameterized hash; the CALL's own attribution is one labeled bucket
    assert "QUERY_PARAMETERIZED_HASH" in sql
    assert "CALL (own overhead)" in sql and "ROOT_QUERY_ID IS NULL" in sql
    assert "AS EXECUTIONS" in sql
    # SAME ROOT_QUERY_ID rollup as the leaderboard/trend, so the breakdown reconciles
    assert "COALESCE(a.ROOT_QUERY_ID, a.QUERY_ID) IN (SELECT QUERY_ID FROM named)" in sql
    _parse(sql)


def test_procedure_child_breakdown_reconciles_with_the_leaderboard_row():
    # The drill targets ONE $/call-leaderboard row (procedure_costs_usd groups by exact
    # PROC_NAME + DATABASE + SCHEMA), so to sum to that row's TOTAL_CREDITS it must match the
    # SAME way: CURRENT_TIMESTAMP call window (NOT day-grain CURRENT_DATE, which would over-count
    # the trailing boundary slice — verify finding wjbxr0hbh MED), EXACT proc name (no
    # bare->qualified suffix arm that would pull a sibling row), and EXACT db + schema.
    bd = insights_sql.procedure_child_cost_breakdown("DB.SC.SP_X", 30, "ALFA", "MYDB", "PUBLIC")
    lb = insights_sql.procedure_costs_usd(30, "ALFA")
    assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" in bd     # same call anchor as leaderboard
    assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" in lb
    assert "CURRENT_DATE()" not in bd                           # NOT the day-grain anchor
    assert "PROC_NAME = " in bd and "PROC_NAME LIKE" not in bd  # exact name, no suffix arm
    assert "UPPER(c.SCHEMA_NAME) = 'PUBLIC'" in bd              # exact schema, not contains
    assert "MYDB" in bd                                         # exact database
    # attribution read shares the leaderboard's +1d lag headroom window
    assert "DATEADD('day', -31, CURRENT_TIMESTAMP())" in bd and "-31, CURRENT_TIMESTAMP" in lb


def test_sp_breakdown_renders_directly_not_only_in_the_collapsed_expander():
    src = _read("app/ui/pages/cost_parts/unit_costs.py")
    assert "insights_sql.procedure_child_cost_breakdown(" in src
    assert "Cost breakdown for" in src                      # rendered at the leaderboard, visible
