"""v4.134 recommendation round: cost depth, evidence, forecasts, and exports."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import cost_sql, insights_sql, mart_sql
from app.logic import metric_registry
from app.logic.call_tree import build_call_tree
from app.logic.capacity import capacity_forecasts
from app.logic.cost_coverage import drill_ready_spend_share, service_coverage_inventory
from app.logic.formulas import (
    ExecutiveSummaryView,
    executive_slide_bullets,
    executive_summary_csv,
    executive_summary_html,
)
from app.ui import charts

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_cost_coverage_uses_service_specific_rates_and_exposes_material_gap():
    frame = pd.DataFrame({
        "SERVICE_TYPE": ["WAREHOUSE_METERING", "REPLICATION", "MYSTERY_SERVICE"],
        "CREDITS_BILLED": [10.0, 5.0, 1.0],
    })
    coverage = service_coverage_inventory(frame, rate=4.0, ai_rate=2.0)
    assert coverage["SERVICE_TYPE"].tolist() == [
        "WAREHOUSE_METERING", "REPLICATION", "MYSTERY_SERVICE",
    ]
    assert coverage["BILLED_USD"].tolist() == [40.0, 20.0, 4.0]
    unknown = coverage[coverage["SERVICE_TYPE"] == "MYSTERY_SERVICE"].iloc[0]
    assert unknown["DRILL_STATUS"] == "Service total only"
    assert unknown["MATERIALITY"] == "Material"
    assert round(drill_ready_spend_share(coverage), 2) == 93.75


def test_coco_cortex_code_is_priced_at_the_ai_rate_not_compute():
    # SNOWFLAKE_COCO_SNOWSIGHT (Cortex Code / CoWork) carries neither a CORTEX
    # token nor an AI prefix, so a name-only match priced it at the compute rate
    # and over-stated it ~67%. It must categorize AI / Cortex and price at ai_rate
    # — but keep the honest "Service total only" drill (CoCo compute has no
    # per-user usage history to drill).
    frame = pd.DataFrame({
        "SERVICE_TYPE": ["SNOWFLAKE_COCO_SNOWSIGHT"],
        "CREDITS_BILLED": [100.0],
    })
    row = service_coverage_inventory(frame, rate=3.68, ai_rate=2.20).iloc[0]
    assert row["CATEGORY"] == "AI / Cortex"
    assert round(float(row["BILLED_USD"]), 2) == 220.0   # 100 * 2.20 (AI), not 368.0 (compute)
    assert row["DRILL_STATUS"] == "Service total only"


def test_hybrid_request_label_discloses_that_the_charge_is_historical():
    frame = pd.DataFrame({
        "SERVICE_TYPE": ["HYBRID_TABLE_REQUESTS"],
        "CREDITS_BILLED": [2.0],
    })
    coverage = service_coverage_inventory(frame, rate=3.68, ai_rate=2.20)
    assert coverage.iloc[0]["CATEGORY"] == "Hybrid requests (historical)"


def test_native_cost_builders_pin_grain_scope_window_and_currency_contracts():
    replication = cost_sql.replication_by_database(999, "ALFA", "ALFA_EDW_PRD")
    assert "DATABASE_REPLICATION_USAGE_HISTORY" in replication
    assert "DATEADD('day', -365" in replication
    assert "'ALFA_EDW_PRD'" in replication
    assert "BYTES_TRANSFERRED" in replication and "GROUP BY DATABASE_NAME" in replication

    pools = cost_sql.compute_pool_usage(30)
    notebooks = cost_sql.notebook_container_usage(30)
    marketplace = cost_sql.marketplace_paid_usage(30)
    assert "SNOWPARK_CONTAINER_SERVICES_HISTORY" in pools and "APPLICATION_NAME" in pools
    assert "NOTEBOOKS_CONTAINER_RUNTIME_HISTORY" in notebooks
    assert "NOTEBOOK_EXECUTION_TIME_SECS" in notebooks
    assert "MARKETPLACE_PAID_USAGE_DAILY" in marketplace
    assert "CONSUMER_ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()" in marketplace
    assert "CURRENCY" in marketplace and "CREDIT_PRICE" not in marketplace


def test_cost_service_detail_is_lazy_and_notebook_spend_is_declared_non_additive():
    source = _source("app/ui/pages/cost_parts/spend.py")
    assert 'st.toggle(\n        "Load detailed service attribution"' in source
    assert "cost_sql.replication_by_database(days, company, database)" in source
    assert "cost_sql.compute_pool_usage(days)" in source
    assert "cost_sql.notebook_container_usage(days)" in source
    assert "Notebook credits are a subset" in source
    assert "native currency and are not converted" in source


def test_call_children_sql_and_tree_preserve_parent_order_and_numeric_costs():
    sql = insights_sql.call_children_costs("root-call", 7)
    assert "a.PARENT_QUERY_ID" in sql and "a.ROOT_QUERY_ID" in sql
    assert "CREDITS_USED_QUERY_ACCELERATION" in sql
    frame = pd.DataFrame([
        {"QUERY_ID": "grandchild", "PARENT_QUERY_ID": "child", "ROOT_QUERY_ID": "root",
         "STEP_TYPE": "INSERT", "STEP_START": "2026-08-03 10:02", "CREDITS": 0.3},
        {"QUERY_ID": "root", "PARENT_QUERY_ID": "", "ROOT_QUERY_ID": "root",
         "STEP_TYPE": "CALL (own time)", "STEP_START": "2026-08-03 10:00", "CREDITS": 0.1},
        {"QUERY_ID": "child", "PARENT_QUERY_ID": "root", "ROOT_QUERY_ID": "root",
         "STEP_TYPE": "SELECT", "STEP_START": "2026-08-03 10:01", "CREDITS": 0.2},
    ])
    tree = build_call_tree(frame, "root")
    assert tree["QUERY_ID"].tolist() == ["root", "child", "grandchild"]
    assert tree["DEPTH"].tolist() == [0, 1, 2]
    assert tree["STEP"].tolist() == ["CALL (own time)", "+- SELECT", "|  +- INSERT"]
    assert pd.api.types.is_numeric_dtype(tree["CREDITS"])


def test_ranked_bar_takeaway_is_opt_in(monkeypatch):
    captions: list[str] = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda *args, **kwargs: None)
    monkeypatch.setattr(charts.st, "caption", captions.append)
    frame = pd.DataFrame({"NAME": ["A", "B"], "USD": [75.0, 25.0]})
    charts.bar_usd(frame, "NAME", "USD")
    assert captions == []
    charts.bar_usd(frame, "NAME", "USD", takeaway=True)
    assert captions == ["Top: A $75 (75% of $100)."]


def test_shared_executive_model_renders_projector_print_slides_and_csv_safely():
    view = ExecutiveSummaryView(
        company="ALFA <prod>",
        days=30,
        generated="2026-08-03 09:00 (account time)",
        cards=(("Window spend", "$1,200"), ("Open alerts", "1 critical")),
        drivers=(("Queueing", "-4 pts", "WH_A > threshold"),),
        actions=("Resize WH_A after review",),
        spend_series=(10.0, 12.0, 15.0),
        scope_notes=("Window spend is company-scoped.",),
    )
    html = executive_summary_html(view, presentation=True)
    assert html.startswith("<!DOCTYPE html>") and 'class="presentation"' in html
    assert "@media print" in html and "<polyline" in html
    assert "ALFA &lt;prod&gt;" in html and "<script" not in html.lower()
    bullets = executive_slide_bullets(view)
    assert "Window spend: $1,200" in bullets and "Immediate action" in bullets
    csv_text = executive_summary_csv(view)
    assert "SECTION,METRIC,VALUE,DETAIL" in csv_text
    assert "Score deduction,Queueing,-4 pts,WH_A > threshold" in csv_text


def _capacity_frame(days: int = 60, *, rising: bool = True, changed: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=days, freq="D")
    rows = []
    for index, day in enumerate(dates):
        pressure = 0.10 + 0.011 * index if rising else 0.08
        rows.append({
            "DAY": day,
            "WAREHOUSE_NAME": "WH_TEST",
            "COMPANY": "ALFA",
            "QUERY_COUNT": 100 + index * 3 if rising else 100,
            "QUEUED_MIN": pressure * 30.0,
            "SPILL_REMOTE_GB": pressure * 0.25,
            "P95_ELAPSED_SEC": 10.0,
            "CREDITS_TOTAL": 10.0 + index * 0.2 if rising else 10.0,
            "CHANGE_EVENTS": 1 if changed and index == days - 1 else 0,
        })
    return pd.DataFrame(rows)


def test_capacity_forecast_emits_eta_only_for_dense_stable_backtested_growth():
    forecast = capacity_forecasts(_capacity_frame(), as_of=date(2026, 7, 31))
    row = forecast.iloc[0]
    assert row["STATUS"] == "FORECAST"
    assert 0 < row["DAYS_TO_PRESSURE"] <= 180
    assert row["ETA_LOW_DAYS"] <= row["DAYS_TO_PRESSURE"] <= row["ETA_HIGH_DAYS"]
    assert row["R2"] > 0.9 and row["BACKTEST_MAE"] < 0.1


def test_capacity_forecast_refuses_sparse_flat_and_recently_changed_evidence():
    sparse = capacity_forecasts(_capacity_frame(days=20), as_of=date(2026, 6, 21)).iloc[0]
    stable = capacity_forecasts(_capacity_frame(rising=False), as_of=date(2026, 7, 31)).iloc[0]
    changed = capacity_forecasts(_capacity_frame(changed=True), as_of=date(2026, 7, 31)).iloc[0]
    assert sparse["STATUS"] == "INSUFFICIENT" and pd.isna(sparse["DAYS_TO_PRESSURE"])
    assert stable["STATUS"] == "STABLE" and pd.isna(stable["DAYS_TO_PRESSURE"])
    assert changed["STATUS"] == "UNSTABLE" and pd.isna(changed["DAYS_TO_PRESSURE"])


def test_capacity_forecast_refuses_stale_but_otherwise_strong_history():
    row = capacity_forecasts(_capacity_frame(), as_of=date(2026, 8, 3)).iloc[0]
    assert row["STATUS"] == "STALE"
    assert row["STALE_DAYS"] == 3
    assert pd.isna(row["DAYS_TO_PRESSURE"])
    assert "refresh telemetry" in row["BASIS"]


def test_capacity_forecast_separates_inactive_warehouse_from_fresh_source():
    recent = _capacity_frame()
    older = _capacity_frame(days=40).assign(WAREHOUSE_NAME="WH_OLD")
    frame = pd.concat([recent, older], ignore_index=True)
    result = capacity_forecasts(frame, as_of=date(2026, 7, 31)).set_index("WAREHOUSE_NAME")
    assert result.loc["WH_TEST", "STATUS"] == "FORECAST"
    assert result.loc["WH_OLD", "STATUS"] == "INACTIVE"
    assert result.loc["WH_OLD", "STALE_DAYS"] == 0
    assert result.loc["WH_OLD", "ACTIVITY_GAP_DAYS"] > 2


def test_capacity_sql_uses_complete_mart_days_and_capacity_change_points():
    sql = mart_sql.warehouse_capacity_daily(90, "ALFA")
    assert "DATE(q.HOUR_TS) < CURRENT_DATE()" in sql
    assert "FACT_QUERY_HOURLY" in sql and "FACT_WAREHOUSE_DAILY" in sql
    assert "WAREHOUSE_CHANGE_REGISTRY" in sql
    assert "SOURCE_LATEST_DAY" in sql and "CROSS JOIN source_freshness" in sql
    assert "'SIZE', 'MIN_CLUSTERS', 'MAX_CLUSTERS', 'SCALING_POLICY'" in sql
    assert "q.COMPANY = 'ALFA'" in sql


def test_v072_is_entity_scoped_evidence_bearing_and_view_only():
    migration = _source("snowflake/migrations/V072__entity_aware_incident_proposals.sql")
    assert "EXCEPTION (-20072" in migration and "IF (v < 71)" in migration
    assert "GROUP BY FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME" in migration
    assert "MATCHED_WH_CHANGES" in migration and "MATCHED_OBJECT_CHANGES" in migration
    assert "MATCHED_TASK_FAILURES" in migration and "CONFIDENCE" in migration
    assert "FAMILY || '|' || COMPANY || '|' || ENTITY_KIND || '|' || ENTITY_NAME" in migration
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS" not in migration
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS" not in migration
    assert "SELECT 72 AS VERSION" in migration


def test_incident_app_carries_entity_scope_but_preserves_legacy_proposals():
    source = _source("app/ui/pages/control_room.py")
    assert 'proposal_parts = str(proposal_key).split("|", 3)' in source
    assert "proposal_parts[2].upper() != \"ACCOUNT\"" in source
    assert "MATCHED_OBJECT_CHANGES" in source and "Human confirmation is still required" in source
    assert "confirm_gate(\"DECLARE\"" in source
    reader = mart_sql.incident_proposals(20, "ALFA")
    assert "SELECT *" in reader
    assert "(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')" in reader


def test_targeted_typography_and_metric_registry_contracts_are_pinned():
    theme = _source("app/theme.py")
    assert ".ow-help {" in theme and "font-size:0.75rem" in theme
    assert ".ow-kicker { font-size:0.75rem; letter-spacing:0;" in theme
    for key in (
        "replication_usage", "compute_pool_usage", "notebook_container_usage",
        "marketplace_paid_usage", "capacity_pressure_forecast",
    ):
        assert metric_registry.get(key) is not None
    assert metric_registry.get("marketplace_paid_usage").unit == "currency"
    assert metric_registry.validate() == []


def test_new_account_usage_builders_are_canaried_and_org_view_is_deliberately_lazy():
    canary = _source("app/data/canary.py")
    for name in (
        "cost_sql.replication_by_database", "cost_sql.compute_pool_usage",
        "cost_sql.notebook_container_usage", "mart_sql.warehouse_capacity_daily",
    ):
        assert name in canary
    assert "cost_sql.marketplace_paid_usage" not in canary
    spend = _source("app/ui/pages/cost_parts/spend.py")
    assert "ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY" in spend
