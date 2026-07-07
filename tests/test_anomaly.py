import pandas as pd

from app.logic.anomaly import anomaly_summary, flag_anomalies, robust_zscores


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
