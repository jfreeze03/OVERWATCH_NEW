"""Next-Fifty #18 (v4.588.0): the nightly-cycle SLA-finish forecast fits its slope over CALENDAR nights
(a failed night leaves a gap instead of compressing the axis — the CALENDAR-DAY-SLOPE class), excludes
EXPECTED_SPIKE_CALENDAR nights (month/quarter-end) from the trend FIT while still judging them for an
actual miss, and warns before a known-heavy night using how much later past labelled nights finished."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd

from app.data import etl_control_sql as etl
from app.logic.insights import etl_cycle_sla_forecast
from app.ui.pages.brief import _nightly_cycle_kpi

_ROOT = Path(__file__).resolve().parents[1]


def _night(d: str, finish_hhmm: str, *, failed: int = 0, running: int = 0) -> dict:
    """A cycle night starting on D at 22:00 and finishing on D+1 at finish_hhmm."""
    d0 = pd.Timestamp(d).normalize()
    fh, fm = (int(x) for x in finish_hhmm.split(":"))
    finish = d0 + timedelta(days=1, hours=fh, minutes=fm)
    return {"CYCLE_DATE": d0, "CYCLE_START": d0 + timedelta(hours=22), "CYCLE_FINISH": finish,
            "N_FAILED": failed, "N_RUNNING": running, "SNAPSHOT_TS": finish}


def _days(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


def test_failed_night_gap_uses_calendar_slope():
    # each CALENDAR night finishes 10 min later; 09-04 failed. Per calendar night that is -600 s of margin.
    # A row-index fit would compress the gap and overstate the drift (~-720 s/night).
    finishes = {"2026-09-01": "05:00", "2026-09-02": "05:10", "2026-09-03": "05:20",
                "2026-09-05": "05:40", "2026-09-06": "05:50", "2026-09-07": "06:00"}
    rows = [_night(d, f) for d, f in finishes.items()] + [_night("2026-09-04", "05:30", failed=1)]
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows))
    assert fc["slope_sec_per_night"] == -600.0
    assert fc["nights_fit"] == 6


def test_month_end_night_excluded_from_fit_not_from_tier():
    rows = [_night(d, "05:00") for d in _days("2026-08-25", "2026-08-30")] + [_night("2026-08-31", "06:50")]
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar="MONTH_END:1")
    assert fc["forecast"] == "On track"
    assert fc["spike_nights_excluded"] == 1 and fc["nights_fit"] == 6
    assert fc["nights"][0]["EXPECTED_SPIKE"] == "month-end"        # newest-first table row (08-31)
    assert etl_cycle_sla_forecast(pd.DataFrame(rows))["nights_fit"] == 7   # no calendar -> it is fitted
    # a labelled night that ACTUALLY misses is still a miss — the calendar never hides a real breach
    late = [*rows[:-1], _night("2026-08-31", "07:30")]
    assert etl_cycle_sla_forecast(pd.DataFrame(late), spike_calendar="MONTH_END:1")["forecast"] == "Missed target"


def test_upcoming_month_end_warns_with_history():
    spikes = {"2026-07-31", "2026-08-01", "2026-08-31", "2026-09-01"}     # MONTH_END:1 night keys
    rows = [_night(d, "06:00" if d in spikes else "05:00") for d in _days("2026-07-25", "2026-09-29")]
    rows[-1]["SNAPSHOT_TS"] = pd.Timestamp("2026-09-30 10:00")
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar="MONTH_END:1")
    assert fc["upcoming_night_key"] == pd.Timestamp("2026-09-30")     # Sep 30 = the last day of September
    assert fc["upcoming_spike_label"] == "month-end"
    assert fc["spike_extra_sec"] == 3600.0                            # labelled nights finish 1h later
    assert fc["spike_nights_seen"] == 4


def test_fit_window_is_the_newest_n_but_history_is_longer():
    spikes = {"2026-08-31", "2026-09-01"}                             # both older than the newest 14
    rows = [_night(d, "06:00" if d in spikes else "05:00") for d in _days("2026-08-25", "2026-09-23")]
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar="MONTH_END:1")
    assert fc["nights_total"] == 14 and len(fc["nights"]) == 14
    assert fc["spike_nights_excluded"] == 0                           # none inside the fit window
    assert fc["spike_nights_seen"] == 2                               # but the history still counts them


def test_builder_returns_spike_history_and_fits_14():
    sql = etl.cycle_finish_history_scan("DB.S.CONTROL_STATUS", start_workflow="WF_A", end_workflow="WF_Z")
    assert f"QUALIFY ROW_NUMBER() OVER (ORDER BY s.CYCLE_DATE DESC) <= {etl.SLA_HISTORY_NIGHTS}" in sql
    assert etl.SLA_HISTORY_NIGHTS == 100 and etl.SLA_BASELINE_RUNS == 14   # > a quarter of nights


def test_brief_on_track_tile_names_a_month_end_night():
    tile = _nightly_cycle_kpi({"severity": "OK", "forecast": "On track", "latest_margin_sec": 7200,
                               "latest_state": "COMPLETE", "target_hhmm": "07:00",
                               "upcoming_spike_label": "month-end", "spike_extra_sec": 1800}, 0)
    assert tile["value"] == "On track"
    assert "month-end tonight" in tile["delta"]


def test_both_surfaces_pass_the_spike_calendar():
    for rel in ("app/ui/pages/operations.py", "app/ui/pages/brief.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert 'spike_calendar=str(settings.get("EXPECTED_SPIKE_CALENDAR")' in src, rel


# --- adversarial review of #18 -----------------------------------------------------------------------
_CAL = "MONTH_END:1;QUARTER_END:2"      # the default EXPECTED_SPIKE_CALENDAR shape


def _night_m(d: str, margin_sec: float) -> dict:
    """A night starting D 22:00 that finishes margin_sec before the D+1 07:00 target."""
    d0 = pd.Timestamp(d).normalize()
    finish = d0 + timedelta(days=1, hours=7) - timedelta(seconds=margin_sec)
    return {"CYCLE_DATE": d0, "CYCLE_START": d0 + timedelta(hours=22), "CYCLE_FINISH": finish,
            "N_FAILED": 0, "N_RUNNING": 0, "SNAPSHOT_TS": finish}


def test_quarter_boundary_projection_counts_from_tonight():
    # typical margin erodes 600 s per calendar night (4800 s on Sep 28); Sep 29 - Oct 2 are all labelled
    # (quarter-end / month-end), 1800 s heavier. The anchor is 5 nights behind tonight (Oct 3).
    anchor_day = pd.Timestamp("2026-09-28")
    labelled = {"2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"}
    rows = []
    for d in _days("2026-09-15", "2026-10-02"):
        typ = 4800 - 600 * (pd.Timestamp(d) - anchor_day).days
        rows.append(_night_m(d, typ - 1800 if d in labelled else typ))
    rows[-1]["SNAPSHOT_TS"] = pd.Timestamp("2026-10-03 07:30")
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar=_CAL)
    assert fc["slope_sec_per_night"] == -600.0
    assert fc["forecast"] == "Trending to miss"        # was a false 'On track' (n2b counted from Sep 28)
    assert fc["nights_to_breach"] == 4                 # typical trend hits 0 on Oct 6 = tonight + 3
    assert fc["typical_margin_sec"] == 1800.0          # 4800 - 5 x 600


def test_at_pace_base_is_the_typical_margin_not_a_labelled_last_night():
    # typical nights finish 1h45 early, every labelled night 45 min early; last night (Sep 30) was labelled
    rows = [_night_m(d, 2700 if d in {"2026-07-31", "2026-08-01", "2026-08-31", "2026-09-01", "2026-09-29",
                                      "2026-09-30", "2026-07-01", "2026-07-02"} else 6300)
            for d in _days("2026-06-25", "2026-09-30")]
    rows[-1]["SNAPSHOT_TS"] = pd.Timestamp("2026-10-01 08:00")
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar=_CAL)
    assert fc["upcoming_spike_label"] == "month-end" and fc["latest_margin_sec"] == 2700
    assert fc["typical_margin_sec"] == 6300.0 and fc["spike_extra_sec"] == 3600.0
    assert fc["typical_margin_sec"] - fc["spike_extra_sec"] > 0      # no false 'would miss' warning
    ops = (_ROOT / "app/ui/pages/operations.py").read_text(encoding="utf-8")
    assert 'safe_float(fc.get("typical_margin_sec")) - safe_float(_xtra)' in ops


def test_quarter_end_is_sized_from_quarter_end_nights_only():
    # quarter-end-labelled nights run 2h heavy, month-end ones 30 min: tonight (Sep 29) is quarter-end
    qe, me = {"2026-06-29", "2026-07-02"}, {"2026-06-30", "2026-07-01", "2026-07-31", "2026-08-01",
                                           "2026-08-31", "2026-09-01"}
    rows = [_night_m(d, 9000 - 7200 if d in qe else 9000 - 1800 if d in me else 9000)
            for d in _days("2026-06-20", "2026-09-28")]
    rows[-1]["SNAPSHOT_TS"] = pd.Timestamp("2026-09-29 10:00")
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar=_CAL)
    assert fc["upcoming_spike_label"] == "quarter-end"
    assert fc["spike_extra_sec"] == 7200.0 and fc["spike_nights_seen"] == 2


def test_a_missing_night_still_advances_tonight_after_the_deadline():
    # rows through the Oct 31 night; the Nov 1 night never started; it is 08:30 on Nov 2
    rows = [_night_m(d, 7200) for d in _days("2026-10-15", "2026-10-31")]
    rows[-1]["SNAPSHOT_TS"] = pd.Timestamp("2026-11-02 08:30")
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), spike_calendar="MONTH_END:1")
    assert fc["upcoming_night_key"] == pd.Timestamp("2026-11-02")    # was Nov 1 (a month-end) until noon
    assert fc["upcoming_spike_label"] == ""
