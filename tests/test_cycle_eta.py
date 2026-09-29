"""Next-Fifty #36: tonight's projected cycle finish (insights.etl_cycle_eta) and the per-workflow cycle
timeline (insights.cycle_timeline_frame). Pure: no Snowflake, no Streamlit.

Fixtures use whole-minute durations and odd sample sizes, so the pandas linear quantiles (the median equals
Snowflake MEDIAN) land on exact minutes and round('min') is exact."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from app.data import etl_control_sql
from app.logic import insights
from app.logic.insights import (
    ETA_NIGHT_ROW_CAP,
    SLA_FORECAST_MIN_RUNS,
    TIMELINE_COLUMNS,
    cycle_timeline_frame,
    etl_cycle_eta,
    etl_cycle_sla_forecast,
)

_START = pd.Timestamp("2026-09-09 22:30")
_TONIGHT = _START.normalize()


def _fc(durations_min=(410, 420, 430, 440, 450), *, snapshot="2026-09-10 02:00", states=None, spikes=None,
        **over) -> dict:
    """An etl_cycle_sla_forecast-shaped dict: nights newest-first, [0] is tonight (in flight)."""
    nights = [{"CYCLE_DATE": _TONIGHT, "CYCLE_START": _START, "CYCLE_FINISH": pd.NaT,
               "RUN_STATE": "INCOMPLETE", "MARGIN_SEC": None, "EXPECTED_SPIKE": None}]
    for i, d in enumerate(durations_min):
        s = _TONIGHT - timedelta(days=i + 1) + timedelta(hours=22)
        nights.append({"CYCLE_DATE": s.normalize(), "CYCLE_START": s, "CYCLE_FINISH": s + timedelta(minutes=d),
                       "RUN_STATE": (states or {}).get(i, "COMPLETE"), "MARGIN_SEC": 0.0,
                       "EXPECTED_SPIKE": (spikes or {}).get(i)})
    fc = {"ok": True, "nights": nights, "latest_state": "INCOMPLETE", "latest_failed": False,
          "latest_start": _START, "latest_cycle_date": _TONIGHT,
          "snapshot_ts": pd.Timestamp(snapshot) if snapshot else None,
          "target_hhmm": "07:00", "breach_hhmm": "08:00", "upcoming_spike_label": "", "spike_extra_sec": None}
    fc.update(over)
    return fc


def _night(rows=None, *, cycle_date=_TONIGHT, pace_late=1380.0, pace_wf="WF_MID") -> pd.DataFrame:
    base = rows or [{"WORKFLOW_NAME": "WF_START"}, {"WORKFLOW_NAME": "WF_MID"}, {"WORKFLOW_NAME": "WF_END"}]
    return pd.DataFrame([{**r, "CYCLE_DATE": cycle_date, "PACE_WORKFLOW_NAME": pace_wf,
                          "PACE_LATE_SEC": pace_late, "PACE_END_AT": pd.Timestamp("2026-09-10 01:33")}
                         for r in base])


def test_not_in_flight_returns_empty():
    assert etl_cycle_eta(None) == {} and etl_cycle_eta({}) == {}
    assert etl_cycle_eta({"_reason": "needs_setup"}) == {}
    assert etl_cycle_eta(_fc(latest_state="COMPLETE")) == {}
    assert etl_cycle_eta(_fc(latest_state="FAILED", latest_failed=True)) == {}
    assert etl_cycle_eta(_fc(latest_failed=True)) == {}
    # stale: 12h past tonight's 08:00 hard deadline -> an old night never projects (V156's rule)
    assert etl_cycle_eta(_fc(snapshot="2026-09-10 20:00")) == {}
    assert etl_cycle_eta(_fc(snapshot="2026-09-10 19:59"))["ok"] is True


def test_short_history_is_reported_not_guessed():
    eta = etl_cycle_eta(_fc((410, 420, 430)))
    assert eta["ok"] is False and eta["reason"] == "short_history"
    assert (eta["nights_used"], eta["min_nights"]) == (3, SLA_FORECAST_MIN_RUNS) == (3, 4)
    assert etl_cycle_eta(_fc((410, 420, 430, 440)))["ok"] is True


def test_projection_is_start_plus_median_with_quartile_band():
    eta = etl_cycle_eta(_fc())
    assert eta["ok"] is True and eta["nights_used"] == 5
    assert (eta["typical_sec"], eta["band_lo_sec"], eta["band_hi_sec"]) == (430 * 60.0, 420 * 60.0, 440 * 60.0)
    assert eta["projected"] == pd.Timestamp("2026-09-10 05:40")
    assert (eta["start_hhmm"], eta["projected_hhmm"], eta["band_lo_hhmm"], eta["band_hi_hhmm"]) == (
        "22:30", "05:40", "05:30", "05:50")
    assert eta["deadline"] == pd.Timestamp("2026-09-10 07:00")
    assert eta["hard_deadline"] == pd.Timestamp("2026-09-10 08:00")
    assert (eta["vs_target_sec"], eta["vs_breach_sec"]) == (4800.0, 8400.0)      # + = before the deadline
    assert (eta["risk"], eta["phase"]) == ("ok", "on_schedule")
    assert eta["pace_projected"] is None and eta["pace_hhmm"] == "—" and eta["worst_hhmm"] == "05:40"
    # an even sample uses the mean of the two middles, like Snowflake MEDIAN
    assert etl_cycle_eta(_fc((410, 420, 430, 440)))["typical_sec"] == 425 * 60.0


def test_sample_skips_tonight_failed_unfinished_and_labelled_nights():
    fc = _fc((410, 420, 999, 430, 998, 440, 997, 450),
             states={2: "FAILED", 4: "INCOMPLETE"}, spikes={6: "MONTH_END"})
    fc["nights"][0].update(CYCLE_FINISH=_START + timedelta(minutes=5), RUN_STATE="COMPLETE")  # never sampled
    eta = etl_cycle_eta(fc)
    assert eta["nights_used"] == 5 and eta["typical_sec"] == 430 * 60.0
    # a night with no finish, or a finish before its start, is skipped too
    fc2 = _fc((410, 420, 430, 440, 450))
    fc2["nights"][1]["CYCLE_FINISH"] = pd.NaT
    fc2["nights"][2]["CYCLE_FINISH"] = fc2["nights"][2]["CYCLE_START"] - timedelta(minutes=1)
    assert etl_cycle_eta(fc2)["ok"] is False


def test_labelled_night_adds_the_typical_extra():
    eta = etl_cycle_eta(_fc(upcoming_spike_label="MONTH_END", spike_extra_sec=1800.0))
    assert (eta["projected_hhmm"], eta["band_lo_hhmm"], eta["band_hi_hhmm"]) == ("06:10", "06:00", "06:20")
    assert eta["spike_label"] == "MONTH_END" and eta["spike_extra_sec"] == 1800.0
    for over in ({"upcoming_spike_label": "MONTH_END", "spike_extra_sec": -600.0},
                 {"upcoming_spike_label": "", "spike_extra_sec": 1800.0},
                 {"upcoming_spike_label": "MONTH_END", "spike_extra_sec": None}):
        e = etl_cycle_eta(_fc(**over))
        assert e["projected_hhmm"] == "05:40" and e["spike_extra_sec"] is None, over


def test_phases_and_snapshot_floor():
    assert etl_cycle_eta(_fc(snapshot="2026-09-10 02:00"))["phase"] == "on_schedule"
    due = etl_cycle_eta(_fc(snapshot="2026-09-10 05:45"))           # past the median, inside the range
    assert due["phase"] == "due" and due["projected"] == pd.Timestamp("2026-09-10 05:45")
    assert due["risk"] == "ok"
    long_ = etl_cycle_eta(_fc(snapshot="2026-09-10 05:55"))         # past the 75th percentile
    assert long_["phase"] == "running_long" and long_["risk"] == "running_long"
    assert long_["projected_hhmm"] == "05:55"                       # never earlier than now
    no_snap = etl_cycle_eta(_fc(snapshot=None))                     # no clock: no floor, never raises
    assert no_snap["projected_hhmm"] == "05:40" and no_snap["phase"] == "on_schedule"


def test_deadlines_cross_midnight_and_never_invert():
    miss = etl_cycle_eta(_fc(snapshot="2026-09-10 07:30"))
    assert miss["vs_target_sec"] == -1800.0 and miss["vs_breach_sec"] == 1800.0 and miss["risk"] == "miss"
    breach = etl_cycle_eta(_fc(snapshot="2026-09-10 08:30"))
    assert breach["risk"] == "breach" and breach["vs_breach_sec"] == -1800.0
    inverted = etl_cycle_eta(_fc(breach_hhmm="06:00"))              # the forecaster's never-invert rule
    assert inverted["breach_hhmm"] == "08:00" and inverted["hard_deadline"] == pd.Timestamp("2026-09-10 08:00")
    after_midnight = _fc(latest_start=pd.Timestamp("2026-09-10 00:30"))
    e = etl_cycle_eta(after_midnight)
    assert e["deadline"] == pd.Timestamp("2026-09-10 07:00") and e["projected_hhmm"] == "07:40"
    assert e["risk"] == "miss"


def test_pace_comes_from_the_night_frame():
    eta = etl_cycle_eta(_fc(), _night(pace_late=1380.0), end_workflow="WF_END")
    assert eta["pace_workflow"] == "WF_MID" and eta["pace_late_sec"] == 1380.0
    assert eta["pace_projected"] == pd.Timestamp("2026-09-10 06:03") and eta["pace_hhmm"] == "06:03"
    assert eta["pace_end"] == pd.Timestamp("2026-09-10 01:33")
    assert eta["projected_hhmm"] == "05:40"                         # pace never moves the headline
    assert eta["risk"] == "ok" and eta["worst_hhmm"] == "06:03"
    late = etl_cycle_eta(_fc(), _night(pace_late=5400.0), end_workflow="WF_END")   # +90 min -> 07:10
    assert late["risk"] == "miss" and late["risk_from_pace"] is True and late["worst_hhmm"] == "07:10"
    assert late["projected_hhmm"] == "05:40"
    ahead = etl_cycle_eta(_fc(), _night(pace_late=-600.0), end_workflow="WF_END")
    assert ahead["risk_from_pace"] is False and ahead["worst_hhmm"] == "05:40"
    assert eta["pace_available"] is True
    # a known-but-NULL pace (nothing upstream finished yet) vs an unknown one (the fallback frame without the
    # pace columns, or a frame from another night): both leave the headline alone, but only one is "not yet"
    for night, known in ((_night(pace_late=float("nan")), True), (_night().drop(columns=["PACE_LATE_SEC"]), False),
                         (_night(cycle_date=_TONIGHT - timedelta(days=1)), False), (None, False)):
        e = etl_cycle_eta(_fc(), night, end_workflow="WF_END")
        assert e["ok"] is True and e["pace_projected"] is None and e["pace_late_sec"] is None
        assert e["pace_available"] is known


def test_terminal_not_due_guard():
    without_end = _night([{"WORKFLOW_NAME": "WF_START"}, {"WORKFLOW_NAME": "WF_MID"}])
    eta = etl_cycle_eta(_fc(), without_end, end_workflow="WF_END")
    assert eta == {"ok": False, "reason": "terminal_not_due", "start": _START, "end_workflow": "WF_END"}
    # the frame hit its row cap: the terminal may just be cut off -> no guard
    assert etl_cycle_eta(_fc(), without_end, end_workflow="WF_END", night_row_cap=2)["ok"] is True
    assert etl_cycle_eta(_fc(), without_end, end_workflow="")["ok"] is True
    stale_night = _night([{"WORKFLOW_NAME": "WF_START"}], cycle_date=_TONIGHT - timedelta(days=1))
    assert etl_cycle_eta(_fc(), stale_night, end_workflow="WF_END")["ok"] is True


def test_tz_aware_inputs_do_not_raise():
    fc = _fc(snapshot=None)
    fc["snapshot_ts"] = pd.Timestamp("2026-09-10 02:00", tz="America/Chicago")
    fc["latest_start"] = pd.Timestamp("2026-09-09 22:30", tz="America/Chicago")
    for n in fc["nights"][1:]:
        n["CYCLE_START"] = n["CYCLE_START"].tz_localize("America/Chicago")
        n["CYCLE_FINISH"] = n["CYCLE_FINISH"].tz_localize("America/Chicago")
    night = _night()
    night["CYCLE_DATE"] = pd.Timestamp("2026-09-09", tz="UTC")
    eta = etl_cycle_eta(fc, night, end_workflow="WF_END")
    assert eta["ok"] is True and eta["projected_hhmm"] == "05:40" and eta["pace_hhmm"] == "06:03"


def _history_frame() -> pd.DataFrame:
    rows = []
    for i, finish_min in enumerate((410, 420, 430, 440, 450)):
        d = pd.Timestamp("2026-09-04") + timedelta(days=i)
        s = d + timedelta(hours=22)
        rows.append({"CYCLE_DATE": d, "CYCLE_START": s, "CYCLE_FINISH": s + timedelta(minutes=finish_min),
                     "N_FAILED": 0, "N_RUNNING": 0})
    rows.append({"CYCLE_DATE": _TONIGHT, "CYCLE_START": _START, "CYCLE_FINISH": pd.NaT,
                 "N_FAILED": 0, "N_RUNNING": 1})
    return pd.DataFrame(rows).assign(SNAPSHOT_TS=pd.Timestamp("2026-09-10 02:00", tz="America/Chicago"))


def test_forecaster_exposes_a_naive_snapshot():
    fc = etl_cycle_sla_forecast(_history_frame())
    assert fc["snapshot_ts"] == pd.Timestamp("2026-09-10 02:00") and fc["snapshot_ts"].tzinfo is None
    assert etl_cycle_sla_forecast(_history_frame().drop(columns=["SNAPSHOT_TS"]))["snapshot_ts"] is None


def test_end_to_end_with_the_real_forecaster():
    fc = etl_cycle_sla_forecast(_history_frame())
    assert fc["latest_state"] == "INCOMPLETE"
    eta = etl_cycle_eta(fc, _night(pace_late=600.0), end_workflow="WF_END")
    # 22:30 + the median 7h10m of the five clean nights; the range is their 25th-75th percentile
    assert (eta["projected_hhmm"], eta["band_lo_hhmm"], eta["band_hi_hhmm"]) == ("05:40", "05:30", "05:50")
    assert eta["nights_used"] == 5 and eta["pace_hhmm"] == "05:50" and eta["risk"] == "ok"


def test_shaped_garbage_never_raises():
    g = pd.DataFrame({c: [1.0, 2.0] for c in ("CYCLE_START", "CYCLE_FINISH", "N_FAILED", "N_RUNNING",
                                               "CYCLE_DATE", "SNAPSHOT_TS")})
    for frame in (g, g.assign(N_FAILED=0.0), g.assign(N_FAILED=0.0, N_RUNNING=0.0)):
        fc = etl_cycle_sla_forecast(frame)
        out = etl_cycle_eta(fc, None)
        assert out == {} or out.get("ok") is False or out.get("ok") is True
    gn = pd.DataFrame({c: ([f"{c}_0", f"{c}_1"] if ("NAME" in c or "STATUS" in c) else [1.0, 2.0])
                       for c in ("WORKFLOW_NAME", "NIGHT_STATUS", "CYCLE_DATE", "START_OFFSET_SEC",
                                 "END_OFFSET_SEC", "TYPICAL_OFFSET_SEC", "TYPICAL_END_OFFSET_SEC",
                                 "END_NIGHTS_COUNT", "PACE_WORKFLOW_NAME", "PACE_LATE_SEC", "PACE_END_AT",
                                 "FIRST_START_AT", "LAST_END_AT", "SNAPSHOT_TS")})
    fc2 = etl_cycle_sla_forecast(g.assign(N_FAILED=0.0))
    fc2["snapshot_ts"] = 2.0
    out = etl_cycle_eta(fc2, gn, end_workflow="WF")
    assert isinstance(out, dict)
    assert etl_cycle_eta({"nights": ["x", None], "latest_state": "INCOMPLETE", "latest_start": "garbage"}) == {}
    assert etl_cycle_eta({"nights": [{}], "latest_state": "INCOMPLETE", "latest_start": _START}) == {
        "ok": False, "reason": "short_history", "nights_used": 0, "min_nights": 4, "start": _START}
    tl = cycle_timeline_frame(gn, end_workflow="X")
    assert list(tl.columns) == TIMELINE_COLUMNS and len(tl) == 2


def test_constants_mirror_the_data_layer():
    assert insights.ETA_NIGHT_ROW_CAP == ETA_NIGHT_ROW_CAP == etl_control_sql.MAX_NIGHT_WORKFLOWS
    assert etl_control_sql.NIGHT_END_MIN_NIGHTS == insights.SLA_FORECAST_MIN_RUNS
    assert insights.ETA_BAND_QUANTILES == (0.25, 0.75)


def _tl_night() -> pd.DataFrame:
    snap = pd.Timestamp("2026-09-10 02:00", tz="America/Chicago")
    rows = [
        {"WORKFLOW_NAME": "WF_END", "NIGHT_STATUS": "PENDING", "FIRST_START_AT": pd.NaT, "LAST_END_AT": pd.NaT,
         "START_OFFSET_SEC": None, "TYPICAL_OFFSET_SEC": 18000, "END_OFFSET_SEC": None,
         "TYPICAL_END_OFFSET_SEC": 25000, "END_NIGHTS_COUNT": 12},
        {"WORKFLOW_NAME": "WF_LOAD", "NIGHT_STATUS": "RUNNING", "FIRST_START_AT": pd.Timestamp("2026-09-10 01:40"),
         "LAST_END_AT": pd.NaT, "START_OFFSET_SEC": 11400, "TYPICAL_OFFSET_SEC": 10500, "END_OFFSET_SEC": None,
         "TYPICAL_END_OFFSET_SEC": 16000, "END_NIGHTS_COUNT": 9},
        {"WORKFLOW_NAME": "WF_MID", "NIGHT_STATUS": "OK", "FIRST_START_AT": pd.Timestamp("2026-09-09 23:10"),
         "LAST_END_AT": pd.Timestamp("2026-09-10 01:33"), "START_OFFSET_SEC": 2400, "TYPICAL_OFFSET_SEC": 2100,
         "END_OFFSET_SEC": 11580, "TYPICAL_END_OFFSET_SEC": 10200, "END_NIGHTS_COUNT": 13},
        {"WORKFLOW_NAME": "WF_NEW", "NIGHT_STATUS": "OK", "FIRST_START_AT": pd.Timestamp("2026-09-09 22:40"),
         "LAST_END_AT": pd.Timestamp("2026-09-10 01:50"), "START_OFFSET_SEC": 600, "TYPICAL_OFFSET_SEC": 600,
         "END_OFFSET_SEC": 12000, "TYPICAL_END_OFFSET_SEC": 11000, "END_NIGHTS_COUNT": 3},
        {"WORKFLOW_NAME": "WF_START", "NIGHT_STATUS": "OK", "FIRST_START_AT": pd.Timestamp("2026-09-09 22:30"),
         "LAST_END_AT": pd.Timestamp("2026-09-09 23:00"), "START_OFFSET_SEC": 0, "TYPICAL_OFFSET_SEC": 0,
         "END_OFFSET_SEC": 1800, "TYPICAL_END_OFFSET_SEC": 1700, "END_NIGHTS_COUNT": 14},
        {"WORKFLOW_NAME": "WF_GONE", "NIGHT_STATUS": "MISSING", "FIRST_START_AT": pd.NaT, "LAST_END_AT": pd.NaT,
         "START_OFFSET_SEC": None, "TYPICAL_OFFSET_SEC": None, "END_OFFSET_SEC": None,
         "TYPICAL_END_OFFSET_SEC": None, "END_NIGHTS_COUNT": 0},
    ]
    return pd.DataFrame(rows).assign(PACE_WORKFLOW_NAME="WF_MID", PACE_LATE_SEC=1380.0, SNAPSHOT_TS=snap)


def test_timeline_is_chronological_with_notes():
    tl = cycle_timeline_frame(_tl_night(), start_workflow="WF_START", end_workflow="WF_END")
    assert list(tl.columns) == TIMELINE_COLUMNS
    # tonight's start offset, else the usual one; no offset at all sorts last
    assert list(tl["WORKFLOW_NAME"]) == ["WF_START", "WF_NEW", "WF_MID", "WF_LOAD", "WF_END", "WF_GONE"]
    notes = dict(zip(tl["WORKFLOW_NAME"], tl["TIMELINE_NOTE"], strict=True))
    assert notes == {"WF_START": "Starts the cycle", "WF_NEW": "Finished last so far", "WF_MID": "Pace marker",
                     "WF_LOAD": "", "WF_END": "Finishes the cycle", "WF_GONE": ""}
    # precedence: the pace marker outranks 'finished last', the anchors outrank both
    only_mid = _tl_night()
    only_mid = only_mid[only_mid["WORKFLOW_NAME"].isin(["WF_MID", "WF_LOAD"])]
    tl2 = cycle_timeline_frame(only_mid, end_workflow="WF_MID")
    assert tl2.set_index("WORKFLOW_NAME").loc["WF_MID", "TIMELINE_NOTE"] == "Finishes the cycle"


def test_timeline_running_elapsed_and_usual_end_needs_4_nights():
    tl = cycle_timeline_frame(_tl_night()).set_index("WORKFLOW_NAME")
    assert tl.loc["WF_LOAD", "RUNNING_FOR_SEC"] == 1200.0            # 02:00 snapshot - 01:40 start
    assert pd.isna(tl.loc["WF_MID", "RUNNING_FOR_SEC"])              # only RUNNING rows
    assert pd.isna(tl.loc["WF_NEW", "USUAL_END_OFFSET_SEC"])          # 3 clean nights < 4
    assert pd.isna(tl.loc["WF_NEW", "LATE_VS_USUAL_SEC"])
    assert tl.loc["WF_MID", "LATE_VS_USUAL_SEC"] == 1380.0            # + = late
    assert tl.loc["WF_START", "LATE_VS_USUAL_SEC"] == 100.0
    assert tl.loc["WF_END", "USUAL_END_OFFSET_SEC"] == 25000.0 and pd.isna(tl.loc["WF_END", "END_OFFSET_SEC"])
    early = _tl_night()
    early.loc[early["WORKFLOW_NAME"] == "WF_MID", "END_OFFSET_SEC"] = 9600
    assert cycle_timeline_frame(early).set_index("WORKFLOW_NAME").loc["WF_MID", "LATE_VS_USUAL_SEC"] == -600.0
    # the duration columns carry the _SEC suffix, so the table humanizes them
    assert all(c.endswith("_SEC") for c in TIMELINE_COLUMNS if "OFFSET" in c or "FOR" in c or "LATE" in c)


def test_timeline_empty_or_missing_columns_gives_the_empty_contract():
    for bad in (None, pd.DataFrame(), _tl_night().drop(columns=["END_OFFSET_SEC"]),
                _tl_night().drop(columns=["TYPICAL_END_OFFSET_SEC"])):
        out = cycle_timeline_frame(bad)
        assert out.empty and list(out.columns) == TIMELINE_COLUMNS
    # a pre-#36 fallback frame (no offset columns) -> empty, never a KeyError
    legacy = _tl_night()[["WORKFLOW_NAME", "NIGHT_STATUS", "FIRST_START_AT", "LAST_END_AT", "TYPICAL_OFFSET_SEC"]]
    assert cycle_timeline_frame(legacy).empty
