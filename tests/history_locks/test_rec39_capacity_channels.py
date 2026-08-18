"""rec#39: capacity forecasts separate the queue/spill channels and gate growth
on a recent sub-window, not head-vs-tail of the whole history.
"""

from datetime import date, timedelta

import pandas as pd

from app.logic.capacity import _growth_pct, capacity_forecasts


def test_growth_reads_a_recent_subwindow_not_head_vs_tail():
    # Demand ramped for 60 days then went flat for 60. Head-vs-tail over the full
    # series reads huge growth; last-30 vs prior-30 correctly reads ~flat.
    ramp_then_flat = pd.Series([100 + min(i, 60) * 5 for i in range(120)])
    assert _growth_pct(ramp_then_flat) < 1.0          # both recent windows sit on the plateau
    # a genuinely-recent climb still registers
    climbing = pd.Series([100 + i * 5 for i in range(120)])
    assert _growth_pct(climbing) > 20.0


def test_capacity_growth_gate_ignores_stale_historical_growth():
    as_of = date(2026, 7, 31)
    dates = pd.date_range(as_of - timedelta(days=120), periods=120, freq="D")
    rows = [{
        "DAY": day, "WAREHOUSE_NAME": "WH_FLAT", "COMPANY": "ALFA",
        "QUERY_COUNT": 100 + min(i, 60) * 5,          # grew for 60d, flat since
        "QUEUED_MIN": 6.0, "SPILL_REMOTE_GB": 0.0,
        "CREDITS_TOTAL": (100 + min(i, 60) * 5) * 0.1,
        "CHANGE_EVENTS": 0,
    } for i, day in enumerate(dates)]
    row = capacity_forecasts(pd.DataFrame(rows), as_of=as_of).iloc[0]
    # the recent-window growth is ~0, so the historical ramp no longer corroborates
    assert abs(row["WORKLOAD_GROWTH_PCT"]) < 5.0


def test_spill_channel_drives_eta_when_queue_is_flat():
    # Queue is flat and low; remote spill rises to its own 1 GB/day line. The ETA
    # and the basis must be attributed to the SPILL channel, not a max()-composite.
    as_of = date(2026, 7, 31)
    dates = pd.date_range(as_of - timedelta(days=60), periods=60, freq="D")
    rows = [{
        "DAY": day, "WAREHOUSE_NAME": "WH_SPILL", "COMPANY": "ALFA",
        "QUERY_COUNT": 100 + i * 3, "QUEUED_MIN": 3.0,           # flat ~0.1 normalized
        "SPILL_REMOTE_GB": 0.10 + 0.011 * i,                     # climbs toward 1.0
        "CREDITS_TOTAL": 10.0 + i * 0.2, "CHANGE_EVENTS": 0,
    } for i, day in enumerate(dates)]
    row = capacity_forecasts(pd.DataFrame(rows), as_of=as_of).iloc[0]
    assert row["STATUS"] == "FORECAST"
    assert "spill" in row["BASIS"].lower()
    assert 0 < row["DAYS_TO_PRESSURE"] <= 180
    assert row["ETA_LOW_DAYS"] <= row["DAYS_TO_PRESSURE"] <= row["ETA_HIGH_DAYS"]


def test_crossing_channels_do_not_yield_a_nonphysical_eta():
    # Queue high early then fades; spill low early then climbs. The old max()-
    # composite fit one slope across the crossover; the driver ETA must instead
    # come from the spill channel's own slope and be within the horizon.
    as_of = date(2026, 7, 31)
    dates = pd.date_range(as_of - timedelta(days=60), periods=60, freq="D")
    rows = []
    for i, day in enumerate(dates):
        queue_min = max(0.0, 24.0 - 0.3 * i)          # 0.8 -> ~0.2 normalized, fading
        spill_gb = 0.05 + 0.012 * i                   # climbing toward 1.0
        rows.append({
            "DAY": day, "WAREHOUSE_NAME": "WH_CROSS", "COMPANY": "ALFA",
            "QUERY_COUNT": 100 + i * 3, "QUEUED_MIN": queue_min,
            "SPILL_REMOTE_GB": spill_gb,
            "CREDITS_TOTAL": 10.0 + i * 0.2, "CHANGE_EVENTS": 0,
        })
    row = capacity_forecasts(pd.DataFrame(rows), as_of=as_of).iloc[0]
    # spill is the only rising channel, so it must drive; slope is spill's (positive)
    assert row["STATUS"] in {"FORECAST", "WATCH"}
    if row["STATUS"] == "FORECAST":
        assert "spill" in row["BASIS"].lower()
        assert row["SLOPE_PER_DAY"] > 0
