"""P1 app-side: evidence pack, timeline, prompt, viz helpers exist."""

import pytest

from app.data import insights_sql, mart_sql


def test_anomaly_evidence_validates_date_and_scopes():
    sql = insights_sql.anomaly_evidence("2026-07-06", "WH_TRXS")
    assert "DATE '2026-07-06'" in sql
    assert "ELAPSED_H_PRIOR_AVG" in sql and "QUERY_PARAMETERIZED_HASH" in sql
    assert "WAREHOUSE_NAME ILIKE '%WH~_TRXS%' ESCAPE '~'" in sql
    with pytest.raises(ValueError):
        insights_sql.anomaly_evidence("not-a-date")
    with pytest.raises(ValueError):
        insights_sql.anomaly_evidence("2026-07-06'; DROP--")


def test_incident_timeline_unions_four_sources_at_mart_parity():
    sql = mart_sql.incident_timeline(7, "Trexis")
    # v4.351: the 7d fallback is kept at parity with the 48h MART_INCIDENT_TIMELINE loader
    # (V066) — identical KIND labels, COMPANY + REF_ID columns, and the WH_CHANGE arm.
    assert "'ALERT'" in sql and "'TASK_FAIL'" in sql and "'DDL'" in sql and "'WH_CHANGE'" in sql
    assert "'TASK FAILURE'" not in sql and "'DDL CHANGE'" not in sql   # old drifted labels gone
    assert "WAREHOUSE_CHANGE_REGISTRY" in sql                          # the change arm is present
    assert "AS REF_ID" in sql and "EVENT_ID AS REF_ID" in sql          # REF_ID carried for drills
    assert "'GRANT', 'REVOKE'" in sql                                  # full DDL type set
    assert "::TIMESTAMP_NTZ" in sql                      # one dtype on the axis
    assert "COMPANY IN ('Trexis', 'ALL')" in sql
    assert "STATE = 'FAILED'" in sql and "LIMIT 400" in sql


def test_fact_daily_activity_builder():
    sql = mart_sql.fact_daily_activity(14)
    assert "FACT_QUERY_HOURLY" in sql and "QUERIES" in sql and "FAILS" in sql


def test_viz_helpers_exist():
    from app.ui import charts

    for fn in ("sparkline_row", "hour_heatmap"):
        assert callable(getattr(charts, fn))
