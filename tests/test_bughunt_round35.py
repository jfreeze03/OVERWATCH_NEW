"""Bug-hunt round 35: operations & pipeline intelligence.

Multi-agent adversarial sweep (6 finder dimensions -> per-finder refute -> completeness critic).
5 fixes shipped; the freshness dead-Late-tier (R35-01), a LOW row-cap ordering (OPS35-3), and three
unverified critic gaps are deferred to a focused follow-up.

OPS35-1 [MED]  pipeline_sla_forecast flagged "Overdue" against the MEDIAN refresh gap, so a table that
        idles on weekends/overnight (median ~1 day, but real gaps include the ~3-day weekend) read
        Overdue/High every Monday while within SLA. Now judges against the p90 gap (LONG_GAP_MIN),
        mirroring the task_freshness_status p90 fix; falls back to the median for uniform cadence / old
        frames.
OPS35-2 [MED]  compare_release_periods' zero-baseline branch decided by sign with no absolute floor, so a
        from-zero move below the per-metric min_abs_delta (e.g. 0 -> 0.0000002 GB/q remote spill) read
        "Worse" while an equal nonzero-baseline move read "Flat". Now honors the floor in both branches.
R35-SORT [MED]  task_freshness_status sorted the on-call silent-stop table by (median-based) OVERDUE_MIN
        with no severity key, so a Medium 'Late' row could rank above a High 'Stale' one. Now
        severity-first, mirroring dormant/reawakening/takeover_severity.
R35-DUR-01 [MED]  duration_sla_forecast vetoed a SUSTAINED plateau (a task that stepped up N× and then
        held flat) because its 'climbing' gate is strict '>', a false all-clear. Now also admits a
        recent window whose LOW end is already >= AT_RISK_X × baseline.
R35-DUR-02 [LOW]  the forecast table showed SLOWER_X (= recent-median/baseline) beside LATEST_SEC under a
        caption reading 'latest vs baseline'. Added RECENT_MED_SEC so the ratio reconciles and reworded
        the caption.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.data import insights_sql
from app.logic.insights import (
    compare_release_periods,
    duration_sla_forecast,
    pipeline_sla_forecast,
    task_freshness_status,
)


# --- OPS35-2: release-compare honors the floor on a zero baseline ----------
def _release_df(spill_before: float, spill_after: float) -> pd.DataFrame:
    base = {"QUERY_COUNT": 2000, "FAILED_COUNT": 0, "P95_ELAPSED_SEC": 1.0, "QUEUED_SEC": 0.0}
    return pd.DataFrame([
        {"PERIOD": "BEFORE", **base, "SPILL_REMOTE_GB": spill_before},
        {"PERIOD": "AFTER", **base, "SPILL_REMOTE_GB": spill_after},
    ])


def _verdict(rows: list[dict], label: str) -> str:
    return next(r["Verdict"] for r in rows if r["Metric"] == label)


def test_round35_release_compare_suppresses_sub_floor_from_zero_move():
    # 0 -> 0.0000002 GB/q is far below the 0.001 floor -> Flat, not a false "Worse"
    rows = compare_release_periods(_release_df(0.0, 0.0000002))
    assert _verdict(rows, "Remote spill (GB/query)") == "Flat"
    # a genuine from-zero regression above the floor still reads Worse (direction preserved)
    rows_big = compare_release_periods(_release_df(0.0, 0.5))
    assert _verdict(rows_big, "Remote spill (GB/query)") == "Worse"
    # a from-zero improvement above the floor still reads Better
    rows_imp = compare_release_periods(_release_df(0.5, 0.0))
    assert _verdict(rows_imp, "Remote spill (GB/query)") == "Better"


# --- OPS35-1: pipeline SLA judges overdue against the p90 gap --------------
def _pipe_row(**over):
    row = {"SLA_MET": True, "HOURS_SINCE": 60.0, "MAX_AGE_HOURS": 72.0, "RUNWAY_HOURS": 12.0,
           "MEDIAN_GAP_MIN": 1440.0, "LONG_GAP_MIN": 4320.0, "REFRESHES": 10}
    row.update(over)
    return row


def test_round35_pipeline_sla_uses_p90_gap_not_median():
    # weekend-idle table within SLA: p90 gap (72h) means 60h-since is NOT overdue
    weekend = pipeline_sla_forecast(pd.DataFrame([_pipe_row()]))
    assert weekend.iloc[0]["FORECAST"] != "Overdue"
    # a genuinely stalled uniform-cadence table still flags Overdue
    stalled = pipeline_sla_forecast(pd.DataFrame([_pipe_row(
        MAX_AGE_HOURS=1000.0, HOURS_SINCE=200.0, RUNWAY_HOURS=800.0,
        MEDIAN_GAP_MIN=60.0, LONG_GAP_MIN=60.0)]))
    assert stalled.iloc[0]["FORECAST"] == "Overdue"
    # old-shape frame without LONG_GAP_MIN falls back to the median (unchanged behavior)
    old = _pipe_row()
    del old["LONG_GAP_MIN"]
    legacy = pipeline_sla_forecast(pd.DataFrame([old]))
    assert legacy.iloc[0]["FORECAST"] == "Overdue"   # median 24h -> 60h is >1.5x median


def test_round35_pipeline_sla_sql_emits_p90_gap():
    sql = insights_sql.pipeline_sla_forecast(14)
    assert "APPROX_PERCENTILE(GAP_MIN, 0.9) AS LONG_GAP_MIN" in sql
    assert "c.LONG_GAP_MIN" in sql


# --- R35-SORT: freshness triage is severity-first --------------------------
def test_round35_freshness_sorts_severity_first():
    df = pd.DataFrame([
        # High/Stale but small overdue (short-cadence stopped cron)
        {"TASK_NAME": "HIGH_STALE", "MEDIAN_GAP_MIN": 30, "LONG_GAP_MIN": 30, "MINS_SINCE_SUCCESS": 200},
        # Medium/Late with a LARGER median-based overdue (long-cadence late daily)
        {"TASK_NAME": "MED_LATE", "MEDIAN_GAP_MIN": 1440, "LONG_GAP_MIN": 1440, "MINS_SINCE_SUCCESS": 1700},
    ])
    out = task_freshness_status(df)
    assert out.iloc[0]["STATUS"] == "Stale" and out.iloc[0]["SEVERITY"] == "High"
    assert out.iloc[1]["STATUS"] == "Late"
    # the High row leads DESPITE its smaller OVERDUE_MIN
    assert out.iloc[0]["OVERDUE_MIN"] < out.iloc[1]["OVERDUE_MIN"]


# --- R35-DUR-01 / R35-DUR-02: sustained plateau + reconciling ratio --------
def _duration_df(seq: list[float]) -> pd.DataFrame:
    d0 = date(2026, 1, 1)
    return pd.DataFrame([
        {"DATABASE_NAME": "DB", "TASK_NAME": "T", "DAY": d0 + timedelta(days=i), "AVG_SEC": v}
        for i, v in enumerate(seq)
    ])


def test_round35_duration_forecast_flags_sustained_plateau():
    # stepped up 3x at day 5 and HELD flat — the strict climbing gate used to veto this (false clear)
    out = duration_sla_forecast(_duration_df([30, 30, 30, 30, 90, 90, 90, 90]))
    assert not out.empty
    row = out.iloc[0]
    assert row["FORECAST"] == "Predicted miss" and row["SEVERITY"] == "High"
    # R35-DUR-02: the displayed ratio reconciles with the columns beside it
    assert "RECENT_MED_SEC" in out.columns
    assert row["SLOWER_X"] == round(row["RECENT_MED_SEC"] / row["BASELINE_SEC"], 1)


def test_round35_duration_forecast_still_ignores_flat_at_baseline():
    # a task steady at its baseline is not a regression — must stay unflagged
    out = duration_sla_forecast(_duration_df([40, 40, 40, 40, 40, 40, 40, 40]))
    assert out.empty
