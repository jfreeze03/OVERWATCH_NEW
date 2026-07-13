"""P1 app-side: evidence pack, timeline, prompt, viz helpers exist."""

import pandas as pd
import pytest

from app.data import insights_sql, mart_sql
from app.logic.ai_prompts import anomaly_explain_prompt


def test_anomaly_evidence_validates_date_and_scopes():
    sql = insights_sql.anomaly_evidence("2026-07-06", "WH_TRXS")
    assert "DATE '2026-07-06'" in sql
    assert "ELAPSED_H_PRIOR_AVG" in sql and "QUERY_PARAMETERIZED_HASH" in sql
    assert "WAREHOUSE_NAME ILIKE '%WH~_TRXS%' ESCAPE '~'" in sql
    with pytest.raises(ValueError):
        insights_sql.anomaly_evidence("not-a-date")
    with pytest.raises(ValueError):
        insights_sql.anomaly_evidence("2026-07-06'; DROP--")


def test_incident_timeline_unions_three_sources():
    sql = mart_sql.incident_timeline(7, "Trexis")
    assert "'ALERT'" in sql and "'DDL CHANGE'" in sql
    # r26 (owner 2026-07-13): the TASK FAILURE arm left with task monitoring.
    assert "'TASK FAILURE'" not in sql
    assert "::TIMESTAMP_NTZ" in sql                      # one dtype on the axis
    assert "COMPANY IN ('Trexis', 'ALL')" in sql
    assert "LIMIT 400" in sql


def test_fact_daily_activity_builder():
    sql = mart_sql.fact_daily_activity(14)
    assert "FACT_QUERY_HOURLY" in sql and "QUERIES" in sql and "FAILS" in sql


def test_anomaly_prompt_grounded_and_bounded():
    ev = pd.DataFrame([{"SAMPLE_TEXT": "SELECT 1", "WAREHOUSE_NAME": "WH_X",
                        "RUNS_DAY": 10, "ELAPSED_H_DAY": 5.0, "ELAPSED_H_PRIOR_AVG": 1.0}])
    p = anomaly_explain_prompt("t", "d", ev, None, "2026-07-06 vs prior 7d")
    assert "ONLY the evidence" in p and "not counted" in p
    assert "WH_X" in p and "Never invent" in p
    p2 = anomaly_explain_prompt("t", "d", ev, 3, "w")
    assert "DDL statements in the same window: 3" in p2


def test_viz_helpers_exist():
    from app.ui import charts

    for fn in ("sparkline_row", "hour_heatmap", "waterfall_usd", "event_timeline"):
        assert callable(getattr(charts, fn))
