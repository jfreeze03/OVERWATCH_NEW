"""R1-084: the Held? failure rule never reads a week with no runs as a measured 0% failure rate.

A failing task that was suspended, auto-suspended or unscheduled after its item was marked done has no
MART_TASK_NODE_DAILY rows since (the mart only holds days with SUCCEEDED / FAILED runs), and LOADED_THROUGH is
mart-wide, so the stalled-loader guard does not see it. Each empty trailing week was a "0 of 0 = 0%" fix, and
the item read "Held N days" on Action Center / Entity 360 -- a broken task merely switched off shown as a
verified fix. A week with no run now carries no rate: no run since done is "Not measurable".
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app.logic import fix_queue, outcomes
from app.logic.outcomes import action_held, held_basis

TODAY = date(2026, 9, 29)
DONE = date(2026, 9, 1)
_YESTERDAY = TODAY - timedelta(days=1)


def _rows(kind: str, key: str, series: dict) -> list[dict]:
    return [{"ENTITY_TYPE": kind, "ENTITY_KEY_U": key, "DAY": day, "CREDITS": v.get("c"), "P95_SEC": v.get("p"),
             "RUNS": v.get("r"), "FAILS": v.get("f"), "LOADED_THROUGH": _YESTERDAY} for day, v in series.items()]


def _with_a_healthy_neighbour(kind: str, key: str, series: dict) -> pd.DataFrame:
    """The entity's rows plus another entity of the same kind loaded through yesterday (a fresh mart)."""
    other = {TODAY - timedelta(days=i): {"r": 10.0, "f": 0.0, "p": 5.0, "c": 1.0} for i in range(1, 60)}
    return pd.DataFrame(_rows(kind, key, series) + _rows(kind, "DB.S.OTHER", other))


def _failing_before_only() -> dict:
    return {DONE - timedelta(days=i): {"r": 2.0, "f": 1.0, "p": 60.0} for i in range(1, 29)}


@pytest.mark.parametrize("source", [fix_queue.TRIAGE_TRACK_SOURCE, "Action Center", ""])
def test_a_failing_task_with_no_runs_since_done_is_not_measurable_never_held(source):
    daily = _with_a_healthy_neighbour("TASK", "DB.S.BROKEN", _failing_before_only())
    r = action_held("TASK", "DB.S.BROKEN", DONE, daily, TODAY, source=source)
    assert r["state"] == outcomes.NOT_MEASURABLE and r["label"] == outcomes.NOT_MEASURABLE_LABEL
    assert r["state"] not in outcomes.OVERRIDES_COOLDOWN          # same cooldown effect as before


def test_a_failing_family_that_disappears_is_not_measurable():
    before = {DONE - timedelta(days=i): {"r": 100.0, "f": 10.0, "c": 2.0} for i in range(1, 29)}
    daily = _with_a_healthy_neighbour("QUERY_FINGERPRINT", "FP_GONE", before)
    assert action_held("QUERY_FINGERPRINT", "FP_GONE", DONE, daily, TODAY)["state"] == outcomes.NOT_MEASURABLE


def test_a_sparse_task_still_failing_since_done_is_not_fixed_not_held():
    # failed its only run before (Aug 20) and its only run after (Sep 4); the empty weeks around the Sep 4
    # run used to read 0% and turn a 100%-failing task into "Held"
    ser = {date(2026, 8, 20): {"r": 1.0, "f": 1.0, "p": 30.0}, date(2026, 9, 4): {"r": 1.0, "f": 1.0, "p": 30.0}}
    r = action_held("TASK", "DB.S.SPARSE", DONE, _with_a_healthy_neighbour("TASK", "DB.S.SPARSE", ser), TODAY)
    assert r["state"] == outcomes.NOT_FIXED
    assert held_basis(r, 3.68).endswith("; 1 of 1 since (100%)")


def test_a_recent_completion_is_still_too_early():
    done = TODAY - timedelta(days=4)
    r = action_held("TASK", "DB.S.BROKEN", done, _with_a_healthy_neighbour("TASK", "DB.S.BROKEN", {
        done - timedelta(days=i): {"r": 2.0, "f": 1.0, "p": 60.0} for i in range(1, 29)}), TODAY)
    assert r["state"] == outcomes.TOO_EARLY and r["label"] == "Too early (3 of 7 days)"


def test_a_task_that_runs_clean_since_done_still_holds():
    ser = _failing_before_only()
    ser.update({DONE + timedelta(days=i): {"r": 2.0, "f": 0.0, "p": 60.0} for i in range(1, 28)})
    r = action_held("TASK", "DB.S.FIXED", DONE, _with_a_healthy_neighbour("TASK", "DB.S.FIXED", ser), TODAY)
    assert r["state"] == outcomes.HELD and r["label"] == "Held 27 days"
