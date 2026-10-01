"""rec5 (v4.585.0) — the Warehouses attention-ranked opener's pure merge helper
(anomaly.warehouse_attention_ranking). Unit-tests the ranking/merge logic so the
"lead with what's wrong" opener stays honest and empty-safe."""

from __future__ import annotations

import pandas as pd

from app.logic.anomaly import warehouse_attention_ranking


def _anoms(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "USD", "Z_SCORE"])


def _peaks(rows: list[tuple[str, float]], intervals: int = 168) -> pd.DataFrame:
    # the builder's real shape (ops_sql.warehouse_concurrency_peaks): R1-074 gates "sustained" on
    # QUEUED_INTERVALS (a count over the whole 14-day read), so a default fixture models a warehouse
    # that queued ~1h/day: 168 five-minute intervals over 14 days, over the 30 min/day bar
    df = pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "PEAK_QUEUED"])
    df["QUEUED_INTERVALS"] = intervals
    df["INTERVALS"] = 4000
    return df


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
    # R1-074 added QUEUED_INTERVALS (the count behind "sustained") between PEAK_QUEUED and REASON
    assert list(out.columns) == [
        "WAREHOUSE_NAME", "WORST_Z", "ANOM_DAYS", "ANOM_USD", "PEAK_QUEUED", "QUEUED_INTERVALS", "REASON"]
    # counts + dollars, never durations -> no _SEC/_MS/_S suffix obligation
    assert not any(c.endswith(("_SEC", "_MS", "_S", "_MIN")) for c in out.columns)


def test_a_one_interval_burst_is_not_sustained_and_never_outranks_spend() -> None:
    """R1-074: PEAK_QUEUED is a single-interval MAX. A 5-minute burst (1 queued interval of 4000)
    used to read "queued ~1.0 sustained" and sort above a z=12 $9,000 spend anomaly -- five such
    bursts pushed every real anomaly out of the opener's head(5)."""
    from app.logic.anomaly import sustained_queue_min_intervals

    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    bursts = pd.concat([_peaks([(f"WH_SANDBOX_{i}", 1.0)], intervals=1) for i in range(5)])
    out = warehouse_attention_ranking(anomalies, bursts)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD"]                # the bursts are not attention
    assert "sustained" not in " ".join(out["REASON"])
    # at the bar the queue signal is real again, sorts first and says how much per day
    held = _peaks([("WH_BUSY", 1.0)], intervals=sustained_queue_min_intervals())
    out = warehouse_attention_ranking(anomalies, held)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BUSY", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 14d)"
    assert int(out.iloc[0]["QUEUED_INTERVALS"]) == sustained_queue_min_intervals()


def test_scattered_bursts_across_the_14d_window_are_not_sustained() -> None:
    """Review r1 on R1-074: QUEUED_INTERVALS counts over the WHOLE 14-day read, so a flat floor of 6
    was ~30 min per 14 days (~2 min/day). Six separate 5-minute bursts on six different days (6 of
    4000 intervals) still read "queued ~1.0 sustained" and sorted above a z=12 $9,000 spend anomaly.
    Sustained is sizing's 30 min/day rate across the window: 30 x 14 / 5 = 84 intervals."""
    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    for n in (6, 83):           # six scattered bursts; one interval under 30 min/day over 14 days
        out = warehouse_attention_ranking(anomalies, _peaks([("WH_BURSTY", 1.0)], intervals=n))
        assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD"], n
        assert "sustained" not in " ".join(out["REASON"]), n
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_BURSTY", 1.0)], intervals=84))
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BURSTY", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 14d)"
    # the rate in the reason is per day of the window, humanized (never raw minutes)
    out = warehouse_attention_ranking(_anoms([]), _peaks([("WH_HOT", 4.0)], intervals=1000))
    assert out.iloc[0]["REASON"] == "queued ~4.0 sustained (~5h 57m/day over 14d)"


def test_the_sustained_bar_is_sizings_per_day_rate_scaled_to_the_window() -> None:
    from app.logic.anomaly import LOAD_INTERVAL_MIN, sustained_queue_min_intervals
    from app.logic.sizing import QUEUE_UP_MIN_PER_DAY

    assert sustained_queue_min_intervals(14) == 84 and sustained_queue_min_intervals(7) == 42
    for days in (1, 7, 14, 30, 90):
        n = sustained_queue_min_intervals(days)
        # the smallest whole count whose per-day queueing reaches sizing's bar
        assert n * LOAD_INTERVAL_MIN / days >= QUEUE_UP_MIN_PER_DAY
        assert (n - 1) * LOAD_INTERVAL_MIN / days < QUEUE_UP_MIN_PER_DAY
    # a caller reading a different window passes it, and the bar and the wording follow
    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_W", 1.0)], intervals=41), window_days=7)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD"]
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_W", 1.0)], intervals=42), window_days=7)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_W", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 7d)"


def test_the_opener_reads_peaks_over_the_window_the_bar_is_scaled_to() -> None:
    # The opener calls warehouse_attention_ranking with the default window_days, so the window its
    # peaks read uses must BE that default: a 30-day read judged against a 14-day bar would call
    # half the real rate "sustained". Change both together (or pass window_days=) if it moves.
    import re
    from pathlib import Path

    from app.logic.anomaly import ATTENTION_PEAKS_WINDOW_DAYS

    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "operations.py").read_text(
        encoding="utf-8")
    body = src.split("def _wh_activity_anomalies(", 1)[1].split("\ndef ", 1)[0]
    assert "warehouse_attention_ranking(" in body
    windows = re.findall(r"warehouse_concurrency_peaks\(\s*(\d+)\s*,", body)
    assert windows, "the opener's peaks read moved; re-point this lock"
    assert {int(w) for w in windows} == {ATTENTION_PEAKS_WINDOW_DAYS}


def test_a_peak_without_an_interval_count_is_only_a_peak() -> None:
    # an older frame shape (no QUEUED_INTERVALS): the peak is shown, never called sustained,
    # never sorted above a spend anomaly
    peaks = pd.DataFrame({"WAREHOUSE_NAME": ["WH_OLDSHAPE"], "PEAK_QUEUED": [3.0]})
    out = warehouse_attention_ranking(_anoms([("WH_PROD", 900.0, 5.0)]), peaks)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD", "WH_OLDSHAPE"]
    assert out.iloc[1]["REASON"] == "peak queued ~3.0"
