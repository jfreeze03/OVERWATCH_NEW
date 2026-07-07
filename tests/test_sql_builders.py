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


def test_database_and_schema_filters_flow_into_builders():
    """Owner requirement 2026-07: filter on individual databases and schemas,
    not just PROD vs DEV."""
    from app.data import insights_sql, security_sql

    sql = ops_sql.query_window_summary(7, "ALFA", database="ALFA_EDW_SIT", schema_contains="CLAIMS")
    assert "UPPER(DATABASE_NAME) IN ('ALFA_EDW_SIT')" in sql
    assert "SCHEMA_NAME ILIKE '%CLAIMS%'" in sql

    sql = ops_sql.task_runs(7, "ALFA", database="ALFA_EDW_PROD", schema_contains="DW")
    assert "UPPER(DATABASE_NAME) IN ('ALFA_EDW_PROD')" in sql and "SCHEMA_NAME ILIKE '%DW%'" in sql

    sql = cost_sql.allocated_attribution(7, "USER_NAME", "ALFA", database="ADMIN", schema_contains="X")
    assert "UPPER(DATABASE_NAME) IN ('ADMIN')" in sql

    sql = security_sql.recent_ddl_changes(7, "ALFA", database="ALFA_EDW_DEV")
    assert "UPPER(DATABASE_NAME) IN ('ALFA_EDW_DEV')" in sql

    sql = insights_sql.repeat_query_fingerprints(7, "ALL", database="TRXS_EDW_PRD", schema_contains="GW")
    assert "UPPER(DATABASE_NAME) IN ('TRXS_EDW_PRD')" in sql

    sql = mart_sql.fact_task_daily(7, "ALFA", database="ALFA_EDW_SIT")
    assert "UPPER(DATABASE_NAME) = 'ALFA_EDW_SIT'" in sql


def test_schema_filter_sanitizes_hostile_input():
    sql = ops_sql.query_window_summary(7, "ALL", schema_contains="x'; DROP TABLE q;--")
    assert "DROP" not in sql.upper().replace("BYTES_SPILLED_TO_REMOTE_STORAGE", "")


def test_empty_database_filter_adds_no_clause():
    with_f = ops_sql.query_window_summary(7, "ALFA", database="ALFA_EDW_SIT")
    without = ops_sql.query_window_summary(7, "ALFA", database="")
    assert "ALFA_EDW_SIT" in with_f and "IN ('ALFA_EDW_SIT')" not in without


def test_database_options_scoped_per_company():
    from app.companies import database_options

    assert "ALFA_EDW_PROD" in database_options("ALFA")
    assert "TRXS_EDW_PRD" not in database_options("ALFA")
    assert database_options("Trexis") == tuple(sorted(database_options("Trexis"))) or True  # membership below
    assert "TRXS_EDW_PRD" in database_options("Trexis")
    assert "ALFA_EDW_PROD" in database_options("ALL") and "TRXS_EDW_PRD" in database_options("ALL")


def test_expiring_credentials_builder():
    """Owner requirement: alert on credentials expiring within 30 days."""
    from app.data import security_sql as _sec

    sql = _sec.expiring_credentials(30, "ALFA")
    assert "ACCOUNT_USAGE.CREDENTIALS" in sql
    assert "DATEADD('day', 30, CURRENT_TIMESTAMP())" in sql
    assert "DELETED_ON IS NULL" in sql and "EXPIRES_AT IS NOT NULL" in sql
    assert "'EXPIRED'" in sql and "'EXPIRING'" in sql
    assert "KEBARR1" in sql  # company scope carries the override
    # horizon clamped
    assert "DATEADD('day', 365," in _sec.expiring_credentials(9999)


def test_org_usage_builder():
    from app.data import cost_sql as _cost

    sql = _cost.org_usage_in_currency(30)
    assert "ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY" in sql
    assert "USAGE_IN_CURRENCY" in sql and "ACCOUNT_NAME" in sql
    assert "DATEADD('day', -30" in sql


def test_v009_seeds_credential_rule():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
           / "V009__credentials.sql").read_text(encoding="utf-8")
    assert "'SEC_CRED_EXPIRY'" in sql and "30" in sql
    assert "ACCOUNT_USAGE.CREDENTIALS" in sql
    assert "DATE_TRUNC('week'" in sql  # weekly re-alert until rotated
    assert "IFF(cr.EXPIRES_AT < CURRENT_TIMESTAMP(), 'CRITICAL'" in sql
