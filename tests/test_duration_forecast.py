"""Trending-late SLA forecast (F5): pure logic + wiring.

duration_sla_forecast (app/logic/insights.py) flags tasks whose daily runtime is
CLIMBING toward a miss — the leading half of the duration signal, complementary to
the retrospective task_duration_anomalies. Reuses the already-loaded FACT_TASK_DAILY
frame; no new SQL builder, no migration. It gates on the recent-window MEDIAN (so a
single spike can't fake a trend) plus a net climb across the recent window.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.insights import duration_sla_forecast

_ROOT = Path(__file__).resolve().parents[1]


def _task(values: list[float], db: str = "DB", task: str = "T") -> pd.DataFrame:
    return pd.DataFrame([
        {"DAY": f"2026-08-{i + 1:02d}", "DATABASE_NAME": db, "TASK_NAME": task, "AVG_SEC": v}
        for i, v in enumerate(values)
    ])


def _task_schema(values: list[float], schema: str, db: str = "DB", task: str = "T") -> pd.DataFrame:
    return pd.DataFrame([
        {"DAY": f"2026-08-{i + 1:02d}", "DATABASE_NAME": db, "SCHEMA_NAME": schema,
         "TASK_NAME": task, "AVG_SEC": v}
        for i, v in enumerate(values)
    ])


def test_sustained_climb_is_predicted_miss():
    out = duration_sla_forecast(_task([30, 30, 30, 30, 30, 80, 85, 90]))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["FORECAST"] == "Predicted miss" and row["SEVERITY"] == "High"
    assert row["SLOWER_X"] >= 2.0


def test_steady_linear_climb_uses_pre_window_baseline():
    # baseline is the level BEFORE the recent window, so a steady climb reads as a
    # miss; a full-window median would inflate the baseline and under-report it.
    out = duration_sla_forecast(_task([20, 30, 40, 50, 60, 70, 80, 90]))
    assert len(out) == 1 and out.iloc[0]["FORECAST"] == "Predicted miss"


def test_moderate_climb_is_at_risk():
    out = duration_sla_forecast(_task([40, 40, 40, 40, 40, 50, 55, 60]))
    assert len(out) == 1 and out.iloc[0]["FORECAST"] == "At risk"
    assert out.iloc[0]["SEVERITY"] == "Medium"


def test_terminal_one_day_spike_not_flagged():
    # a single heavy final day is NOT a climb — the recent-window median stays at
    # baseline, so it must not masquerade as a Predicted miss (the drift owns it).
    assert duration_sla_forecast(_task([40, 40, 40, 40, 40, 40, 40, 90])).empty


def test_climb_with_last_day_dip_still_flagged():
    # a real climber that eases a little on the final day must NOT be dropped — the
    # median gate tolerates last-day noise (the old latest==peak rule missed these).
    out = duration_sla_forecast(_task([30, 30, 30, 30, 40, 60, 80, 75]))
    assert len(out) == 1 and out.iloc[0]["FORECAST"] == "Predicted miss"


def test_flat_task_not_flagged():
    assert duration_sla_forecast(_task([50] * 8)).empty


def test_mid_window_spike_not_flagged():
    assert duration_sla_forecast(_task([40, 40, 40, 40, 40, 40, 90, 50])).empty


def test_recovering_task_not_flagged():
    assert duration_sla_forecast(_task([90, 80, 70, 60, 50, 45, 42, 40])).empty


def test_short_history_not_flagged():
    assert duration_sla_forecast(_task([40, 50, 60, 70, 90, 100])).empty   # 6 days < 7


def test_trivially_fast_baseline_not_flagged():
    assert duration_sla_forecast(_task([5, 5, 5, 5, 5, 6, 8, 12])).empty   # baseline < 30s


def test_same_task_name_across_schemas_kept_separate():
    climb = _task_schema([30, 30, 30, 30, 30, 80, 85, 90], "PROD")
    flat = _task_schema([50] * 8, "STAGING")
    out = duration_sla_forecast(pd.concat([climb, flat], ignore_index=True))
    # only the PROD climber is flagged; the STAGING task of the same name is separate
    assert list(out["SCHEMA_NAME"]) == ["PROD"] and len(out) == 1


def test_duplicate_day_rows_deduped_below_gate():
    # 5 distinct days duplicated into 10 rows must NOT pass the >=7-distinct-day gate
    base = _task([40, 40, 40, 50, 90])
    dup = pd.concat([base, base], ignore_index=True)
    assert duration_sla_forecast(dup).empty


def test_empty_and_missing_columns():
    assert duration_sla_forecast(pd.DataFrame()).empty
    assert duration_sla_forecast(pd.DataFrame([{"DAY": "2026-08-01", "AVG_SEC": 50}])).empty


def test_forecast_wired_into_operations():
    src = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "duration_sla_forecast(" in src and "Predicted SLA miss" in src
