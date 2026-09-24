"""Next-Fifty #1 (v4.589.0): insights.cycle_night_summary folds the whole-night roll-up
(etl_control_sql.cycle_night_health_scan) into the counts the Brief / Control Room / Operations share.
Counts come from the UNCAPPED TOTAL_* window columns (never len() of the LIMITed frame)."""

from __future__ import annotations

import pandas as pd

from app.logic.insights import cycle_night_summary


def _row(wf: str, status: str, **kw) -> dict:
    base = {"WORKFLOW_NAME": wf, "NIGHT_STATUS": status, "CYCLE_DATE": pd.Timestamp("2026-09-23"),
            "FAILED_TASK_COUNT": 0, "RUNNING_TASK_COUNT": 0, "TASK_COUNT": 5}
    base.update(kw)
    return base


def test_no_data_is_empty():
    assert cycle_night_summary(None) == {}
    assert cycle_night_summary(pd.DataFrame()) == {}
    assert cycle_night_summary(pd.DataFrame([{"WORKFLOW_NAME": "WF_A"}])) == {}


def test_counts_come_from_uncapped_totals():
    df = pd.DataFrame([_row("WF_A", "FAILED", FAILED_TASK_COUNT=2, TOTAL_WORKFLOWS=40, TOTAL_FAILED_WF=5,
                            TOTAL_FAILED_TASKS=9, TOTAL_MISSING_WF=3, TOTAL_RUNNING_WF=1, TOTAL_PENDING_WF=0),
                       _row("WF_B", "MISSING")])
    s = cycle_night_summary(df)
    assert (s["failed_wf"], s["failed_tasks"], s["missing_wf"], s["workflows"]) == (5, 9, 3, 40)
    assert s["ok_wf"] == 40 - 5 - 3 - 1 - 0


def test_falls_back_to_frame_counts_without_totals():
    df = pd.DataFrame([_row("WF_A", "FAILED", FAILED_TASK_COUNT=2), _row("WF_B", "FAILED", FAILED_TASK_COUNT=1),
                       _row("WF_C", "MISSING"), _row("WF_D", "OK")])
    s = cycle_night_summary(df)
    assert (s["failed_wf"], s["failed_tasks"], s["missing_wf"], s["workflows"], s["ok_wf"]) == (2, 3, 1, 4, 1)


def test_labels_name_two_plus_more():
    df = pd.DataFrame([_row("WF_C", "FAILED"), _row("WF_A", "FAILED"), _row("WF_B", "FAILED")])
    assert cycle_night_summary(df)["failed_label"] == "WF_A, WF_B +1 more"
    assert cycle_night_summary(df)["missing_label"] == ""


def test_next_cycle_overdue_and_age():
    s = cycle_night_summary(pd.DataFrame([_row("WF_A", "OK", NEXT_CYCLE_OVERDUE=1, CYCLE_AGE_SEC=120000)]))
    assert s["next_cycle_overdue"] is True and s["cycle_age_sec"] == 120000.0
    s = cycle_night_summary(pd.DataFrame([_row("WF_A", "OK", NEXT_CYCLE_OVERDUE=float("nan"),
                                               CYCLE_AGE_SEC=float("nan"))]))
    assert s["next_cycle_overdue"] is False and s["cycle_age_sec"] is None


def test_ok_wf_floored_at_zero():
    df = pd.DataFrame([_row("WF_A", "FAILED", TOTAL_WORKFLOWS=1, TOTAL_FAILED_WF=3, TOTAL_MISSING_WF=2)])
    assert cycle_night_summary(df)["ok_wf"] == 0
