"""rec#17: account-wide failed / killed / aborted query waste board — allocated
compute spent on non-success queries, rolled up by fingerprint.
"""

from pathlib import Path

from app.data import insights_sql

_OPS = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"
        / "operations.py").read_text(encoding="utf-8")


def test_builder_allocates_hour_share_to_non_success_by_fingerprint():
    sql = insights_sql.wasted_query_spend_usd(30, "ALFA")
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY" in sql
    # allocation: hour credits x this query's exec-time share
    assert "m.HOUR_CREDITS * q.EXEC_MS / NULLIF(t.TOTAL_EXEC_MS, 0)" in sql
    # display filtered to non-success, rolled up by fingerprint with a run count
    assert "a.EXECUTION_STATUS <> 'SUCCESS'" in sql
    assert "QUERY_PARAMETERIZED_HASH AS FINGERPRINT" in sql
    assert "COUNT(DISTINCT a.QUERY_ID) AS FAILED_RUNS" in sql


def test_allocation_denominator_counts_all_queries_not_just_failures():
    # the t CTE (the per-warehouse-hour denominator) is built from q, which does NOT
    # filter status — otherwise a failed query's share would be inflated by dropping
    # the successful queries that also consumed the hour. The status filter lives ONLY
    # in the outer WHERE (after the t/a CTEs).
    sql = insights_sql.wasted_query_spend_usd(30, "ALFA")
    before_t = sql.split("t AS (", 1)[0]        # the q and m CTEs
    assert "EXECUTION_STATUS <> 'SUCCESS'" not in before_t


def test_builder_bounds_the_limit():
    assert "LIMIT 200" in insights_sql.wasted_query_spend_usd(30, "ALFA", limit=9999)


def test_panel_wired_on_operations_queries_tab():
    assert "wasted_query_spend_usd(" in _OPS
    assert "ops_waste_toggle" in _OPS
    assert "WASTED_USD" in _OPS
