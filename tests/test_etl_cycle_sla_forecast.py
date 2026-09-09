"""ETL whole-cycle SLA finish forecast (insights.etl_cycle_sla_forecast + _parse_hhmm).

Covers the cross-midnight deadline + margin, the tier ladder (On track / Trending / Missed
target / Breaching), the Theil-Sen margin trend + nights-to-breach, start-drift, the failed /
short-history / empty edges, and the HH:MM parser.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from app.logic.insights import _parse_hhmm, etl_cycle_sla_forecast


def _night(d: str, finish_hhmm: str, *, start_hhmm: str = "22:00",
           failed: int = 0, running: int = 0) -> dict:
    """One cycle night: starts on date D at start_hhmm, finishes on D+1 at finish_hhmm."""
    d0 = pd.Timestamp(d).normalize()
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    fh, fm = (int(x) for x in finish_hhmm.split(":"))
    start = d0 + timedelta(hours=sh, minutes=sm)
    finish = (d0 + timedelta(days=1)) + timedelta(hours=fh, minutes=fm)
    return {"CYCLE_DATE": d0, "CYCLE_START": start, "CYCLE_FINISH": finish,
            "N_FAILED": failed, "N_RUNNING": running, "SNAPSHOT_TS": finish}


def _df(nights: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(nights)


def test_parse_hhmm() -> None:
    assert _parse_hhmm("07:00", (0, 0)) == (7, 0)
    assert _parse_hhmm("08:30", (0, 0)) == (8, 30)
    assert _parse_hhmm("7:5", (0, 0)) == (7, 5)
    for bad in ("", "7", "25:00", "07:99", "ab:cd", None, 700):
        assert _parse_hhmm(bad, (7, 0)) == (7, 0)


def test_cross_midnight_margin_exact() -> None:
    # start 22:00 D, finish 02:47 D+1, deadline 07:00 D+1 -> margin = 4h13m = 15180s
    fc = etl_cycle_sla_forecast(_df([_night("2026-09-01", "02:47")]))
    assert fc["latest_margin_sec"] == 15180.0
    assert fc["latest_state"] == "COMPLETE"


def test_on_track_stable() -> None:
    nights = [_night(f"2026-09-0{i}", "05:00") for i in range(1, 7)]  # ~2h early, flat
    fc = etl_cycle_sla_forecast(_df(nights))
    assert fc["forecast"] in ("On track", "On track (short history)")
    assert fc["severity"] == "OK"
    assert fc["latest_margin_sec"] == 7200.0            # 07:00 - 05:00
    assert fc["nights_to_breach"] is None


def test_trending_to_miss_with_start_drift() -> None:
    # both start AND finish slip 15 min/night: margin erodes, start drifts later
    nights = []
    for i in range(6):
        sh, sm = divmod(22 * 60 + i * 15, 60)
        fh, fm = divmod(5 * 60 + i * 15, 60)
        nights.append(_night(f"2026-09-0{i + 1}", f"{fh:02d}:{fm:02d}", start_hhmm=f"{sh:02d}:{sm:02d}"))
    fc = etl_cycle_sla_forecast(_df(nights))
    assert fc["forecast"] == "Trending to miss"
    assert fc["severity"] == "Medium"
    assert fc["slope_sec_per_night"] == -900.0          # 15 min/night later
    assert fc["start_slope_min_per_night"] == 15.0      # starting 15 min/night later
    assert fc["nights_to_breach"] is not None and fc["nights_to_breach"] >= 1


def test_missed_target_but_before_hard() -> None:
    # latest complete cycle finishes 07:30 -> after 07:00 target, before 08:00 hard
    fc = etl_cycle_sla_forecast(_df([_night("2026-09-01", "07:30")]))
    assert fc["forecast"] == "Missed target"
    assert fc["severity"] == "High"
    assert fc["latest_margin_sec"] == -1800.0           # 30 min late vs target
    assert fc["margin_hard_sec"] == 1800.0              # 30 min early vs hard


def test_breaching_after_hard_deadline() -> None:
    fc = etl_cycle_sla_forecast(_df([_night("2026-09-01", "08:30")]))
    assert fc["forecast"] == "Breaching"
    assert fc["severity"] == "High"
    assert fc["margin_hard_sec"] == -1800.0             # 30 min past the 08:00 hard deadline


def test_failed_latest_uses_last_complete_and_flags() -> None:
    nights = [_night("2026-09-01", "05:00"), _night("2026-09-02", "05:00", failed=1)]
    fc = etl_cycle_sla_forecast(_df(nights))
    assert fc["latest_failed"] is True                  # newest cycle's terminal failed
    assert fc["latest_margin_sec"] == 7200.0            # judged on the last CLEAN cycle


def test_all_failed_is_insufficient_history() -> None:
    nights = [_night("2026-09-01", "05:00", failed=1), _night("2026-09-02", "05:00", failed=1)]
    fc = etl_cycle_sla_forecast(_df(nights))
    assert fc["forecast"] == "Insufficient history"
    assert fc["severity"] == "Low"          # NOT "OK" -> the panel must not render a green all-clear
    assert fc["nights_fit"] == 0


def test_latest_complete_finish_tracks_last_clean_cycle() -> None:
    # newest night FAILED -> the KPI finish must be the last CLEAN cycle's finish, never the failed one
    nights = [_night("2026-09-01", "05:00"), _night("2026-09-02", "05:30", failed=1)]
    fc = etl_cycle_sla_forecast(_df(nights))
    assert fc["latest_complete_finish"] is not None
    assert pd.Timestamp(fc["latest_complete_finish"]) == pd.Timestamp("2026-09-02 05:00")


def test_tz_aware_snapshot_with_incomplete_latest_does_not_raise() -> None:
    # SNAPSHOT_TS is TIMESTAMP_LTZ (tz-aware) on live Snowflake while cycle timestamps are naive;
    # mixing them in the live-runway subtraction must NOT TypeError (newest night in-flight).
    done = _night("2026-09-01", "05:00")
    running = _night("2026-09-02", "05:00", running=1)
    running["CYCLE_FINISH"] = pd.NaT
    for nt in (done, running):
        nt["SNAPSHOT_TS"] = pd.Timestamp("2026-09-03 06:30", tz="America/Chicago")
    fc = etl_cycle_sla_forecast(_df([done, running]))
    assert fc["ok"] is True
    assert fc["latest_state"] == "INCOMPLETE"
    assert fc["live_runway_sec"] == 1800.0   # 07:00 deadline − 06:30 snapshot, computed not crashed


def test_tz_aware_start_and_finish_do_not_raise() -> None:
    n = _night("2026-09-01", "05:00")
    n["CYCLE_START"] = pd.Timestamp(n["CYCLE_START"], tz="America/Chicago")   # tz-aware start vs naive CYCLE_DATE
    n["CYCLE_FINISH"] = pd.Timestamp(n["CYCLE_FINISH"], tz="America/Chicago")
    fc = etl_cycle_sla_forecast(_df([n]))
    assert fc["ok"] is True
    assert fc["latest_margin_sec"] == 7200.0


def test_custom_deadline_keys() -> None:
    # a 06:00 target: a 05:00 finish is only 1h early; a 06:30 finish misses it
    early = etl_cycle_sla_forecast(_df([_night("2026-09-01", "05:00")]), target_hhmm="06:00", breach_hhmm="07:00")
    assert early["latest_margin_sec"] == 3600.0
    late = etl_cycle_sla_forecast(_df([_night("2026-09-01", "06:30")]), target_hhmm="06:00", breach_hhmm="07:00")
    assert late["forecast"] == "Missed target"


def test_empty_in_empty_out() -> None:
    assert etl_cycle_sla_forecast(pd.DataFrame()) == {}
    assert etl_cycle_sla_forecast(pd.DataFrame({"X": [1]})) == {}
    assert etl_cycle_sla_forecast(None) == {}  # type: ignore[arg-type]
