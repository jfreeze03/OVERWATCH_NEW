import pandas as pd

from app.logic.anomaly import (
    anomaly_markers,
    anomaly_summary,
    flag_anomalies,
    robust_zscores,
)


def test_anomaly_markers_one_row_per_flagged_day_with_entity_labels():
    df = pd.DataFrame({
        "DAY": ["2026-08-01", "2026-08-01", "2026-08-02", "2026-08-03"],
        "WAREHOUSE_NAME": ["WH_A", "WH_B", "WH_A", "WH_C"],
        "IS_ANOMALY": [True, True, False, True],
        "Z_SCORE": [5.0, 4.0, 1.0, 6.0],
    })
    out = anomaly_markers(df, "DAY", "WAREHOUSE_NAME")
    assert list(out["DAY"]) == ["2026-08-01", "2026-08-03"]   # 08-02 not flagged
    assert {"WH_A", "WH_B"} <= set(out.iloc[0]["LABEL"].split(", "))
    assert out.iloc[1]["LABEL"] == "WH_C"


def test_anomaly_markers_count_label_and_empty_cases():
    df = pd.DataFrame({"DAY": ["2026-08-01", "2026-08-01"],
                       "IS_ANOMALY": [True, True], "Z_SCORE": [5.0, 4.0]})
    out = anomaly_markers(df, "DAY")                     # no label_col -> count
    assert list(out["DAY"]) == ["2026-08-01"] and out.iloc[0]["LABEL"] == "2 anomalies"
    # nothing flagged / empty / missing column -> empty frame (overlay draws nothing)
    assert anomaly_markers(pd.DataFrame({"DAY": ["2026-08-01"], "IS_ANOMALY": [False]}), "DAY").empty
    assert anomaly_markers(pd.DataFrame(), "DAY").empty
    assert anomaly_markers(pd.DataFrame({"DAY": ["x"]}), "DAY").empty  # no IS_ANOMALY


def test_spike_is_flagged():
    values = pd.Series([10, 11, 9, 10, 10, 12, 10, 95])
    scores = robust_zscores(values)
    assert abs(scores.iloc[-1]) > 3.5
    assert abs(scores.iloc[0]) < 2


def test_short_series_never_flags():
    assert robust_zscores(pd.Series([1, 100])).abs().max() == 0.0


def test_constant_series_no_flags():
    assert robust_zscores(pd.Series([5.0] * 10)).abs().max() == 0.0


def test_mad_zero_falls_back_to_mean_abs_deviation():
    # >50% identical values collapses MAD; meanAD fallback must still flag the
    # spike (a std fallback would let the spike inflate its own baseline).
    values = pd.Series([10.0] * 8 + [200.0])
    scores = robust_zscores(values)
    assert scores.iloc[-1] > 3.5


def test_groupwise_baselines_are_independent():
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["BIG"] * 8 + ["SMALL"] * 8,
        "USD": [1000, 1010, 990, 1005, 995, 1002, 998, 1001,  # calm big
                5, 6, 5, 5, 6, 5, 5, 60],                      # small spikes
    })
    out = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME")
    assert bool(out.iloc[-1]["IS_ANOMALY"])
    assert not out[out["WAREHOUSE_NAME"] == "BIG"]["IS_ANOMALY"].any()


def test_summary_orders_strongest_first():
    df = pd.DataFrame({
        "W": ["A"] * 8 + ["B"] * 8,
        "USD": [10, 10, 11, 9, 10, 10, 10, 300, 5, 5, 5, 6, 5, 5, 5, 30],
    })
    flagged = flag_anomalies(df, "USD", group_col="W")
    rows = anomaly_summary(flagged, "W", "USD")
    assert rows and rows[0]["label"] == "A"


def test_empty_frame_safe():
    out = flag_anomalies(pd.DataFrame(), "USD")
    assert "IS_ANOMALY" in out.columns and out.empty


def test_min_value_gate_suppresses_trivial_dollar_spikes():
    # A usually-idle warehouse scores a huge z on any active day; the $ floor keeps
    # it from firing "investigate" on a trivial $49 (the screenshot false alarm).
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["IDLE"] * 20,
        "USD": [0.0] * 15 + [49.0, 41.0, 39.0, 38.0, 28.0],
    })
    assert flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME")["IS_ANOMALY"].any()
    gated = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME", min_value=50.0)
    assert not gated["IS_ANOMALY"].any()          # every spike day is under $50


def test_min_active_days_gate_requires_a_real_baseline():
    # A warehouse active on only a few days has no baseline to be an outlier of.
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["SPARSE"] * 20,
        "USD": [0.0] * 17 + [500.0, 400.0, 4000.0],   # 3 active days, real dollars
    })
    assert flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME",
                          min_value=50.0)["IS_ANOMALY"].any()
    gated = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME",
                           min_value=50.0, min_active_days=10)
    assert not gated["IS_ANOMALY"].any()          # only 3 active days < 10


def test_material_spike_on_a_real_baseline_still_flags():
    # The gates must not suppress a genuine anomaly: a warehouse active every day
    # with a real dollar spike still fires through both gates.
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["PROD"] * 15,
        "USD": [1000, 1010, 990, 1005, 995, 1002, 998, 1001, 1003, 997,
                1000, 1004, 996, 1002, 5000],
    })
    gated = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME",
                           min_value=50.0, min_active_days=10)
    assert bool(gated.iloc[-1]["IS_ANOMALY"])


def test_min_value_gate_keeps_collapse_on_a_material_baseline():
    # A ~$5000/day warehouse dropping to $10 is a material COLLAPSE (stalled
    # pipeline) even though the collapse day is < $50 — the floor must ride the
    # baseline for negative-z drops, not the day value. spend.py surfaces this as
    # "daily spend collapsed ... stalled workload / dead pipeline".
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["PROD"] * 15,
        "USD": [5000, 5010, 4990, 5005, 4995, 5002, 4998, 5001, 5003, 4997,
                5000, 5004, 4996, 5002, 10.0],   # last day collapses
    })
    gated = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME",
                           min_value=50.0, min_active_days=10)
    assert bool(gated.iloc[-1]["IS_ANOMALY"])     # collapse still flags
    assert gated.iloc[-1]["Z_SCORE"] < 0          # ...as a negative-z drop


def test_trivial_warehouse_collapse_stays_suppressed():
    # A warehouse whose baseline is trivial ($2-3/day) dropping to $0 is noise,
    # not a stalled pipeline — the baseline is under the floor.
    df = pd.DataFrame({
        "WAREHOUSE_NAME": ["IDLE"] * 15,
        "USD": [2, 2, 3, 2, 2, 3, 2, 2, 3, 2, 2, 3, 2, 2, 0.0],
    })
    gated = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME",
                           min_value=50.0, min_active_days=10)
    assert not gated["IS_ANOMALY"].any()
