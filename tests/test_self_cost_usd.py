"""Next-Fifty #7 Slice A (v4.589.0): Admin > App self-cost puts dollars on what OVERWATCH costs to run —
task-graph compute ATTRIBUTED per pipeline (mart-only, excl. idle) beside the shared warehouse's METERED
compute — and shows overload queueing on that warehouse by Central hour-of-day."""

from __future__ import annotations

import re

from app.data import mart_sql


def test_run_cost_builder_is_mart_only_and_uncapped():
    sql = mart_sql.app_self_cost_usd(30)
    assert "ACCOUNT_USAGE" not in sql
    for part in ("MART_TASK_GRAPH_DAILY", "FACT_WAREHOUSE_DAILY", "UPPER(DATABASE_NAME) = 'DBA_MAINT_DB'",
                 "UPPER(SCHEMA_NAME) = 'OVERWATCH'", "WAREHOUSE_NAME = 'WH_ALFA_ADMIN'",
                 "SUM(p.ATTRIBUTED_CREDITS) OVER ()", "DAY < CURRENT_DATE()", "LEFT JOIN pipes p ON 1 = 1"):
        assert part in sql, part
    assert "DATEADD('day', -90," in mart_sql.app_self_cost_usd(9999)


def test_queue_by_hour_builder():
    sql = mart_sql.app_warehouse_queue_by_hour(999)
    assert "DATEADD('day', -30," in sql
    assert "CONVERT_TIMEZONE('America/Chicago', START_TIME)" in sql
    assert "APPROX_PERCENTILE(COALESCE(QUEUED_OVERLOAD_TIME, 0), 0.95)" in sql
    durations = re.findall(r"AS (\w*QUEUED_OVERLOAD\w*)", sql)
    assert durations and all(c.endswith("_SEC") for c in durations)
