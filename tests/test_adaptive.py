"""Adaptive-compute candidacy scorer (repo review wave 3).

Key semantics locked here (adversarial review 2026-08-17): the hourly feed is
SPARSE — WAREHOUSE_METERING_HISTORY emits no row for a suspended hour — so the
scorer divides the metered credit sum by a FULL 24 hours, not by active hours.
That is what makes a nightly-batch (maximally bursty) warehouse read as bursty
instead of flat. The production-shaped test below feeds only active hours."""

from __future__ import annotations

import pandas as pd

from app.logic.adaptive import adaptive_compute_candidacy, candidacy_summary


def _hourly(wh, hour_credits: dict):
    """Rows for one warehouse from {hour_of_day: avg_credits} — ONLY the hours
    given (matches the sparse metering feed; suspended hours are simply absent)."""
    return pd.DataFrame({"WAREHOUSE_NAME": [wh] * len(hour_credits),
                         "HOUR_OF_DAY": list(hour_credits.keys()),
                         "AVG_CREDITS": list(hour_credits.values())})


def test_nightly_batch_sparse_frame_scores_strong():
    # Runs 3h at 20 cr, SUSPENDED the other 21h -> only 3 rows exist. Maximally
    # bursty; must score Strong (the review's #1 crux — the old metered-hours mean
    # scored this identical-to-flat at ~0).
    scored = adaptive_compute_candidacy(_hourly("WH_BATCH", {1: 20.0, 2: 20.0, 3: 20.0}))
    row = scored.iloc[0]
    assert row["VERDICT"] == "Strong candidate" and row["SCORE"] >= 60
    assert row["DAILY_CREDITS"] == 60.0                 # sum unaffected by sparseness
    assert row["PEAK_TO_MEAN"] == 8.0                   # 20 / (60/24)


def test_bursty_daytime_peak_scores_strong():
    prof = {**dict.fromkeys(range(8, 18), 3.0), 12: 18.0, 13: 18.0, 14: 18.0}
    scored = adaptive_compute_candidacy(_hourly("WH_BURSTY", prof))
    row = scored.iloc[0]
    assert row["VERDICT"] == "Strong candidate" and row["SCORE"] >= 60


def test_flat_allday_load_is_not_a_candidate():
    scored = adaptive_compute_candidacy(_hourly("WH_FLAT", dict.fromkeys(range(24), 5.0)))
    row = scored[scored["WAREHOUSE_NAME"] == "WH_FLAT"].iloc[0]
    assert row["VERDICT"] == "Not bursty enough" and row["SCORE"] < 35
    assert abs(row["PEAK_TO_MEAN"] - 1.0) < 0.05


def test_trivial_volume_scores_low_even_if_bursty():
    # A spiky shape but ~2 cr/day: the raised volume gate (10) keeps it low (review #3).
    scored = adaptive_compute_candidacy(_hourly("WH_TINY", {1: 0.02, 2: 1.8, 3: 0.02}))
    row = scored[scored["WAREHOUSE_NAME"] == "WH_TINY"].iloc[0]
    assert row["SCORE"] < 35


def test_heavy_idle_overrides_to_auto_suspend_first():
    # Bursty AND 70% idle: idle is the dominant waste, so the verdict is overridden
    # to auto-suspend regardless of the burst score (review #2).
    prof = {**dict.fromkeys(range(8, 18), 0.5), 12: 9.0, 13: 10.0, 14: 9.0}
    idle = pd.DataFrame({"WAREHOUSE_NAME": ["WH_IDLE"],
                         "TOTAL_CREDITS": [1000.0], "IDLE_CREDITS": [700.0]})
    scored = adaptive_compute_candidacy(_hourly("WH_IDLE", prof), idle)
    row = scored[scored["WAREHOUSE_NAME"] == "WH_IDLE"].iloc[0]
    assert float(row["IDLE_PCT"]) == 70.0
    assert row["VERDICT"] == "Auto-suspend first"       # override, not "Strong candidate"


def test_ranks_and_summary_and_empty_safe():
    df = pd.concat([
        _hourly("WH_WEAK", dict.fromkeys(range(24), 1.0)),
        _hourly("WH_STRONG", {1: 20.0, 2: 20.0, 3: 20.0}),
    ])
    scored = adaptive_compute_candidacy(df)
    assert list(scored["WAREHOUSE_NAME"]) == ["WH_STRONG", "WH_WEAK"]   # score desc
    k = candidacy_summary(scored)
    assert k["warehouses"] == 2 and k["strong"] >= 1
    assert adaptive_compute_candidacy(None).empty
    assert adaptive_compute_candidacy(pd.DataFrame()).empty
    assert candidacy_summary(pd.DataFrame())["warehouses"] == 0
