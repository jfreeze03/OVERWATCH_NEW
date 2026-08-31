"""Data-loader/ETL bug-hunt locks (2026-08-30, v4.371.0).

Adversarial data-loader pass (6 finders). Eight surfaced, six confirmed, two refuted. Four fixes here
(2 app + V113 + V114); three deferred (SP_LOAD_APP_COST/SP_LOAD_STORAGE_TRUTH txn-wrap, IDLE_PCT
weighting) with a documented rationale.
  - [MED] backfill_365 FACT_TASK_DAILY collapses auto-retries to the terminal attempt (was RUNS=2/FAILED=1).
  - [LOW] ops_diag mart readers window on CURRENT_DATE (day-aligned, matching the live twins).
  - [MED/V113] MART_INCIDENT_TIMELINE TASK_FAIL uses COMPLETED_TIME (matching the live reader).
  - [LOW/V114] TASK_ANOMALY_SWEEP cron moved to 07:00, after the 06:45 daily loader.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_backfill_365_task_daily_dedups_retries() -> None:
    sql = _read("snowflake/backfill_365.sql")
    # the FACT_TASK_DAILY arm now sources a terminal-attempt CTE before aggregating
    assert "WITH task_attempts AS (" in sql
    assert ("QUALIFY ROW_NUMBER() OVER (\n"
            "        PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME\n"
            "        ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1") in sql
    assert "FROM task_attempts" in sql
    # the idempotency guard survives on the outer select
    assert "DATE(QUERY_START_TIME) < COALESCE((SELECT MIN(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY)" in sql


def test_ops_diag_readers_are_day_aligned() -> None:
    src = _read("app/data/mart27_sql.py")
    # both ops_diag builders + their coverage gates use CURRENT_DATE (2 where + 2 gate)
    assert src.count("d.HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())") == 2
    assert src.count("FIRST_TS FROM cov) <= DATEADD('day', -{days} + 1, CURRENT_DATE())") == 2
    # the ops_diag builders no longer use the rolling CURRENT_TIMESTAMP for HOUR_TS
    assert "d.HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())" not in src


def test_v113_incident_timeline_uses_completed_time() -> None:
    mig = _read("snowflake/migrations/V113__incident_timeline_task_fail_completed_time.sql")
    assert "SELECT COMPLETED_TIME, 'TASK_FAIL'," in mig
    assert "SELECT QUERY_START_TIME, 'TASK_FAIL'," not in mig
    assert "WHERE COMPLETED_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'" in mig
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    assert "EXCEPTION (-20113" in mig and "IF (v < 112) THEN" in mig
    assert "SELECT 113 AS VERSION" in mig and "WHERE VERSION = 113)" in mig


def test_v114_anomaly_sweep_reschedule() -> None:
    mig = _read("snowflake/migrations/V114__anomaly_sweep_after_daily_loader.sql")
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP SUSPEND;" in mig
    assert "SET SCHEDULE = 'USING CRON 0 7 * * * America/Chicago'" in mig
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ANOMALY_SWEEP RESUME;" in mig
    assert "EXCEPTION (-20114" in mig and "IF (v < 113) THEN" in mig
    assert "SELECT 114 AS VERSION" in mig and "WHERE VERSION = 114)" in mig
