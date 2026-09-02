"""Locks for V122 — anomaly sweep self-heals across the reconcile delete+reload race
(round-9 task-dag-ordering finding).

The standalone 07:00 TASK_ANOMALY_SWEEP reads FACT_METERING_DAILY / FACT_WAREHOUSE_DAILY,
which TASK_NIGHTLY_RECONCILE deletes+reloads for D-1..D-3 non-atomically. Scoring only
MAX(DAY) meant a mid-reload run scored D-4 and yesterday's spike was permanently missed.
V122 scores the last 3 complete days; the existing per-(series,day) dedup makes a skipped
day self-heal on the next run."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V122__anomaly_sweep_reconcile_race.sql").read_text(encoding="utf-8")


def test_v122_guard_shape_and_chain():
    assert "EXCEPTION (-20122" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                 # the V035 lesson holds
    assert "IF (v < 121) THEN" in _MIG and "SELECT 122 AS VERSION" in _MIG
    assert "$_" not in _MIG                                # $$-only dollar quoting (V089 lesson)
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()" in _MIG


def test_sweep_scores_the_last_three_days_not_just_max_day():
    # the self-heal: widen the scored window from MAX(DAY) to the last 3 complete days.
    assert "WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())" in _MIG
    assert "s.DAY = (SELECT MAX(DAY) FROM series)" not in _MIG
    # per-(series,day) dedup is what keeps each day to at most one alert across runs
    assert "'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)" in _MIG
    assert "AND NOT EXISTS (" in _MIG
    # migration tail re-runs the sweep so the last 3 days are (re)scored under the new window
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();" in _MIG


def test_v122_preserves_the_materiality_and_baseline_logic():
    # re-derived from V097 — the robust-z / mean-AD fallback / materiality gate survive
    assert "MEAN_AD" in _MIG and "0.7979" in _MIG          # V097 mean-AD fallback
    assert "l.CREDITS * :credit_price >= 50" in _MIG        # V076 materiality floor
    assert "l.ACTIVE_DAYS >= 10" in _MIG


def test_validate_floor_and_docs_track_v122():
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V124 applied" in val and "VERSION BETWEEN 1 AND 124) = 124" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V122__anomaly_sweep_reconcile_race.sql" in (_ROOT / rel).read_text(encoding="utf-8")
