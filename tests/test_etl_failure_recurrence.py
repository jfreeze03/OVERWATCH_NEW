"""ETL failure-recurrence estimator (insights.task_failure_recurrence).

Covers the streak walk, retry/running handling, the recency + rate, the evidence-gated
verdict ladder, the anti-overclaim LOW_HISTORY gate, worst-first ranking, and empty-in.
"""

from __future__ import annotations

import pandas as pd

from app.logic.insights import task_failure_recurrence


def _task(wf: str, task: str, states: list[str], start: str = "2026-09-01") -> pd.DataFrame:
    """Rows for one task; ``states`` newest-first: 'F' failed, 'S' succeeded, 'R' running.
    RN=1 is the newest run (latest RUN_START)."""
    n = len(states)
    dates = list(pd.date_range(end=start, periods=n))[::-1]   # newest (RN=1) first
    return pd.DataFrame({
        "WORKFLOW_NAME": [wf] * n,
        "TASK_NAME": [task] * n,
        "RN": list(range(1, n + 1)),
        "RUN_START": dates,
        "IS_FAILED": [1 if s == "F" else 0 for s in states],
        "IS_RUNNING": [1 if s == "R" else 0 for s in states],
    })


def test_actively_broken_leading_streak() -> None:
    out = task_failure_recurrence(_task("WF_A", "SP_BROKEN", ["F", "F", "F", "S", "S", "S"]))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["FAIL_STREAK"] == 3
    assert r["SEVERITY"] == "High"
    assert bool(r["LATEST_FAILED"])
    assert r["FAILS"] == 3 and r["RUNS_ANALYZED"] == 6
    assert "Actively broken" in r["VERDICT"]


def test_chronic_but_currently_green() -> None:
    # latest passed, but 4 of 6 analyzed failed -> Chronic, streak 0 (a streak-only view would miss it)
    out = task_failure_recurrence(_task("WF_A", "SP_CHRONIC", ["S", "F", "F", "F", "F", "S"]))
    r = out.iloc[0]
    assert r["FAIL_STREAK"] == 0
    assert r["SEVERITY"] == "Medium"
    assert "Chronic" in r["VERDICT"]
    assert not bool(r["LATEST_FAILED"])


def test_running_latest_run_excluded_from_analysis() -> None:
    # the in-flight newest run neither counts nor clears the streak of the two prior fails
    out = task_failure_recurrence(_task("WF_A", "SP_RUN", ["R", "F", "F", "S", "S", "S"]))
    r = out.iloc[0]
    assert r["RUNS_ANALYZED"] == 5          # the running run is dropped from the denominator
    assert r["FAIL_STREAK"] == 2
    assert r["SEVERITY"] == "High"


def test_tiny_sample_latest_passed_withholds_chronic_label() -> None:
    # 1 fail in 2 runs, latest passed: LOW_HISTORY -> never a scary label (defeats 1/1=100% overclaim)
    out = task_failure_recurrence(_task("WF_A", "SP_THIN", ["S", "F"]))
    r = out.iloc[0]
    assert bool(r["LOW_HISTORY"])
    assert r["SEVERITY"] == "Low"
    assert r["VERDICT"] == "Occasional failure"


def test_tiny_sample_latest_failed_still_surfaces() -> None:
    # an observed latest failure is a FACT, not an estimate -> surfaces regardless of sample size
    out = task_failure_recurrence(_task("WF_A", "SP_ONE", ["F"]))
    r = out.iloc[0]
    assert r["FAIL_STREAK"] == 1
    assert r["VERDICT"] == "Failed the latest run"
    assert bool(r["LATEST_FAILED"])


def test_zero_failures_and_only_running_are_dropped() -> None:
    frames = [
        _task("WF_A", "SP_CLEAN", ["S", "S", "S", "S"]),   # never failed -> not a triage row
        _task("WF_A", "SP_ALLRUN", ["R", "R"]),            # nothing analyzable -> dropped
    ]
    assert task_failure_recurrence(pd.concat(frames, ignore_index=True)).empty


def test_ranking_worst_first() -> None:
    broken = _task("WF_A", "SP_BROKEN", ["F", "F", "F", "S"])       # High
    chronic = _task("WF_B", "SP_CHRONIC", ["S", "F", "F", "F", "F", "S"])  # Medium
    out = task_failure_recurrence(pd.concat([chronic, broken], ignore_index=True))
    assert list(out["TASK_NAME"]) == ["SP_BROKEN", "SP_CHRONIC"]


def test_empty_and_malformed_in_empty_out() -> None:
    cols = ["WORKFLOW_NAME", "TASK_NAME", "FAIL_STREAK", "RUNS_ANALYZED", "FAILS",
            "FAIL_RATE_PCT", "RECENCY_SCORE_PCT", "LATEST_FAILED", "LAST_FAILED_AT",
            "LOW_HISTORY", "VERDICT", "SEVERITY"]
    assert list(task_failure_recurrence(pd.DataFrame()).columns) == cols
    assert task_failure_recurrence(pd.DataFrame({"X": [1]})).empty
    assert task_failure_recurrence(None).empty  # type: ignore[arg-type]
