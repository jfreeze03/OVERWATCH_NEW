"""Locks for v4.7.0: task-graph cost trends + warehouse change scorecard.

1. Migration V024 bookkeeping: ordering guard, version row, objects created,
   teardown coverage, validate expectation.
2. Graph cost builders honor company AND the Database/Schema filters, and
   measure credits (QUERY_ATTRIBUTION_HISTORY) rather than estimating.
3. Pure math: $/run allocation, success %, the CHEAPER/PRICIER/FLAT trend,
   and warehouse-change delta directions.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import change_impact_sql, graph_sql
from app.logic import wh_change
from app.logic.graphs import enrich_graph_daily, pipeline_summary

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V024__warehouse_change_scorecard.sql").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# 1. Migration bookkeeping
# ---------------------------------------------------------------------------

def test_v024_guard_blocks_out_of_order_apply():
    assert "EXCEPTION (-20024" in _MIG
    assert "SCHEMA_VERSION < 23" in _MIG
    assert "IF (v < 23) THEN" in _MIG


def test_v024_merges_version_row_24():
    assert "SELECT 24 AS VERSION" in _MIG
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION" in _MIG


def test_v024_creates_the_scorecard_objects():
    for obj in ("WAREHOUSE_CONFIG_SNAPSHOT", "WAREHOUSE_CHANGE_REGISTRY",
                "SP_WAREHOUSE_CHANGE_SCAN", "TASK_WAREHOUSE_CHANGE_SCAN"):
        assert obj in _MIG, obj
    assert "'WH_CHANGE_REGRESSION' AS RULE_ID" in _MIG
    assert "SHOW WAREHOUSES LIMIT 500" in _MIG          # no ACCOUNT_USAGE.WAREHOUSES here
    assert "RESULT_SCAN(LAST_QUERY_ID())" in _MIG


def test_v024_verdicts_and_alerts_live_in_the_proc():
    # Single source of truth: page and alert can never disagree.
    assert "'REGRESSED'" in _MIG and "'IMPROVED'" in _MIG and "'NO_BASELINE'" in _MIG
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" in _MIG
    assert "DEDUPE_KEY" in _MIG


def test_teardown_covers_v024_objects():
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8").upper()
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN" in teardown
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()" in teardown
    # Registry/snapshots preserved (frozen baselines are not rebuildable),
    # so they appear in the keep-list rather than as live drops.
    assert "'WAREHOUSE_CHANGE_REGISTRY'" in teardown
    assert "'WAREHOUSE_CONFIG_SNAPSHOT'" in teardown


def test_validate_expects_v024():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V024 applied" in validate
    assert "BETWEEN 1 AND 24) = 24" in validate


# ---------------------------------------------------------------------------
# 2. Graph cost builders — scoping and measurement
# ---------------------------------------------------------------------------

def test_graph_costs_measures_credits_per_run():
    sql = graph_sql.graph_daily_costs(30)
    assert "GRAPH_RUN_GROUP_ID" in sql
    assert "QUERY_ATTRIBUTION_HISTORY" in sql            # measured, not estimated
    assert "MIN_BY" in sql                               # pipeline = root (first to start)
    assert "LIMIT 5000" in sql


def test_graph_costs_honors_database_filter():
    sql = graph_sql.graph_daily_costs(30, "ALFA", "ALFA_EDW_PRD", "EDW")
    assert "ALFA_EDW_PRD" in sql                         # the user's ask: db-filterable
    assert "SCHEMA_NAME" in sql and "EDW" in sql
    unfiltered = graph_sql.graph_daily_costs(30, "ALFA")
    assert "ALFA_EDW_PRD" not in unfiltered


def test_graph_costs_company_scope_and_day_clamp():
    trexis = graph_sql.graph_daily_costs(30, "Trexis")
    assert "TRXS" in trexis
    clamped = graph_sql.graph_daily_costs(999999)
    assert "-90," in clamped                             # bounded live window


def test_serverless_builder_scopes_and_filters():
    sql = graph_sql.serverless_task_daily(30, "ALFA", "ALFA_EDW_PRD")
    assert "SERVERLESS_TASK_HISTORY" in sql
    assert "ALFA_EDW_PRD" in sql
    assert "SERVERLESS_CREDITS" in sql


def test_wh_registry_builder_scopes_company_and_warehouse():
    sql = change_impact_sql.warehouse_change_registry(90, "Trexis", "TRANSFORM")
    assert "WAREHOUSE_CHANGE_REGISTRY" in sql
    assert "COMPANY = 'Trexis'" in sql
    assert "TRANSFORM" in sql
    assert "COMPANY = '" not in change_impact_sql.warehouse_change_registry(90, "ALL")


def test_wh_series_builder_validates_the_name():
    sql = change_impact_sql.warehouse_daily_series("WH_TRXS_TRANSFORM", 28)
    assert "WAREHOUSE_METERING_HISTORY" in sql and "'WH_TRXS_TRANSFORM'" in sql
    with pytest.raises(ValueError):
        change_impact_sql.warehouse_daily_series("bad name; DROP TABLE X")


def test_wh_scan_call_targets_the_new_proc():
    assert change_impact_sql.run_wh_scan_call() == \
        "CALL DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()"


# ---------------------------------------------------------------------------
# 3. Graph math
# ---------------------------------------------------------------------------

def _daily(days_credits_runs_fails, pipeline="ROOT_A"):
    rows = []
    for i, (credits, runs, fails) in enumerate(days_credits_runs_fails):
        rows.append({
            "DAY": pd.Timestamp("2026-06-01") + pd.Timedelta(days=i),
            "PIPELINE": pipeline, "DATABASE_NAME": "ALFA_EDW_PRD", "SCHEMA_NAME": "EDW",
            "GRAPH_RUNS": runs, "RUNS_WITH_FAILURES": fails, "TASK_RUNS": runs * 3,
            "AVG_WALL_SEC": 100.0, "P95_WALL_SEC": 200.0, "WH_CREDITS": credits,
        })
    return pd.DataFrame(rows)


def test_enrich_dollars_and_allocated_per_run():
    out = enrich_graph_daily(_daily([(2.0, 4, 1)]), rate_usd=3.68)
    row = out.iloc[0]
    assert row["USD"] == 7.36                            # 2 credits at 3.68
    assert row["USD_PER_RUN"] == 1.84                    # allocated: day $ / day runs
    assert row["SUCCESS_PCT"] == 75.0


def test_enrich_empty_frame_is_safe():
    out = enrich_graph_daily(pd.DataFrame(), 3.68)
    assert out.empty and "USD" in out.columns


def test_summary_trend_pricier_and_cheaper():
    # $/run 1.0 for four days then 2.0 for four days -> PRICIER (and reverse).
    pricier = pipeline_summary(enrich_graph_daily(
        _daily([(1, 1, 0)] * 4 + [(2, 1, 0)] * 4), rate_usd=1.0))
    assert pricier.iloc[0]["TREND"] == "PRICIER"
    cheaper = pipeline_summary(enrich_graph_daily(
        _daily([(2, 1, 0)] * 4 + [(1, 1, 0)] * 4), rate_usd=1.0))
    assert cheaper.iloc[0]["TREND"] == "CHEAPER"


def test_summary_trend_flat_and_thin():
    flat = pipeline_summary(enrich_graph_daily(_daily([(1, 1, 0)] * 6), rate_usd=1.0))
    assert flat.iloc[0]["TREND"] == "FLAT"
    thin = pipeline_summary(enrich_graph_daily(_daily([(1, 1, 0)] * 3), rate_usd=1.0))
    assert thin.iloc[0]["TREND"] == "n/a"                # < 4 active days


def test_summary_totals_reconcile():
    out = pipeline_summary(enrich_graph_daily(_daily([(1, 2, 0), (3, 2, 2)]), rate_usd=2.0))
    row = out.iloc[0]
    assert row["USD"] == 8.0                             # (1+3) credits at $2
    assert row["GRAPH_RUNS"] == 4
    assert row["USD_PER_RUN"] == 2.0
    assert row["SUCCESS_PCT"] == 50.0                    # 2 of 4 runs failed


# ---------------------------------------------------------------------------
# 3b. Warehouse change deltas
# ---------------------------------------------------------------------------

def test_change_deltas_directions():
    row = {
        "BASELINE_CREDITS_PER_DAY": 10.0, "AFTER_CREDITS_PER_DAY": 15.0,   # worse
        "BASELINE_P95_S": 100.0, "AFTER_P95_S": 50.0,                      # better
        "BASELINE_QUEUED_MIN_PER_DAY": 20.0, "AFTER_QUEUED_MIN_PER_DAY": 20.5,  # flat (2.5%)
        "BASELINE_FAIL_PCT": 0.0, "AFTER_FAIL_PCT": 0.0,                   # flat (0/0)
    }
    deltas = {d["metric"]: d for d in wh_change.change_deltas(row)}
    assert deltas["credits/day"]["direction"] == "worse"
    assert deltas["credits/day"]["delta_pct"] == 50.0
    assert deltas["p95 s"]["direction"] == "better"
    assert deltas["queue min/d"]["direction"] == "flat"
    assert deltas["fail %"]["direction"] == "flat"


def test_change_deltas_new_load_from_zero_base():
    deltas = wh_change.change_deltas(
        {"BASELINE_CREDITS_PER_DAY": 0.0, "AFTER_CREDITS_PER_DAY": 5.0})
    assert deltas[0]["direction"] == "worse"
    assert deltas[0]["delta_pct"] is None               # something from nothing


def test_change_deltas_skips_missing_sides():
    assert wh_change.change_deltas({"BASELINE_CREDITS_PER_DAY": 10.0}) == []


def test_registry_kpis_counts_and_empty():
    df = pd.DataFrame({"VERDICT": ["REGRESSED", "IMPROVED", "PENDING", "NO_BASELINE", "NEUTRAL"]})
    assert wh_change.registry_kpis(df) == {
        "changes": 5, "regressed": 1, "improved": 1, "pending": 2}
    assert wh_change.registry_kpis(pd.DataFrame()) == {
        "changes": 0, "regressed": 0, "improved": 0, "pending": 0}
