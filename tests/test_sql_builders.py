"""Contract tests for SQL builders.

Not snapshot tests — they assert the invariants that made the old app's SQL
review findings impossible to regress silently:
  1. every live ACCOUNT_USAGE scan is date-bounded,
  2. company scoping appears when a company is requested,
  3. detail queries carry an explicit LIMIT,
  4. billed credits always apply the cloud-services adjustment.
"""

import re

import pytest

from app.data import cost_sql, mart_sql, ops_sql, security_sql

LIVE_BUILDERS = [
    lambda: cost_sql.metering_daily_by_service(7),
    lambda: cost_sql.warehouse_daily_credits(7, "ALFA"),
    lambda: cost_sql.warehouse_window_vs_prior(7, "Trexis"),
    lambda: cost_sql.allocated_attribution(7, "USER_NAME", "ALFA"),
    lambda: cost_sql.cortex_daily_spend(7),
    lambda: cost_sql.storage_by_database(7, "ALFA"),
    lambda: ops_sql.query_window_summary(7, "ALFA"),
    lambda: ops_sql.top_queries_by_elapsed(7, "ALFA"),
    lambda: ops_sql.failures_by_error(7, "Trexis"),
    lambda: ops_sql.task_runs(7, "ALFA"),
    lambda: ops_sql.warehouse_pressure(7, "ALFA"),
    lambda: ops_sql.lock_contention(7),
    lambda: security_sql.failed_logins(7, "ALFA"),
    lambda: security_sql.recent_role_grants(7),
    lambda: security_sql.recent_ddl_changes(7, "ALFA"),
    lambda: mart_sql.app_self_cost(7),
]


@pytest.mark.parametrize("builder", LIVE_BUILDERS)
def test_every_live_scan_is_date_bounded(builder):
    sql = builder()
    assert re.search(r"DATEADD\('(day|hour)',\s*-\d+", sql), f"unbounded scan:\n{sql}"


def test_day_windows_are_clamped():
    sql = cost_sql.metering_daily_by_service(10_000)
    assert "-90," in sql.replace(" ", "")  # MAX_LIVE_WINDOW_DAYS
    sql = ops_sql.lock_contention(10_000)
    assert "-14," in sql.replace(" ", "")  # builder-specific tighter cap


def test_company_scope_present_when_requested():
    alfa = cost_sql.warehouse_daily_credits(7, "ALFA")
    trexis = cost_sql.warehouse_daily_credits(7, "Trexis")
    both = cost_sql.warehouse_daily_credits(7, "ALL")
    assert "NOT IN" in alfa and "WH_TRXS_LOAD" in alfa
    assert re.search(r"\bIN \('WH_TRXS_LOAD'", trexis)
    assert "WH_TRXS_LOAD' " not in both.split("CASE")[0]  # ALL: no filter before CASE label


def test_user_scope_carries_kebarr1_override():
    sql = cost_sql.allocated_attribution(7, "USER_NAME", "ALFA")
    assert "KEBARR1" in sql


def test_detail_queries_have_limits():
    for sql in (
        ops_sql.top_queries_by_elapsed(7, "ALL", limit=50),
        ops_sql.failures_by_error(7),
        security_sql.failed_logins(7),
        security_sql.recent_ddl_changes(7),
        mart_sql.open_alert_events(),
        mart_sql.action_queue(),
    ):
        assert re.search(r"\bLIMIT \d+", sql), f"missing LIMIT:\n{sql}"


def test_top_queries_limit_is_clamped():
    assert "LIMIT 500" in ops_sql.top_queries_by_elapsed(7, "ALL", limit=99999)


def test_billed_credits_apply_cloud_services_adjustment():
    for sql in (
        cost_sql.metering_daily_by_service(7),
        cost_sql.cortex_daily_spend(7),
        cost_sql.contract_consumed_credits("2026-01-01"),
    ):
        assert "CREDITS_ADJUSTMENT_CLOUD_SERVICES" in sql
        assert "CREDITS_BILLED" in sql


def test_contract_start_date_validated():
    with pytest.raises(ValueError):
        cost_sql.contract_consumed_credits("2026-01-01'; DROP TABLE x;--")
    with pytest.raises(ValueError):
        cost_sql.contract_consumed_credits("Jan 1 2026")


def test_mfa_gap_requires_login_evidence():
    sql = security_sql.users_without_mfa("ALFA")
    assert "LOGIN_HISTORY" in sql and "PASSWORD" in sql
    assert "JOIN" in sql  # evidence join, not a bare USERS scan


def test_attribution_is_share_based_not_dollars():
    sql = cost_sql.allocated_attribution(7, "DATABASE_NAME", "ALL")
    assert "RATIO_TO_REPORT" in sql
    assert "$" not in sql  # dollarization happens in logic/formulas


def test_free_text_filters_are_sanitized_into_builders():
    hostile = ops_sql.top_queries_by_elapsed(7, "ALL", warehouse_contains="x'; DROP TABLE q;--")
    assert "DROP" not in hostile.upper().replace("BYTES_SPILLED_TO_REMOTE_STORAGE", "")
    clean = ops_sql.top_queries_by_elapsed(7, "ALL", warehouse_contains="TRXS")
    assert "ILIKE '%TRXS%'" in clean


def test_mart_readers_target_overwatch_objects():
    assert "DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD" in mart_sql.exec_board("ALFA", 7)
    assert "DBA_MAINT_DB.OVERWATCH.SETTINGS" in mart_sql.settings()
    assert "DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" in mart_sql.open_alert_events()
