"""Operations-layer bug-hunt #4 locks (2026-08-30, v4.367.0).

Fourth adversarial pass (6 finders). Four surfaced, three confirmed, one refuted (V027's dead 'FAILED'
mart token was already re-derived by V057). Two app-side, one owner-gated migration (V109).
  - [MED] task_failure_details counted auto-retry attempts that ultimately SUCCEEDED as failures
    (no terminal-attempt dedup) -> paged on-call for green runs.
  - [MED] pipeline_sla_forecast trusted a single-interval refresh cadence -> false High "Overdue".
  - [MED/V109] SP_WAREHOUSE_CHANGE_SCAN counted failures with the dead = 'FAILED' token, so the
    post-change regression fail axis could never fire.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import insights_sql
from app.logic.insights import pipeline_sla_forecast

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Finding O1 -- task_failure_details dedups auto-retries to the terminal attempt
# --------------------------------------------------------------------------- #
def test_task_failure_details_keeps_only_the_terminal_attempt() -> None:
    sql = insights_sql.task_failure_details(7, "ALFA")
    # the terminal-attempt dedup keyed on the scheduled run
    assert "QUALIFY ROW_NUMBER() OVER (" in sql
    assert "PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in sql
    assert "ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1" in sql
    # FAILED is filtered AFTER the dedup (outer select over the terminal CTE), not in the inner scan
    assert "FROM terminal\nWHERE STATE = 'FAILED'" in sql
    # company scoping is still live (a company-taking builder) in the inner scan
    assert "DATABASE_NAME ILIKE 'ALFA%'" in sql


# --------------------------------------------------------------------------- #
# Finding O2 -- pipeline_sla_forecast needs >= 3 intervals to trust a cadence
# --------------------------------------------------------------------------- #
def _within_sla_row(refreshes: int) -> dict:
    # comfortably within a 48h SLA (5h old, 43h runway) but with a fast median gap
    return {"DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "TABLE_NAME": "T",
            "SLA_MET": True, "HOURS_SINCE": 5.0, "MAX_AGE_HOURS": 48.0,
            "RUNWAY_HOURS": 43.0, "MEDIAN_GAP_MIN": 10.0, "REFRESHES": refreshes}


def test_single_interval_cadence_does_not_forecast_overdue() -> None:
    out = pipeline_sla_forecast(pd.DataFrame([_within_sla_row(1)]))
    assert out.loc[0, "FORECAST"] == "On track", "a 1-interval backfill cadence must not fire Overdue"
    assert out.loc[0, "SEVERITY"] == "OK"


def test_established_cadence_still_forecasts_overdue() -> None:
    # same table, but a real >=3-interval cadence: 5h since refresh vs a ~10-min rhythm IS overdue
    out = pipeline_sla_forecast(pd.DataFrame([_within_sla_row(5)]))
    assert out.loc[0, "FORECAST"] == "Overdue"
    assert out.loc[0, "SEVERITY"] == "High"


# --------------------------------------------------------------------------- #
# Finding O3 / V109 -- warehouse change-scan failure axis uses the real status domain
# --------------------------------------------------------------------------- #
def test_v109_warehouse_change_scan_fail_token() -> None:
    mig = _src("snowflake/migrations/V109__warehouse_change_scan_fail_token.sql")
    # both the baseline and after arms count failures as <> 'SUCCESS'; the dead token is gone from code
    assert mig.count("COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS") == 2
    assert "COUNT_IF(q.EXECUTION_STATUS = 'FAILED') AS FAILS" not in mig
    # re-derives the proc only; no schema change, no task re-creation
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN" in mig
    assert "CREATE TASK" not in mig and "CREATE TABLE " not in mig and "ALTER TABLE " not in mig
    # ordered-apply guard + version stamp
    assert "EXCEPTION (-20109" in mig and "IF (v < 108) THEN" in mig
    assert "SELECT 109 AS VERSION" in mig and "WHERE VERSION = 109)" in mig
