"""House law 4 ("every SQL builder gets a canary"), enforced for the cost / mart builder modules (v4.608).

R2-062 / R2-064 / R2-068: the ORGANIZATION_USAGE readers (the All-in invoice tile, the org balance behind every
runway, the rate-card reconciliation), ALERT_EVENTS' SNOOZE_* reader, the MART_TABLE_STORAGE_DAILY byte readers,
the email-path ALERT_HISTORY read and MART_TASK_NODE_DAILY's timing reader had no canary. Several are probe=True
reads, which log neither an absent object nor a missing column, so a renamed column failed silently and Admin >
Canary still said "All N applicable canary statements passed".

The ratchet below fails on any public builder in cost_sql / mart_sql / mart27_sql that has neither a canary
nor a named exemption with its reason, and on an exemption that is no longer needed (the list only shrinks).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import sqlglot

from app.data import canary, cost_sql, mart27_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_CANARY_SRC = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")

_TWIN = "reads only columns a registered canary already compiles (R2-062/R2-064 review, 2026-09-30)"
# Frozen: names may only LEAVE this map (when they gain a canary). A new builder needs a canary or a reason here.
CANARY_EXEMPT: dict[str, dict[str, str]] = {
    "cost_sql": {
        "hourly_credits": _TWIN,
        "contract_consumed_credits": _TWIN,
        "untagged_executions_for_user": _TWIN,
        "marketplace_paid_usage": "deliberately lazy org view, locked out by test_v4134_recommendations",
        "qas_roi": "edition-sensitive QUERY_ACCELERATION views; documented exemption (4.178.0)",
        "qas_eligible_queries": "edition-sensitive QUERY_ACCELERATION views; documented exemption (4.178.0)",
        "native_anomaly_insights": "SELECT * on the optional SNOWFLAKE.LOCAL feed: a canary can only see absence",
    },
    "mart_sql": {
        "app_statement_stats_telemetry": _TWIN,
        "open_alert_severity_counts": _TWIN,
        "since_last_visit": _TWIN,
        "verified_wins": _TWIN,
        "app_self_cost_usd": _TWIN,
        "app_warehouse_queue_by_hour": _TWIN,
        "health_strip": _TWIN,
        "ledger_for_event": _TWIN,
        "events_for_rule": _TWIN,
        "resolutions_for_rule": _TWIN,
        "last_delivery_health": _TWIN,
        "action_acceptance": _TWIN,
        "incident_gantt": _TWIN,
        "supersede_ledger_twins_sql": "an UPDATE (a write), not a read",
        "email_alert_objects": "SHOW ALERTS: EXPLAIN cannot compile a SHOW",
        "flyway_history": "deliberately not canaried (docstring + test_prep_iac)",
        "email_notification_history": ("NOTIFICATION_HISTORY(INTEGRATION_NAME => 'OVERWATCH_EMAIL') on an install "
                                       "without the opt-in integration is unproven (owner probe first)"),
        "ml_forecast_daily": "reads the opt-in FORECAST_ML_DAILY (snowflake/ml_forecast_option.sql) only",
    },
    "mart27_sql": {
        "ai_code_user_rollup": "reverted with its tab; locked out by test_v041_loader_pass",
        "ai_code_user_daily": _TWIN,
        "ai_code_daily": _TWIN,
        "compare_pattern_costs_by_warehouse": _TWIN,
    },
}


def _public_builders(module: str) -> list[str]:
    tree = ast.parse((_ROOT / "app" / "data" / f"{module}.py").read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]


@pytest.mark.parametrize("module", sorted(CANARY_EXEMPT))
def test_every_public_builder_has_a_canary_or_a_named_exemption(module):
    builders = _public_builders(module)
    uncanaried = {b for b in builders if not re.search(rf"\b{module}\.{b}\b", _CANARY_SRC)}
    exempt = CANARY_EXEMPT[module]
    assert all(reason.strip() for reason in exempt.values())
    assert not (uncanaried - set(exempt)), f"{module}: register a canary (or a reasoned exemption) for " \
                                           f"{sorted(uncanaried - set(exempt))}"
    assert not (set(exempt) - uncanaried), f"{module}: drop the now-canaried or removed names " \
                                           f"{sorted(set(exempt) - uncanaried)} from CANARY_EXEMPT"


_ORG = ("cost.org_all_in_window_usd", "cost.org_usage_in_currency", "cost.org_remaining_balance",
        "cost.org_contract_items", "cost.org_account_month_usd", "cost.org_rate_sheet")
_CORE = {
    "mart.snoozed_alert_events": lambda: mart_sql.snoozed_alert_events(1, "ALFA"),
    "mart.table_storage_waste_mart": lambda: mart_sql.table_storage_waste_mart("ALFA"),
    "mart.table_storage_breakdown_mart": lambda: mart_sql.table_storage_breakdown_mart("ALFA"),
    "mart.email_alert_history": lambda: mart_sql.email_alert_history(1),
    "mart27.task_nodes": lambda: mart27_sql.task_nodes(2, "ALFA"),
    "mart.cs_by_query_type_mart": lambda: mart_sql.cs_by_query_type_mart(1, "ALFA"),
}


def test_org_usage_readers_are_declared_gap_canaries():
    """R2-062: registered, reading ORGANIZATION_USAGE, and declared gaps (a missing org-viewer grant is an
    account-feature state); every one parses."""
    reg = dict(canary.CANARIES)
    for name in _ORG:
        assert name in reg, name
        sql = reg[name]()
        assert "SNOWFLAKE.ORGANIZATION_USAGE." in sql, name
        assert name in canary.EXPECTED_GAPS, name
        sqlglot.parse_one(sql, read="snowflake")
    assert reg["cost.org_all_in_window_usd"]() == cost_sql.org_all_in_window_usd(2)
    assert reg["cost.org_remaining_balance"]() == cost_sql.org_remaining_balance(2)


@pytest.mark.parametrize("name", sorted(_CORE))
def test_core_readers_are_canaried_and_fail_on_absence(name):
    """R2-064 / R2-068: core OVERWATCH objects (or an always-present INFORMATION_SCHEMA table function), so an
    absence must FAIL -- never a declared gap."""
    reg = dict(canary.CANARIES)
    assert name in reg
    assert reg[name]() == _CORE[name]()
    assert name not in canary.EXPECTED_GAPS
    sqlglot.parse_one(reg[name](), read="snowflake")


def test_the_new_canaries_compile_the_columns_that_had_none():
    sqls = "\n".join(b() for n, b in canary.CANARIES)
    for col in ("SNOOZED_UNTIL", "SNOOZE_REASON", "FAILSAFE_BYTES", "RETAINED_FOR_CLONE_BYTES", "LAST_DML",
                "ALERT_HISTORY(", "P95_QUEUE_SEC", "FIRST_START", "LAST_COMPLETED", "REMAINING_BALANCE_DAILY",
                "USAGE_IN_CURRENCY_DAILY", "RATE_SHEET_DAILY", "CONTRACT_ITEMS"):
        assert col in sqls, col
