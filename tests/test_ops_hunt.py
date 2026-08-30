"""Operations-layer bug-hunt locks (2026-08-30).

Shipped in v4.354.0. One recurring family drove the round: Snowflake task auto-retries emit
multiple TASK_HISTORY rows for one scheduled run, and several read/loader paths counted attempts
instead of runs. App-side fixes F1/F3/F6-F11 plus two owner-gated forward migrations (V101, V102).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import insights_sql, ops_sql
from app.logic.insights import classify_task_error, compare_release_periods

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- F1 (HIGH): graph-run node view collapses auto-retries ------------------------
def test_task_graph_run_nodes_collapses_retries_to_terminal_attempt():
    sql = ops_sql.task_graph_run_nodes("42", "run-1", 7)
    assert "QUALIFY ROW_NUMBER() OVER (" in sql
    assert "PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in sql
    assert "ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1" in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- F3 (MED): dynamic-table failure count includes terminal-failure states -------
def test_dynamic_table_health_counts_upstream_and_cancelled_as_failures():
    sql = ops_sql.dynamic_table_health(7)
    # a DT that never refreshed because its upstream failed, or was cancelled, is not healthy
    assert "STATE IN ('FAILED', 'UPSTREAM_FAILED', 'CANCELLED')" in sql
    assert "COUNT_IF(STATE = 'FAILED') AS FAILURES," not in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- F7 (MED): release-compare normalizes queued/spill per query -------------------
def test_release_query_compare_normalizes_queued_and_spill_per_query():
    sql = insights_sql.release_query_compare("2026-08-01", 7, "ALFA")
    # both raw sums are divided by the per-period query count so a bigger release doesn't
    # look worse purely on volume
    assert "/ NULLIF(COUNT(*), 0) AS QUEUED_SEC" in sql
    assert "/ NULLIF(COUNT(*), 0) AS SPILL_REMOTE_GB" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_release_verdict_labels_declare_per_query_units():
    df = pd.DataFrame([
        {"PERIOD": "BEFORE", "QUERY_COUNT": 1000, "FAILED_COUNT": 10,
         "P95_ELAPSED_SEC": 10.0, "QUEUED_SEC": 0.5, "SPILL_REMOTE_GB": 0.01},
        {"PERIOD": "AFTER", "QUERY_COUNT": 1000, "FAILED_COUNT": 10,
         "P95_ELAPSED_SEC": 10.0, "QUEUED_SEC": 0.5, "SPILL_REMOTE_GB": 0.01},
    ])
    labels = {r["Metric"] for r in compare_release_periods(df)}
    assert "Queued (s/query)" in labels and "Remote spill (GB/query)" in labels
    assert "Queued (s)" not in labels and "Remote spill (GB)" not in labels


# ---- F8 (LOW): task-error classifier catches 'aborted' ----------------------------
def test_classify_task_error_maps_aborted_to_timeout_cancelled():
    assert classify_task_error("Statement was aborted by the user") == "Timeout / cancelled"
    assert classify_task_error("query cancelled") == "Timeout / cancelled"
    assert classify_task_error("statement timed out") == "Timeout / cancelled"


# ---- F11 (LOW): release verdicts get a minimum-absolute-delta floor ----------------
def test_release_verdict_flat_when_absolute_move_below_floor():
    # fail 0.1% -> 0.2% is +100% relative but only +0.1 percentage points (< 0.5pp floor);
    # p95 0.4s -> 0.5s is +25% but only +0.1s (< 2s floor). Both must read Flat, not Worse.
    df = pd.DataFrame([
        {"PERIOD": "BEFORE", "QUERY_COUNT": 1000, "FAILED_COUNT": 1,
         "P95_ELAPSED_SEC": 0.4, "QUEUED_SEC": 0.0, "SPILL_REMOTE_GB": 0.0},
        {"PERIOD": "AFTER", "QUERY_COUNT": 1000, "FAILED_COUNT": 2,
         "P95_ELAPSED_SEC": 0.5, "QUEUED_SEC": 0.0, "SPILL_REMOTE_GB": 0.0},
    ])
    v = {r["Metric"]: r["Verdict"] for r in compare_release_periods(df)}
    assert v["Failure %"] == "Flat"
    assert v["p95 runtime (s)"] == "Flat"


# ---- F6 (MED): monitor-coverage panel never paints a false green -------------------
def test_monitor_coverage_panel_neutral_when_probe_fails():
    body = _src("app/ui/pages/operations.py")
    panel = body.split("def _monitor_coverage_panel", 1)[1].split("\ndef ", 1)[0]
    # probe failure (or account scope) drops the section to neutral + a caveat, never a green
    # "all monitored" all-clear computed from an empty/failed read
    assert "not _mons.ok" in panel
    assert "alarm_health(None)" in panel


# ---- F9 (LOW): duration detectors disclose sub-baseline windows --------------------
def test_duration_detectors_gate_on_min_active_days():
    body = _src("app/ui/pages/operations.py")
    assert "DURATION_MIN_ACTIVE_DAYS" in body.split("from app.logic.insights import", 1)[1][:400]
    assert "_enough_hist = days >= DURATION_MIN_ACTIVE_DAYS" in body
    assert "days of history to assess drift" in body
    assert "days of history to project a miss" in body


# ---- F10 (LOW): failure-timeline all-clear discloses its mart basis ----------------
def test_failure_timeline_discloses_hourly_mart_basis():
    body = _src("app/ui/pages/operations.py")
    section = body.split("def _failure_timeline_section", 1)[1].split("\ndef ", 1)[0]
    assert "known_from_live: bool = False" in section
    assert "per the" in section and "hourly task mart" in section
    # the caller passes whether the count came from the live fallback vs the lagging mart
    assert "known_from_live=not _from_mart" in body


# ---- V101 / V102: owner-gated retry-collapse migrations ----------------------------
def test_v101_collapses_fact_task_daily_and_is_guarded():
    mig = _src("snowflake/migrations/V101__fact_task_daily_retry_collapse.sql")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS" in mig
    assert "WITH task_attempts AS (" in mig
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in mig
    assert "EXCEPTION (-20101" in mig and "IF (v < 100) THEN" in mig
    assert "SELECT 101 AS VERSION" in mig
    assert "CREATE TABLE" not in mig and "ALTER TABLE" not in mig
    sqlglot.parse(mig, dialect="snowflake")


def test_v102_collapses_both_task_marts_and_is_guarded():
    mig = _src("snowflake/migrations/V102__task_marts_retry_collapse.sql")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in mig
    # graph arm partitions INCLUDING the graph-run id; node arm without it
    assert "PARTITION BY GRAPH_RUN_GROUP_ID, DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in mig
    assert mig.count("QUALIFY ROW_NUMBER() OVER (") == 3   # 1 pre-existing (V095) + 2 new
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in mig  # V095 fix preserved
    assert "EXCEPTION (-20102" in mig and "IF (v < 101) THEN" in mig
    assert "SELECT 102 AS VERSION" in mig
    sqlglot.parse(mig, dialect="snowflake")
