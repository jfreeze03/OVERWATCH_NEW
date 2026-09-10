"""ETL runtime-creep forecaster (insights.etl_runtime_creep).

Covers the Theil-Sen trend fit, the projection + runs-to-2x math, the materiality /
history / triviality gates, robustness to a single spike, and empty-in/empty-out.
"""

from __future__ import annotations

import pandas as pd

from app.logic.insights import etl_runtime_creep


def _series(wf: str, task: str, runtimes: list[float]) -> pd.DataFrame:
    """Rows for one task; ``runtimes`` is oldest→newest, so RN counts down to 1 at the newest."""
    n = len(runtimes)
    return pd.DataFrame(
        {
            "WORKFLOW_NAME": [wf] * n,
            "TASK_NAME": [task] * n,
            # RN=1 is the newest run; the oldest runtime gets the highest RN
            "RN": list(range(n, 0, -1)),
            "RUNTIME_SEC": runtimes,
        }
    )


def test_creep_flags_a_rising_task_with_correct_projection() -> None:
    df = _series("WF_A", "SP_RISING", [100, 110, 120, 130, 140, 150])
    out = etl_runtime_creep(df)
    assert len(out) == 1
    r = out.iloc[0]
    assert r["TASK_NAME"] == "SP_RISING"
    assert r["RUNS"] == 6
    assert r["SLOPE_SEC_PER_RUN"] == 10.0          # Theil-Sen slope of a clean +10/run line
    assert r["LATEST_SEC"] == 150.0
    assert r["BASELINE_SEC"] == 125.0              # median(100..150)
    assert r["PROJECTED_SEC"] == 220.0             # 150 + 10*7 (default horizon)
    assert r["RUNS_TO_2X"] == 10                   # ceil((250 - 150) / 10)


def test_creep_ignores_flat_declining_short_and_thin_history() -> None:
    frames = [
        _series("WF_A", "SP_FLAT", [120, 120, 120, 120, 120, 120]),      # slope 0
        _series("WF_A", "SP_FALLING", [150, 140, 130, 120, 110, 100]),   # slope -10 (improving)
        _series("WF_A", "SP_TINY", [5, 6, 7, 8, 9, 10]),                 # rising but < min latest
        _series("WF_A", "SP_THIN", [100, 140, 200]),                     # only 3 runs (< min_runs)
    ]
    out = etl_runtime_creep(pd.concat(frames, ignore_index=True))
    assert out.empty


def test_creep_is_robust_to_a_single_spike() -> None:
    # a lone spike at the newest run must NOT read as a trend (Theil-Sen median slope stays 0)
    df = _series("WF_A", "SP_SPIKE", [100, 100, 100, 100, 100, 300])
    assert etl_runtime_creep(df).empty


def test_creep_n4_single_spike_not_flagged() -> None:
    # exactly 4 runs with a lone newest spike: at n=4 half the Theil-Sen pairwise slopes involve the
    # spike so the median falsely clears the gate -> the min_runs=5 floor must reject it (regression).
    df = _series("WF_A", "SP_N4", [120, 120, 120, 600])
    assert etl_runtime_creep(df).empty


def test_creep_already_doubled_reports_zero_runs_to_2x() -> None:
    # an ACCELERATING task whose latest is already >= 2x its baseline median -> RUNS_TO_2X
    # clamps to 0 (already there), and it is still flagged (material upward slope).
    df = _series("WF_A", "SP_HOT", [100, 100, 100, 100, 200, 500])       # baseline median 100
    out = etl_runtime_creep(df)
    assert len(out) == 1
    assert out.iloc[0]["BASELINE_SEC"] == 100.0
    assert out.iloc[0]["LATEST_SEC"] == 500.0                            # 500 >= 2 * 100
    assert out.iloc[0]["RUNS_TO_2X"] == 0


def test_creep_empty_and_malformed_in_empty_out() -> None:
    cols = ["WORKFLOW_NAME", "TASK_NAME", "RUNS", "LATEST_SEC", "BASELINE_SEC",
            "SLOPE_SEC_PER_RUN", "PROJECTED_SEC", "RUNS_TO_2X"]
    assert list(etl_runtime_creep(pd.DataFrame()).columns) == cols
    assert etl_runtime_creep(pd.DataFrame({"X": [1]})).empty
    assert etl_runtime_creep(None).empty  # type: ignore[arg-type]


def test_creep_sorted_steepest_first() -> None:
    steep = _series("WF_A", "SP_STEEP", [100, 130, 160, 190, 220, 250])   # +30/run
    mild = _series("WF_B", "SP_MILD", [100, 108, 116, 124, 132, 140])      # +8/run
    out = etl_runtime_creep(pd.concat([mild, steep], ignore_index=True))
    assert list(out["TASK_NAME"]) == ["SP_STEEP", "SP_MILD"]
