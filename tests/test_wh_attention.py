"""rec5 (v4.585.0) — the Warehouses attention-ranked opener's pure merge helper
(anomaly.warehouse_attention_ranking). Unit-tests the ranking/merge logic so the
"lead with what's wrong" opener stays honest and empty-safe."""

from __future__ import annotations

import pandas as pd

from app.logic.anomaly import warehouse_attention_ranking


def _anoms(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "USD", "Z_SCORE"])


def _peaks(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "PEAK_QUEUED"])


def test_merges_both_signals_and_queueing_sorts_above_pure_spend() -> None:
    anomalies = _anoms([("WH_SPEND", 900.0, 6.2), ("WH_SPEND", 800.0, 4.0)])
    peaks = _peaks([("WH_QUEUE", 3.4)])
    out = warehouse_attention_ranking(anomalies, peaks)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_QUEUE", "WH_SPEND"]   # queueing first
    spend_row = out[out["WAREHOUSE_NAME"] == "WH_SPEND"].iloc[0]
    assert spend_row["WORST_Z"] == 6.2                              # max |z|
    assert int(spend_row["ANOM_DAYS"]) == 2
    assert spend_row["ANOM_USD"] == 1700.0                          # summed anomalous-day spend
    assert pd.isna(spend_row["PEAK_QUEUED"])                        # no queueing signal for it
    queue_row = out[out["WAREHOUSE_NAME"] == "WH_QUEUE"].iloc[0]
    assert queue_row["PEAK_QUEUED"] == 3.4 and pd.isna(queue_row["WORST_Z"])


def test_a_warehouse_with_both_signals_carries_both_in_reason() -> None:
    out = warehouse_attention_ranking(
        _anoms([("WH_HOT", 500.0, -5.0)]), _peaks([("WH_HOT", 2.0)]))
    assert len(out) == 1
    reason = out.iloc[0]["REASON"]
    assert "spend anomaly z=5.0" in reason and "queued ~2.0 sustained" in reason


def test_below_floor_queueing_is_dropped() -> None:
    # PEAK_QUEUED below the floor (default 1.0) is not "what's wrong"
    out = warehouse_attention_ranking(_anoms([]), _peaks([("WH_QUIET", 0.4)]))
    assert out.empty


def test_empty_inputs_and_none_peaks_return_empty() -> None:
    assert warehouse_attention_ranking(_anoms([]), None).empty
    assert warehouse_attention_ranking(pd.DataFrame(), None).empty
    # missing columns must not raise
    assert warehouse_attention_ranking(pd.DataFrame({"X": [1]}), pd.DataFrame({"Y": [2]})).empty


def test_columns_are_stable_and_carry_no_duration_suffix() -> None:
    out = warehouse_attention_ranking(_anoms([("WH_A", 100.0, 4.0)]), _peaks([("WH_B", 2.0)]))
    assert list(out.columns) == [
        "WAREHOUSE_NAME", "WORST_Z", "ANOM_DAYS", "ANOM_USD", "PEAK_QUEUED", "REASON"]
    # counts + dollars, never durations -> no _SEC/_MS/_S suffix obligation
    assert not any(c.endswith(("_SEC", "_MS", "_S", "_MIN")) for c in out.columns)
