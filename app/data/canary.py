"""Canary registry: every SQL builder with safe default arguments.

The Admin canary runs each statement with LIMIT 1 semantics to detect
ACCOUNT_USAGE column drift or object loss before a user hits it. Pure module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from app.data import (
    app_cost_sql,
    change_impact_sql,
    chargeback_sql,
    chatter_sql,
    cortex_sql,
    cost_sql,
    etl_sql,
    graph_sql,
    insights_sql,
    mart27_sql,
    mart_sql,
    ops_sql,
    prefs_sql,
    recheck_sql,
    security_sql,
    workbench_sql,
)
from app.logic.formulas import account_today


def _recent_release_iso() -> str:
    """Canary anchor for the release-compare builders. A fixed old date made
    the canary scan half a year of TASK/QUERY_HISTORY (153s in production
    telemetry); anchoring 3 days back keeps the scan inside warm partitions."""
    return (account_today() - timedelta(days=3)).isoformat()


def _recent_pair() -> tuple[str, str, str, str]:
    """Recent contiguous week-vs-week windows for the compare canaries —
    same recency rule as the release anchor (fixed dates are forbidden
    here, and recent windows exercise warm fact partitions)."""
    a1 = account_today() - timedelta(days=1)
    a0, b0 = a1 - timedelta(days=7), a1 - timedelta(days=14)
    return a0.isoformat(), a1.isoformat(), b0.isoformat(), a0.isoformat()


CANARIES: tuple[tuple[str, Callable[[], str]], ...] = (
    ("app_cost.mart", lambda: app_cost_sql.app_cost_mart(2)),
    ("app_cost.live", lambda: app_cost_sql.app_cost_live(2, "ALFA")),
    ("cost.metering_daily_by_service", lambda: cost_sql.metering_daily_by_service(2)),
    ("cost.warehouse_daily_credits", lambda: cost_sql.warehouse_daily_credits(2, "ALFA")),
    ("cost.warehouse_window_vs_prior", lambda: cost_sql.warehouse_window_vs_prior(2, "ALFA")),
    ("cost.allocated_attribution.user", lambda: cost_sql.allocated_attribution(2, "USER_NAME", "ALFA")),
    ("cost.allocated_attribution.db", lambda: cost_sql.allocated_attribution(2, "DATABASE_NAME", "ALFA")),
    ("cost.cortex_daily_spend", lambda: cost_sql.cortex_daily_spend(2)),
    ("cost.replication_by_database", lambda: cost_sql.replication_by_database(2, "ALFA")),
    ("cost.transfer_egress_priced", lambda: cost_sql.transfer_egress_priced(2)),
    ("cost.compute_pool_usage", lambda: cost_sql.compute_pool_usage(2)),
    ("cost.notebook_container_usage", lambda: cost_sql.notebook_container_usage(2)),
    ("cost.storage_account_truth", lambda: cost_sql.storage_account_truth(2)),
    ("cost.storage_account_truth_live", lambda: cost_sql.storage_account_truth_live(2)),
    ("cost.storage_by_database_calendar", lambda: cost_sql.storage_by_database_calendar("ALFA")),
    ("cost.storage_by_database_calendar_live", lambda: cost_sql.storage_by_database_calendar_live("ALFA")),
    ("cost.object_cost_by_arm", lambda: cost_sql.object_cost_by_arm(2, "ALFA")),
    ("cost.object_cost_recon", lambda: cost_sql.object_cost_recon(2)),
    ("cost.object_cost_top", lambda: cost_sql.object_cost_top(2, "ALFA")),
    # R2-062 (v4.608): the ORGANIZATION_USAGE readers -- the All-in invoice tile, the org balance behind every
    # runway, the org spend + rate-card reconciliation and the contract items -- had no canary, and several read
    # probe=True, so a renamed column was neither logged nor caught here. Small windows keep EXPLAIN cheap. Each
    # is declared in EXPECTED_GAPS below: ORGANIZATION_USAGE needs the org-viewer grant (RUNBOOK: "ORGANIZATION_
    # USAGE not granted"), so only its ABSENCE reads GAP; a missing column or a privilege error FAILs.
    ("cost.org_all_in_window_usd", lambda: cost_sql.org_all_in_window_usd(2)),
    ("cost.org_usage_in_currency", lambda: cost_sql.org_usage_in_currency(2)),
    ("cost.org_remaining_balance", lambda: cost_sql.org_remaining_balance(2)),
    ("cost.org_contract_items", cost_sql.org_contract_items),
    ("cost.org_account_month_usd", lambda: cost_sql.org_account_month_usd(1)),
    ("cost.org_rate_sheet", cost_sql.org_rate_sheet),
    # Next-Fifty #30: the unread-maintenance shortlist (mart) and its booked proof query. The ACCESS_HISTORY
    # confirm (insights_sql.object_reads_confirm) is not registered itself: the ACCESS_HISTORY columns it reads
    # (BASE_OBJECTS_ACCESSED, QUERY_START_TIME) FAIL on drift through the security.* ACCESS_HISTORY canaries below.
    ("cost.maintenance_on_unread", lambda: cost_sql.maintenance_on_unread(7, "ALFA")),
    ("cost.unread_maintenance_proof",
     lambda: cost_sql.unread_maintenance_proof("DB.S.T", account_today() - timedelta(days=15), 1.0)),
    ("etl.etl_cost_by_pipeline", lambda: etl_sql.etl_cost_by_pipeline(2, "ALFA")),
    ("etl.etl_tag_coverage", lambda: etl_sql.etl_tag_coverage(2, "ALFA")),
    ("ops.query_window_summary", lambda: ops_sql.query_window_summary(1, "ALFA")),
    ("ops.top_queries_by_elapsed", lambda: ops_sql.top_queries_by_elapsed(1, "ALFA", 1)),
    ("ops.failures_by_error", lambda: ops_sql.failures_by_error(1, "ALFA")),
    ("ops.task_runs", lambda: ops_sql.task_runs(1, "ALFA")),
    # #26 Task health run summary (INFORMATION_SCHEMA). Its SHOW TASKS twin is not a canary: the tab
    # EXPLAINs every entry, and SHOW cannot be EXPLAINed.
    ("ops.overwatch_task_run_summary", lambda: ops_sql.overwatch_task_run_summary(24)),
    ("ops.warehouse_pressure", lambda: ops_sql.warehouse_pressure(1, "ALFA")),
    # Next-Fifty #33: the statement-timeout runtime tail. Its two SHOW PARAMETERS twins are not
    # canaries (SHOW cannot be EXPLAINed).
    # v4.610.0: nor is access_sql.show_grants_of_role_sql, the SHOW GRANTS OF ROLE admin-access lookup
    # session.viewer_access runs once per viewer session (a failure is logged to APP_ERROR_LOG and shown in
    # the sidebar instead); tests/test_canary_coverage.py names the exemption. Likewise
    # access_sql.show_grants_on_app_sql, Admin ▸ App access's read-only SHOW GRANTS ON STREAMLIT (a failure or an
    # empty answer renders there as unavailable).
    ("ops.warehouse_timeout_tail", lambda: ops_sql.warehouse_timeout_tail(1, "ALFA")),
    # v4.603 (#33 D1): the alert drawer's one-warehouse impact read for the 'Statement timeout 1h' lever.
    ("ops.warehouse_timeout_impact", lambda: ops_sql.warehouse_timeout_impact("WH_ALFA_ADMIN", 3600, 1)),
    ("ops.lock_contention", lambda: ops_sql.lock_contention(1)),
    ("security.users_without_mfa", lambda: security_sql.users_without_mfa("ALFA")),
    ("security.users_without_mfa_live", lambda: security_sql.users_without_mfa_live("ALFA")),
    ("security.new_network_logins", lambda: security_sql.new_network_logins(7)),
    ("security.unload_activity", lambda: security_sql.unload_activity(30, "ALFA")),
    ("security.unload_risk_events", lambda: security_sql.unload_risk_events(30, "ALFA")),
    ("security.failed_logins", lambda: security_sql.failed_logins(1, "ALFA")),
    ("security.single_factor_logins", lambda: security_sql.single_factor_logins(30, "ALFA")),
    ("security.recent_role_grants", lambda: security_sql.recent_role_grants(1)),
    ("security.admin_role_holders", security_sql.admin_role_holders),
    ("security.user_auth_inventory", lambda: security_sql.user_auth_inventory("ALFA")),
    ("security.service_users", security_sql.service_users),
    ("security.recent_ddl_changes", lambda: security_sql.recent_ddl_changes(1, "ALFA")),
    ("security.expiring_credentials", lambda: security_sql.expiring_credentials(10, "ALFA")),
    ("security.client_drivers", lambda: security_sql.client_drivers(30, "ALFA")),
    # Next-Fifty #34: Snowflake's own driver support floor (a metadata call, no ACCOUNT_USAGE scan).
    ("security.client_version_info", security_sql.client_version_info),
    ("security.exception_queue", lambda: security_sql.security_exception_queue("ALFA", 1)),
    ("security.domain_coverage", security_sql.security_domain_coverage),
    ("security.trust_center_delta", security_sql.trust_center_delta),
    ("security.login_fact_coverage", lambda: security_sql.login_fact_coverage(1)),
    ("security.security_login_fact_coverage", lambda: security_sql.security_login_fact_coverage(1)),
    ("security.security_change_fact_coverage", lambda: security_sql.security_change_fact_coverage(1)),
    # holistic #7 (v4.608): the change-risk noise diagnostic, a probe=True read and the only reader of
    # FACT_SECURITY_CHANGE.CHANGE_KIND, so a renamed column was neither logged nor caught here. A core fact
    # (V075): absence FAILs (no gap).
    ("security.change_risk_destructive_breakdown", lambda: security_sql.change_risk_destructive_breakdown(1)),
    ("security.failed_logins_fact", lambda: security_sql.failed_logins_fact(1, "ALFA")),
    ("security.failed_login_reasons_fact", lambda: security_sql.failed_login_reasons_fact(1, "ALFA")),
    ("security.new_network_logins_fact", lambda: security_sql.new_network_logins_fact(1)),
    ("security.recent_ddl_changes_fact", lambda: security_sql.recent_ddl_changes_fact(1, "ALFA")),
    ("security.recent_ddl_changes_rollup", lambda: security_sql.recent_ddl_changes_rollup(1, "ALFA")),
    ("security.recent_ddl_changes_rollup_fact", lambda: security_sql.recent_ddl_changes_rollup_fact(1, "ALFA")),
    ("security.effective_access", lambda: security_sql.effective_access("ALFA")),
    ("security.egress_baseline", lambda: security_sql.egress_baseline(1)),
    # Next-Fifty #43: the POLICY_REFERENCES reads (probe=True on Security). A standard view, proven readable (S1b
    # 2026-09-29), so none is a declared gap: an absent view or a renamed column FAILs here while the page shows
    # needs_setup / unavailable.
    ("security.data_policy_coverage", security_sql.data_policy_coverage),
    ("security.masking_environment_parity", security_sql.masking_environment_parity),
    ("security.admin_network_policy_coverage", lambda: security_sql.admin_network_policy_coverage("ALFA")),
    # R2-069: the ACCESS_HISTORY readers, one per distinct column set, as plain FAILs (not EXPECTED_GAPS).
    # This account is Enterprise (RUNBOOK), so an absent view means a lost IMPORTED PRIVILEGES grant and a
    # missing column is drift. Three of these run probe=True, which leaves a missing column unlogged, so this
    # is their only drift record. Between them they cover every ACCESS_HISTORY column the app reads:
    # QUERY_START_TIME, BASE_OBJECTS_ACCESSED, OBJECTS_MODIFIED (+ the GRANTS_TO_ROLES / TABLE_STORAGE_METRICS
    # bridge), DIRECT_OBJECTS_ACCESSED, USER_NAME and QUERY_ID (+ the ENTITY_CATALOG join).
    # tests/test_security_e1_fixes.py finds every reader in app/ and the columns each one reads, and fails on
    # one no canary below reads.
    ("security.access_evidence_days", security_sql.access_evidence_days),
    ("security.grant_scope_usage", lambda: security_sql.grant_scope_usage(1, 1)),
    ("security.unused_table_grants", lambda: security_sql.unused_table_grants(1, 1)),
    ("graph.object_blast_consumers", lambda: graph_sql.object_blast_consumers(("DB.S.T",), 1)),
    ("workbench.product_consumer_reads", lambda: workbench_sql.product_consumer_reads(1, "ALFA")),
    ("change_impact.change_registry", lambda: change_impact_sql.change_registry(30, "ALFA")),
    ("mart.fact_metering_by_service", lambda: mart_sql.fact_metering_by_service(7)),
    ("mart.fact_query_window_summary", lambda: mart_sql.fact_query_window_summary(1, "ALFA")),
    # v4.608 holistic #10: Overview's score (and Control Room's Pulse, sharing its cache entry) ask for
    # the read clock (WIN_START_AT / READ_AT, and READ_ELAPSED_SEC: the real seconds between them, via
    # CONVERT_TIMEZONE('UTC', ...) so a DST change day counts its hour)
    ("mart.fact_query_window_summary.read_clock",
     lambda: mart_sql.fact_query_window_summary(1, "ALFA", read_clock=True)),
    ("mart.app_statement_stats", lambda: mart_sql.app_statement_stats(1)),
    ("mart.cloud_svc_top_shapes", lambda: mart_sql.cloud_svc_top_shapes(1, "ALFA")),
    ("mart.cloud_svc_by_user", lambda: mart_sql.cloud_svc_by_user(1, "ALFA")),
    ("mart.cloud_svc_billed_families", lambda: mart_sql.cloud_svc_billed_families(1, "ALFA")),
    # R2-012 (v4.608): the mart twin of cost.cs_by_query_type now wraps the shared projection to add COVERED_DAYS
    ("mart.cs_by_query_type_mart", lambda: mart_sql.cs_by_query_type_mart(1, "ALFA")),
    ("cost.cloud_services_ratio", lambda: cost_sql.cloud_services_ratio_by_warehouse(1, "ALFA")),
    ("cost.compile_heavy_families", lambda: cost_sql.compile_heavy_families(1, "ALFA")),
    ("ops.poor_pruning_queries", lambda: ops_sql.poor_pruning_queries(1, "ALFA")),
    ("ops.proc_sla_rollup", lambda: ops_sql.proc_sla_rollup(1, "ALFA")),
    ("ops.proc_regression", lambda: ops_sql.proc_regression(1, "ALFA")),
    ("ops.result_cache_daily", lambda: ops_sql.result_cache_daily(1, "ALFA")),
    ("ops.warehouse_concurrency_peaks", lambda: ops_sql.warehouse_concurrency_peaks(1, "ALFA")),
    ("ops.copy_load_failures", lambda: ops_sql.copy_load_failures(1, "ALFA")),
    ("security.failed_login_reasons", lambda: security_sql.failed_login_reasons(1, "ALFA")),
    ("security.admin_role_activity", lambda: security_sql.admin_role_activity(1)),
    ("insights.storage_waste", lambda: insights_sql.storage_waste("ALFA")),
    ("insights.wasted_query_spend_usd", lambda: insights_sql.wasted_query_spend_usd(2, "ALFA")),
    ("insights.warehouse_hourly_activity", lambda: insights_sql.warehouse_hourly_activity(1, "ALFA")),
    ("ops.dynamic_table_health", lambda: ops_sql.dynamic_table_health(1)),
    ("mart.alert_routes", lambda: mart_sql.alert_routes()),
    ("mart.remediation_log", lambda: mart_sql.remediation_log(1)),
    ("prefs.user_prefs", lambda: prefs_sql.user_prefs()),
    ("mart.incident_timeline", lambda: mart_sql.incident_timeline(1, "ALFA")),
    ("mart.fact_daily_activity", lambda: mart_sql.fact_daily_activity(1)),
    ("ops.task_graph_roots", lambda: ops_sql.task_graph_roots()),
    ("ops.task_graph_nodes", lambda: ops_sql.task_graph_nodes()),
    ("ops.task_graph_recent_runs", lambda: ops_sql.task_graph_recent_runs()),
    ("ops.task_graph_run_nodes", lambda: ops_sql.task_graph_run_nodes()),
    ("ops.task_graph_versions", lambda: ops_sql.task_graph_versions()),
    ("ops.task_graph_version_nodes", lambda: ops_sql.task_graph_version_nodes()),
    # v4.608 R2-063 / R2-065 / R2-066 / R2-067: Operations / ETL readers that had no canary. The first four
    # ops entries and query_insights_feed / object_dependency_edges are probe=True reads, which log neither an
    # absent object nor a missing column, so a drifted column failed every render with no APP_ERROR_LOG row
    # while this registry stayed green. The operator boards pass the identity filters so the V147
    # USER_NAME / DATABASE_NAME / SCHEMA_NAME grain compiles too; query_detail takes a VALID id (a bad one
    # raises) and a recent start hint so its scan stays bounded. Every one reads a core object or a standard
    # view (absence FAILs) except insights.query_insights_feed: QUERY_INSIGHTS is an optional view (newer
    # accounts/editions), declared in EXPECTED_GAPS so only its absence reads GAP and a missing column FAILs.
    ("ops.operator_stats_summary", lambda: ops_sql.operator_stats_summary(
        1, "ALFA", "WH", user_contains="X", database="DBA_MAINT_DB", schema_contains="X")),
    ("ops.operator_problem_board", lambda: ops_sql.operator_problem_board(
        1, "ALFA", "WH", user_contains="X", database="DBA_MAINT_DB", schema_contains="X")),
    ("ops.operator_anatomy", lambda: ops_sql.operator_anatomy("canary-probe")),
    ("ops.table_pruning_candidates", lambda: ops_sql.table_pruning_candidates(1, "ALFA")),
    ("ops.query_opportunity_fingerprints", lambda: ops_sql.query_opportunity_fingerprints(1, "ALFA")),
    ("ops.running_queries", lambda: ops_sql.running_queries("WH_ALFA_ADMIN")),
    ("insights.query_insights_feed", lambda: insights_sql.query_insights_feed(1)),
    ("insights.table_retention_live",
     lambda: insights_sql.table_retention_live("DBA_MAINT_DB", "OVERWATCH", "SETTINGS")),
    ("insights.table_storage_breakdown", lambda: insights_sql.table_storage_breakdown("ALFA")),
    ("insights.query_detail", lambda: insights_sql.query_detail(
        "00000000-0000-0000-0000-000000000000", (account_today() - timedelta(days=1)).isoformat())),
    ("graph.object_dependency_edges", lambda: graph_sql.object_dependency_edges(1)),
    ("chatter.by_application", lambda: chatter_sql.chatter_by_application(1)),
    ("chatter.families_for_application", lambda: chatter_sql.chatter_families_for_application("canary-probe", 1)),
    ("ops.volume_deltas", lambda: ops_sql.volume_deltas()),
    ("mart.dept_budgets", lambda: mart_sql.dept_budgets()),
    ("mart.app_usage_summary", lambda: mart_sql.app_usage_summary(1)),
    ("mart.section_visit_summary", lambda: mart_sql.section_visit_summary(1)),
    ("mart.ask_demand_summary", lambda: mart_sql.ask_demand_summary(1)),
    ("mart.app_performance_slo", lambda: mart_sql.app_performance_slo(1)),
    ("mart.contract_exhaustion", lambda: mart_sql.contract_exhaustion()),
    ("mart.savings_summary_quarter", lambda: mart_sql.savings_summary_quarter()),
    ("mart.app_cost_last_30d", lambda: mart_sql.app_cost_last_30d()),
    ("security.unused_roles", lambda: security_sql.unused_roles(1)),
    ("security.role_privilege_matrix", lambda: security_sql.role_privilege_matrix()),
    ("insights.anomaly_evidence", lambda: insights_sql.anomaly_evidence(
        account_today().isoformat())),
    ("insights.idle_warehouse_analysis", lambda: insights_sql.idle_warehouse_analysis(1, "ALFA")),
    ("insights.repeat_query_fingerprints", lambda: insights_sql.repeat_query_fingerprints(1, "ALFA", 2)),
    ("insights.storage_growth_by_database", lambda: insights_sql.storage_growth_by_database(2, "ALFA")),
    ("insights.release_query_compare", lambda: insights_sql.release_query_compare(_recent_release_iso(), 1)),
    ("insights.release_task_compare", lambda: insights_sql.release_task_compare(_recent_release_iso(), 1)),
    ("insights.task_failure_details", lambda: insights_sql.task_failure_details(1, "ALFA")),
    ("insights.dormant_users", lambda: insights_sql.dormant_users(30, "ALFA")),
    ("insights.warehouse_sizing_profile", lambda: insights_sql.warehouse_sizing_profile(1, "ALFA")),
    # Next-Fifty #38 cluster-cap check. NOT an expected gap: CLUSTER_NUMBER is a standard QUERY_HISTORY
    # column (proven on this account by probe W1c), so a missing column must FAIL the canary.
    ("insights.warehouse_cluster_use", lambda: insights_sql.warehouse_cluster_use(("WH_ALFA_ADMIN",), 1)),
    ("insights.measured_query_costs", lambda: insights_sql.measured_query_costs(1, "ALFA")),
    ("insights.procedure_costs_usd", lambda: insights_sql.procedure_costs_usd(1, "ALFA")),
    ("graph.graph_daily_costs", lambda: graph_sql.graph_daily_costs(1, "ALFA")),
    ("graph.serverless_task_daily", lambda: graph_sql.serverless_task_daily(1, "ALFA")),
    ("change_impact.wh_registry", lambda: change_impact_sql.warehouse_change_registry(1)),
    ("change_impact.wh_series", lambda: change_impact_sql.warehouse_daily_series("WH_ALFA_ADMIN", 1)),
    ("insights.pipeline_sla_status", insights_sql.pipeline_sla_status),
    ("insights.pipeline_sla_config", insights_sql.pipeline_sla_config),
    ("cortex.code_user_rollup", lambda: cortex_sql.cortex_code_user_rollup(1, "ALFA")),
    ("cortex.code_daily", lambda: cortex_sql.cortex_code_daily(1, "ALFA")),
    ("cortex.ai_functions_daily", lambda: cortex_sql.cortex_ai_functions_daily(1)),
    ("cortex.model_costs", lambda: cortex_sql.cortex_model_costs(1)),
    ("cortex.source_costs", lambda: cortex_sql.cortex_source_costs(1)),
    # v4.603: the probe=True Cortex reads that had no canary. A probe read logs neither an absent object NOR a
    # missing column (query.run's expected-absence set), so a drifted column failed silently on every render
    # (the v4.601.1 QUOTA_ACCESS_BLOCK_HISTORY CREATED_ON class). Each is declared in EXPECTED_GAPS below, so
    # only a true absence reads GAP; an invalid identifier FAILs. Compile-only (the default) is cheap; the
    # executed probe of code_token_types pays the same window-flat ~22s secure-view scan as code_user_rollup
    # (the builder has no window knob by design). On SiS no per-tier read timeout applies (ALTER SESSION is
    # rejected and only the cortex tier rides statement_params: core.session), so that scan runs up to the
    # app warehouse's STATEMENT_TIMEOUT_IN_SECONDS (read live on Admin > Performance); the live tier's 30s
    # applies only off SiS (local dev), where a timeout in execute mode is load, not drift. Compile-only is
    # the drift check. code_token_types also FAILs (not GAPs) on an account whose Cortex Code views predate the
    # optional TOKENS_GRANULAR column: kept, because a GAP would hide a renamed or dropped column; the Admin
    # canary panel names that one expected FAIL.
    ("cortex.guardrails_daily", lambda: cortex_sql.guardrails_daily(1)),
    ("cortex.code_token_types", cortex_sql.cortex_code_token_types),
    ("cortex.quota_access_block_history", lambda: cortex_sql.quota_access_block_history(1)),
    ("cortex.app_self_cost", lambda: mart_sql.app_cortex_self_cost(1)),
    ("mart.exec_board", lambda: mart_sql.exec_board("ALFA", 7)),
    ("mart.source_freshness", mart_sql.source_freshness),
    ("mart.source_freshness_state", mart_sql.source_freshness_state),
    ("mart.fact_contract_consumed", lambda: mart_sql.fact_contract_consumed(
        (account_today() - timedelta(days=30)).isoformat())),
    ("mart.fact_daily_spend", lambda: mart_sql.fact_daily_spend(2)),
    ("mart.fact_daily_spend_compute", lambda: mart_sql.fact_daily_spend_compute(2)),
    ("mart.fact_warehouse_daily", lambda: mart_sql.fact_warehouse_daily(2, "ALFA")),
    ("mart.fact_task_daily", lambda: mart_sql.fact_task_daily(2, "ALFA")),
    ("mart.fact_wh_window_vs_prior", lambda: mart_sql.fact_warehouse_window_vs_prior(2, "ALFA")),
    ("mart.fact_warehouse_pressure", lambda: mart_sql.fact_warehouse_pressure(1, "ALFA")),
    ("mart.warehouse_capacity_daily", lambda: mart_sql.warehouse_capacity_daily(30, "ALFA")),
    ("mart.fact_cloud_services_ratio", lambda: mart_sql.fact_cloud_services_ratio(2, "ALFA")),
    # R2-064 (v4.608): the only readers of these columns had no canary. ALERT_EVENTS' V086 SNOOZE_* columns
    # (a probe=True read on Alerts), MART_TABLE_STORAGE_DAILY's ACTIVE / FAILSAFE / CLONE bytes, LAST_DML and
    # COMPANY, and the INFORMATION_SCHEMA.ALERT_HISTORY table function behind the email-path verdict (it always
    # exists; an uninstalled email alert only filters to zero rows). Core objects: absence FAILs (no gap).
    ("mart.snoozed_alert_events", lambda: mart_sql.snoozed_alert_events(1, "ALFA")),
    ("mart.table_storage_waste_mart", lambda: mart_sql.table_storage_waste_mart("ALFA")),
    ("mart.table_storage_breakdown_mart", lambda: mart_sql.table_storage_breakdown_mart("ALFA")),
    ("mart.email_alert_history", lambda: mart_sql.email_alert_history(1)),
    ("mart27.task_graphs", lambda: mart27_sql.task_graphs(2)),
    # R2-068 (v4.608): the only reader of MART_TASK_NODE_DAILY's queue / exec timing columns (AVG/P95/MAX
    # QUEUE_SEC, AVG/MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED) -- mart-only, no live fallback. A core mart:
    # absence FAILs.
    ("mart27.task_nodes", lambda: mart27_sql.task_nodes(2, "ALFA")),
    ("mart27.security_posture", lambda: mart27_sql.security_posture(7)),
    ("mart27.incident_timeline", lambda: mart27_sql.incident_timeline(24, "ALFA")),
    ("mart27.eff_idle_analysis", lambda: mart27_sql.eff_idle_analysis(2, "ALFA")),
    ("mart27.eff_sizing_profile", lambda: mart27_sql.eff_sizing_profile(2, "ALFA")),
    ("mart27.family_compile_heavy", lambda: mart27_sql.family_compile_heavy(2, "ALFA")),
    ("mart27.role_share", lambda: mart27_sql.role_share(1, "ALFA")),
    ("mart27.schema_window_summary", lambda: mart27_sql.schema_window_summary(1, "ALFA")),
    ("mart27.ai_costs_by_model", lambda: mart27_sql.ai_costs_by_model(2)),
    # holistic #17 (v4.608): the only canary that compiles FACT_AI_USAGE_DAILY's V042 EMAIL / FIRST_TS / LAST_TS,
    # read fact-first by the Security AI-guardrails tab (the Cost AI-users fact fallback reads the same three).
    # A core mart: absence FAILs (no gap). Days-independent (365d), so it renders with its company only.
    ("mart27.ai_code_user_daily", lambda: mart27_sql.ai_code_user_daily("ALFA")),
    ("mart27.unused_roles_via_fact", lambda: mart27_sql.unused_roles_via_fact(90)),
    ("mart27.tag_coverage_daily", lambda: mart27_sql.tag_coverage_daily(2, "ALFA")),
    ("mart27.lock_wait_daily", lambda: mart27_sql.lock_wait_daily(2, "ALFA")),
    ("mart27.lock_wait_spikes", lambda: mart27_sql.lock_wait_spikes("ALL")),
    ("mart27.monthly_spend_by_warehouse", lambda: mart27_sql.monthly_spend_by_warehouse(2, "ALFA")),
    ("mart27.pattern_cost", lambda: mart27_sql.pattern_cost(2, "ALFA", 5)),
    # R1-016 / PATTERN-RESTAMP (V167): SOURCE_FRESHNESS_STATE.COVERAGE_FROM, the loader-written loaded-from
    # watermark the AI coverage gate and the pattern cap read behind has_migration(167). The column exists only
    # once V167 is applied and the app deploys first, so the Admin runner SKIPS this entry until then
    # (MIGRATION_GATED below) -- a missing column is drift (missing_column), never a declared gap, so
    # EXPECTED_GAPS cannot cover the deploy-to-apply window. Core table: after V167 an absence FAILs.
    ("mart27.fact_coverage_from", mart27_sql.fact_coverage_from),
    # R1-016 (V167): the AI fact's gate reaches (the stamped coverage CTEs) + its last both-arm load day; reads the
    # same V167 column, so the runner skips it until has_migration(167) too.
    ("mart27.ai_fact_coverage", mart27_sql.ai_fact_coverage),
    ("insights.call_cost_lookup", lambda: insights_sql.call_cost_lookup("canary-probe", 1)),
    ("insights.call_children_costs", lambda: insights_sql.call_children_costs("canary-probe", 1)),
    ("insights.proc_cost_trend", lambda: insights_sql.proc_cost_trend("CANARY_PROBE", 1, "ALFA")),
    ("riders.delivery_slo_summary", lambda: mart_sql.delivery_slo_summary(7)),
    ("riders.delivery_by_route", lambda: mart_sql.delivery_by_route(7)),
    ("riders.deliveries_for_event", lambda: mart_sql.deliveries_for_event("evt")),
    ("riders.route_backlog", lambda: mart_sql.route_backlog()),
    # V164 (#40): reads ALERT_AUDIT / APP_ERROR_LOG / SETTINGS only, so it is green before and after the apply
    ("riders.escalation_summary", lambda: mart_sql.escalation_summary(7)),
    ("riders.alert_fatigue", lambda: mart_sql.alert_fatigue(7)),
    ("riders.acceptance_funnel", lambda: mart_sql.acceptance_funnel(7)),
    ("riders.telemetry_by_page", lambda: mart_sql.telemetry_by_page(1)),
    ("riders.usage_event_summary", lambda: mart_sql.usage_event_summary(1)),
    ("incidents.open_incidents", lambda: mart_sql.open_incidents(5)),
    ("incidents.members_detail", lambda: mart_sql.incident_members_detail("canary-probe")),
    ("incidents.proposals", lambda: mart_sql.incident_proposals(5)),
    ("incidents.metrics", lambda: mart_sql.incident_metrics(7)),
    ("mart.open_alert_events", lambda: mart_sql.open_alert_events(1)),
    # holistic #18 (v4.608): the Alerts drawer's 'How this was resolved before' read, the only reader of
    # ALERT_AUDIT.EVENT_ID / NOTE. Core objects: absence FAILs (no gap). The rule id must be an identifier.
    ("mart.resolutions_for_rule", lambda: mart_sql.resolutions_for_rule("CANARY_PROBE", 1)),
    ("mart.alert_event_history", lambda: mart_sql.alert_event_history(2)),
    ("mart.alert_mttr", lambda: mart_sql.alert_mttr(7)),
    ("mart.alert_rules", mart_sql.alert_rules),
    # v4.610.0: the Alerts > Rules 'Recent rule changes' read (ALERT_AUDIT RULE_EDIT rows; a core object)
    ("mart.alert_rule_edits", lambda: mart_sql.alert_rule_edits(1)),
    ("mart.action_queue", lambda: mart_sql.action_queue(1)),
    ("workbench.action_center", lambda: workbench_sql.action_center(limit=1)),
    ("workbench.action_activity", lambda: workbench_sql.action_activity("canary-probe", 1)),
    ("workbench.evidence_links", lambda: workbench_sql.evidence_links("ACTION", "canary-probe", 1)),
    ("workbench.entity_catalog", lambda: workbench_sql.entity_catalog(limit=1)),
    ("workbench.entity_record", lambda: workbench_sql.entity_record("WAREHOUSE", "WH_CANARY")),
    ("workbench.related_actions", lambda: workbench_sql.related_actions("WAREHOUSE", "WH_CANARY", 1)),
    ("workbench.related_remediations", lambda: workbench_sql.related_remediations("WH_CANARY", 1)),
    ("workbench.related_savings", lambda: workbench_sql.related_savings("WH_CANARY", 1)),
    ("workbench.watchlist", lambda: workbench_sql.watchlist("CANARY_VIEWER")),
    ("workbench.experiments", lambda: workbench_sql.experiments(limit=1)),
    ("workbench.entity_metric_snapshot", lambda: workbench_sql.entity_metric_snapshot()),
    ("workbench.workload_portfolio", lambda: workbench_sql.workload_portfolio()),
    ("workbench.optimize_queue", lambda: workbench_sql.optimize_queue()),
    ("workbench.tracked_actions", lambda: workbench_sql.tracked_actions()),
    ("workbench.tracked_entity_actions", lambda: workbench_sql.tracked_entity_actions()),
    ("workbench.entity_daily_signals", lambda: workbench_sql.entity_daily_signals(
        [("WAREHOUSE", "WH_CANARY", account_today() - timedelta(days=2)),
         ("TASK", "DB.SCH.T_CANARY", account_today() - timedelta(days=2)),
         ("QUERY_FINGERPRINT", "CANARYHASH", account_today() - timedelta(days=2))])),
    ("workbench.slo_cockpit", lambda: workbench_sql.slo_cockpit()),
    ("workbench.data_product_economics", lambda: workbench_sql.data_product_economics()),
    ("workbench.cost_truth", lambda: workbench_sql.cost_truth()),
    ("mart.savings_ledger", mart_sql.savings_ledger),
    ("mart.ledger_attribution", mart_sql.ledger_attribution),
    ("mart.settings", mart_sql.settings),
    ("mart.schema_version", mart_sql.schema_version),
    ("mart.unmapped_entities", lambda: mart_sql.unmapped_entities(7)),
    ("mart.app_error_log", lambda: mart_sql.app_error_log(1)),
    ("mart.app_self_cost", lambda: mart_sql.app_self_cost(1)),
    ("mart.latest_digest", mart_sql.latest_digest),
    ("mart.savings_verification_runs", mart_sql.savings_verification_runs),
    # Next-Fifty #46(d): the measured manual-verify before/after, one per basis (a recent booking day,
    # so the scan stays in warm partitions).
    ("mart.ledger_before_after.warehouse",
     lambda: mart_sql.ledger_before_after("WAREHOUSE", "WH_CANARY", account_today() - timedelta(days=15))),
    ("mart.ledger_before_after.object",
     lambda: mart_sql.ledger_before_after("OBJECT", "DB.SCH.T_CANARY", account_today() - timedelta(days=15))),
    ("mart.ledger_before_after.table",
     lambda: mart_sql.ledger_before_after("TABLE", "DB.SCH.T_CANARY", account_today() - timedelta(days=15))),
    ("chargeback.department_window", lambda: chargeback_sql.department_window_credits(1, "ALFA")),
    # #42 Part 1: every fact it reads is a core OVERWATCH table, so an absence is a real failure
    # (deliberately NOT in EXPECTED_GAPS).
    ("chargeback.company_allin_showback", lambda: chargeback_sql.company_allin_showback(2, "ALFA")),
    ("chargeback.role_share", lambda: chargeback_sql.role_share_within_warehouse(1, "ALFA")),
    ("chargeback.department_map", chargeback_sql.department_map),
    ("insights.expensive_queries_usd", lambda: insights_sql.expensive_queries_usd(1, "ALFA", 5)),
    ("mart.rule_precision", lambda: mart_sql.rule_precision(7)),
    ("mart.mart_vs_live_recon", mart_sql.mart_vs_live_recon),
    ("cortex.mart_vs_live_ai_recon", mart_sql.mart_vs_live_ai_recon),
    ("mart.fleet_query_stats", lambda: mart_sql.fleet_query_stats(2)),
    ("mart.rule_metric_kinds", lambda: mart_sql.rule_metric_kinds(7)),
    ("mart.score_inputs_daily", lambda: mart_sql.score_inputs_daily(7)),
    ("insights.expensive_patterns_usd", lambda: insights_sql.expensive_patterns_usd(1, "ALFA", 5)),
    ("recheck.wh_daily_credits", lambda: recheck_sql.recheck_sql("COST_WH_DAILY_CREDITS", "WH_ALFA_ADMIN") or ""),
    # review R1-040: the queued/spill re-checks now read FACT_QUERY_HOURLY's QUEUED_SEC_SUM / SPILL_REMOTE_GB
    ("recheck.queued_minutes", lambda: recheck_sql.recheck_sql("PERF_QUEUED_MINUTES", "WH_ALFA_ADMIN") or ""),
    ("recheck.spill_gb", lambda: recheck_sql.recheck_sql("PERF_SPILL_GB", "WH_ALFA_ADMIN") or ""),
    ("mart.day_spend_movers", lambda: mart_sql.day_spend_movers("2026-01-02")),
    ("mart.day_activity", lambda: mart_sql.day_activity("2026-01-02")),
    ("mart.day_task_failures", lambda: mart_sql.day_task_failures("2026-01-02")),
    ("mart.day_alerts", lambda: mart_sql.day_alerts("2026-01-02")),
    ("mart.drill_history", lambda: mart_sql.drill_history(3)),
    ("mart.metering_restatements", lambda: mart_sql.metering_restatements(14)),
    ("security.day_ddl", lambda: security_sql.day_ddl("2026-01-02")),
    ("security.day_grants", lambda: security_sql.day_grants("2026-01-02")),
    ("ops.warehouse_blast_radius", lambda: ops_sql.warehouse_blast_radius("WH_ALFA_ADMIN", 1)),
    ("cost.tag_coverage", lambda: cost_sql.tag_coverage(1, "ALFA")),
    ("mart27.compare_warehouse_credits", lambda: mart27_sql.compare_warehouse_credits(
        *_recent_pair(), "ALFA")),
    ("mart27.compare_activity", lambda: mart27_sql.compare_activity(
        *_recent_pair(), "ALFA")),
    ("mart27.compare_billed", lambda: mart27_sql.compare_billed(*_recent_pair())),
    ("mart27.compare_pattern_costs", lambda: mart27_sql.compare_pattern_costs(
        *_recent_pair(), "ALFA", 5)),
    ("cost.cs_by_query_type", lambda: cost_sql.cs_by_query_type(1, "ALFA")),
    ("insights.clustering_by_table", lambda: insights_sql.clustering_by_table(7, "ALFA")),
    ("mart.fact_daily_spend_year", mart_sql.fact_daily_spend_year),
    ("mart.fact_cortex_daily_spend", lambda: mart_sql.fact_cortex_daily_spend(7)),
    ("mart27.fact_monthly_spend_by_warehouse", lambda: mart27_sql.fact_monthly_spend_by_warehouse(12, "ALFA")),
    # V041 loader pass
    ("mart27.alloc_xdim_attribution", lambda: mart27_sql.alloc_xdim_attribution(1, "USER", "ALFA", "")),
    ("mart27.alloc_xdim_attribution.db", lambda: mart27_sql.alloc_xdim_attribution(1, "DATABASE", "ALFA", "DBA_MAINT_DB")),
    ("mart27.alloc_xdim_day_drivers", lambda: mart27_sql.alloc_xdim_day_drivers(
        "WH_ALFA_ADMIN", (account_today() - timedelta(days=1)).isoformat(), "ALFA")),
    ("mart27.ops_diag_top_queries", lambda: mart27_sql.ops_diag_top_queries(1, "ALFA", 1)),
    ("mart27.ops_diag_failures", lambda: mart27_sql.ops_diag_failures(1, "ALFA")),
    ("mart27.platform_score_inputs", lambda: mart27_sql.platform_score_inputs(7)),
)

# Entries that read a column or object a migration adds: the Admin canary runner skips each one until
# schema_gate.has_migration(<version>, page) answers True (the app deploys before the owner applies), then
# runs it like any other entry. canary.py stays pure -- the runner owns the gate (gated_out below).
MIGRATION_GATED: dict[str, int] = {
    "mart27.fact_coverage_from": 167,
    "mart27.ai_fact_coverage": 167,
}


def gated_out(name: str, applied: set[int] | frozenset[int]) -> bool:
    """True while the entry's migration is not applied yet (the runner skips it, it never FAILs then)."""
    version = MIGRATION_GATED.get(name)
    return version is not None and version not in applied


# r11 #7: names whose ABSENCE is an account-feature state, not drift — only
# these may report GAP. Anything else absent (a dropped core object, a
# mistyped function, a revoked grant) must FAIL loudly. Fresh deployments:
# missing-migration objects FAIL here by design — Admin > Migrations &
# freshness is the calm view for that state.
EXPECTED_GAPS: frozenset[str] = frozenset({
    # Cortex Code / AI usage views probe a subscription internally (002139
    # without one — Joe's live trace, 2026-07-10); model view needs the
    # feature enabled in-region.
    "cortex.code_user_rollup",
    "cortex.code_daily",
    "cortex.ai_functions_daily",
    "cortex.model_costs",
    "cortex.source_costs",
    "cortex.mart_vs_live_ai_recon",   # reads the subscription-gated CORTEX_CODE_* views
    # Next-Fifty #34: SYSTEM$CLIENT_VERSION_INFO() is an account-feature function (proven live on this
    # account, 2026-09-29 probe W4c). Only its ABSENCE (unknown function) reads GAP -- the Clients tab then
    # shows 'unavailable' support; any other failure (an "Insufficient privileges" grant error, a timeout)
    # still FAILS.
    "security.client_version_info",
    # v4.603 probe-read canaries: absence is an account-feature state (Guardrails / per-user quota views,
    # the Cortex Code subscription, the AI-functions view); a missing column is drift and FAILs.
    "cortex.guardrails_daily",
    "cortex.code_token_types",
    "cortex.quota_access_block_history",
    "cortex.app_self_cost",
    # v4.608 R2-065: ACCOUNT_USAGE.QUERY_INSIGHTS is an optional view (newer accounts/editions; the
    # Operations panel reads it probe=True and shows a calm note when it is absent). Only its absence reads
    # GAP; a renamed column (INSIGHT_TYPE_ID, MESSAGE) is drift and FAILs.
    "insights.query_insights_feed",
    # R2-062 (v4.608): ORGANIZATION_USAGE is visible only with the org-viewer grant (an account-feature state the
    # pages already degrade on), so its ABSENCE reads GAP; a renamed column or a privilege error still FAILs.
    "cost.org_all_in_window_usd",
    "cost.org_usage_in_currency",
    "cost.org_remaining_balance",
    "cost.org_contract_items",
    "cost.org_account_month_usd",
    "cost.org_rate_sheet",
})
