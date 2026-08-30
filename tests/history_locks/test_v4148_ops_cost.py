"""v4.148.0 Operations + Cost fixes: volume-drop robustness + release-compare display."""

from __future__ import annotations

import pandas as pd
import pytest

from app.data import ops_sql
from app.logic import insights

sqlglot = pytest.importorskip("sqlglot")


def test_volume_deltas_excludes_transient_and_requires_steady_baseline():
    sql = ops_sql.volume_deltas()
    # A real drop needs a STEADY baseline (mirrors the anomaly min-active-days
    # idea): >=1,000 rows/day AND written on >=3 of the prior 7 days.
    assert "DAYS_ACTIVE_7D >= 3" in sql
    assert "AVG_ROWS >= 1000" in sql
    assert "DAYS_ACTIVE_7D" in sql.split("SELECT", 2)[1]  # surfaced in the output
    # Transient / staging / internal exclusions kill the truncate-reload false
    # positives (the owner's DB_T_PROD_STAG dated tables, the SNOWFLAKE internal DB).
    assert "REGEXP_LIKE" in sql and "_[0-9]{8}" in sql
    assert "_STAG" in sql          # staging schemas excluded (delimited suffix)
    assert "<> 'SNOWFLAKE'" in sql
    # The staging match is delimited (ESCAPE) so it cannot catch POSTGRES etc.,
    # and the redundant TEMP/TMP name filter was dropped (the baseline gate covers it).
    assert "ESCAPE" in sql
    assert "'%TMP%'" not in sql and "'%TEMP%'" not in sql
    # Still yesterday vs prior-7d.
    assert "DATEADD('day', -1, CURRENT_DATE())" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_release_compare_humanizes_durations_and_keeps_units():
    df = pd.DataFrame([
        {"PERIOD": "BEFORE", "QUERY_COUNT": 1000, "FAILED_COUNT": 10,
         "P95_ELAPSED_SEC": 3661.0, "QUEUED_SEC": 0.50, "SPILL_REMOTE_GB": 0.020},
        {"PERIOD": "AFTER", "QUERY_COUNT": 1200, "FAILED_COUNT": 30,
         "P95_ELAPSED_SEC": 45.0, "QUEUED_SEC": 0.50, "SPILL_REMOTE_GB": 0.020},
    ])
    rows = {r["Metric"]: r for r in insights.compare_release_periods(df)}
    # Durations humanize to Hr/Min/Sec like the rest of the app (no 6-decimal floats).
    assert "h" in rows["p95 runtime (s)"]["Before"]          # 3661s -> hours
    assert rows["p95 runtime (s)"]["After"].endswith("s")    # 45s
    assert "." not in rows["p95 runtime (s)"]["Before"]      # humanized, not "3661.00"
    # Queued/spill are PER-QUERY now (insights_sql divides by QUERY_COUNT); the labels and
    # units say so, and tiny per-query spill renders as MB/query rather than a 0.02 GB float.
    assert rows["Remote spill (GB/query)"]["Before"] == "20.5 MB/q"   # 0.020 GB -> 20.5 MB/q
    assert rows["Queued (s/query)"]["Before"] == "0.50 s/q"
    assert rows["Queries"]["After"] == "1,200"
    assert rows["Failure %"]["Before"] == "1.0%"
    # The verdict logic (computed from numerics before formatting) is unchanged.
    assert rows["p95 runtime (s)"]["Verdict"] == "Better"
