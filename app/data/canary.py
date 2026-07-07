"""Canary registry: every SQL builder with safe default arguments.

The Admin canary runs each statement with LIMIT 1 semantics to detect
ACCOUNT_USAGE column drift or object loss before a user hits it. Pure module.
"""

from __future__ import annotations

from collections.abc import Callable

from app.data import cortex_sql, cost_sql, insights_sql, mart_sql, ops_sql, security_sql

CANARIES: tuple[tuple[str, Callable[[], str]], ...] = (
    ("cost.metering_daily_by_service", lambda: cost_sql.metering_daily_by_service(2)),
    ("cost.warehouse_daily_credits", lambda: cost_sql.warehouse_daily_credits(2, "ALFA")),
    ("cost.warehouse_window_vs_prior", lambda: cost_sql.warehouse_window_vs_prior(2, "ALFA")),
    ("cost.allocated_attribution.user", lambda: cost_sql.allocated_attribution(2, "USER_NAME", "ALFA")),
    ("cost.allocated_attribution.db", lambda: cost_sql.allocated_attribution(2, "DATABASE_NAME", "ALFA")),
    ("cost.cortex_daily_spend", lambda: cost_sql.cortex_daily_spend(2)),
    ("cost.storage_by_database", lambda: cost_sql.storage_by_database(2, "ALFA")),
    ("ops.query_window_summary", lambda: ops_sql.query_window_summary(1, "ALFA")),
    ("ops.top_queries_by_elapsed", lambda: ops_sql.top_queries_by_elapsed(1, "ALFA", 1)),
    ("ops.failures_by_error", lambda: ops_sql.failures_by_error(1, "ALFA")),
    ("ops.task_runs", lambda: ops_sql.task_runs(1, "ALFA")),
    ("ops.warehouse_pressure", lambda: ops_sql.warehouse_pressure(1, "ALFA")),
    ("ops.lock_contention", lambda: ops_sql.lock_contention(1)),
    ("security.users_without_mfa", lambda: security_sql.users_without_mfa("ALFA")),
    ("security.failed_logins", lambda: security_sql.failed_logins(1, "ALFA")),
    ("security.recent_role_grants", lambda: security_sql.recent_role_grants(1)),
    ("security.admin_role_holders", security_sql.admin_role_holders),
    ("security.recent_ddl_changes", lambda: security_sql.recent_ddl_changes(1, "ALFA")),
    ("insights.idle_warehouse_analysis", lambda: insights_sql.idle_warehouse_analysis(1, "ALFA")),
    ("insights.repeat_query_fingerprints", lambda: insights_sql.repeat_query_fingerprints(1, "ALFA", 2)),
    ("insights.storage_growth_by_database", lambda: insights_sql.storage_growth_by_database(2, "ALFA")),
    ("insights.release_query_compare", lambda: insights_sql.release_query_compare("2026-01-01", 1)),
    ("insights.release_task_compare", lambda: insights_sql.release_task_compare("2026-01-01", 1)),
    ("insights.task_failure_details", lambda: insights_sql.task_failure_details(1, "ALFA")),
    ("insights.dormant_users", lambda: insights_sql.dormant_users(30, "ALFA")),
    ("insights.pipeline_sla_status", insights_sql.pipeline_sla_status),
    ("insights.pipeline_sla_config", insights_sql.pipeline_sla_config),
    ("cortex.code_user_rollup", lambda: cortex_sql.cortex_code_user_rollup(1, "ALFA")),
    ("cortex.code_daily", lambda: cortex_sql.cortex_code_daily(1, "ALFA")),
    ("cortex.ai_functions_daily", lambda: cortex_sql.cortex_ai_functions_daily(1)),
    ("mart.exec_board", lambda: mart_sql.exec_board("ALFA", 7)),
    ("mart.source_freshness", mart_sql.source_freshness),
    ("mart.fact_daily_spend", lambda: mart_sql.fact_daily_spend(2)),
    ("mart.fact_warehouse_daily", lambda: mart_sql.fact_warehouse_daily(2, "ALFA")),
    ("mart.fact_task_daily", lambda: mart_sql.fact_task_daily(2, "ALFA")),
    ("mart.open_alert_events", lambda: mart_sql.open_alert_events(1)),
    ("mart.alert_event_history", lambda: mart_sql.alert_event_history(2)),
    ("mart.alert_mttr", lambda: mart_sql.alert_mttr(7)),
    ("mart.alert_rules", mart_sql.alert_rules),
    ("mart.action_queue", lambda: mart_sql.action_queue(1)),
    ("mart.savings_ledger", mart_sql.savings_ledger),
    ("mart.settings", mart_sql.settings),
    ("mart.schema_version", mart_sql.schema_version),
    ("mart.app_error_log", lambda: mart_sql.app_error_log(1)),
    ("mart.app_self_cost", lambda: mart_sql.app_self_cost(1)),
)
