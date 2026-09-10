"""Brief 'Nightly cycle' tile mapping (brief._nightly_cycle_kpi).

The tile replaced 'Open incidents' (owner ask 2026-09-09): it shows the whole-cycle SLA finish
forecast (on track / regressing / late) plus any failures, worst-first, and never fakes a green
'on track' when there is no forecast data.
"""

from __future__ import annotations

from app.ui.pages.brief import _nightly_cycle_kpi


def _kpi(**fc):
    return _nightly_cycle_kpi(fc, 0)


def test_failures_take_priority_over_a_clean_forecast() -> None:
    k = _nightly_cycle_kpi({"severity": "OK", "latest_margin_sec": 7200, "target_hhmm": "07:00"}, 2)
    assert k["value"] == "Failures"
    assert k["severity"] == "bad"
    assert "2 failed tasks" in k["delta"]


def test_late_when_forecast_high() -> None:
    k = _kpi(severity="High", forecast="Breaching", latest_margin_sec=-1800, target_hhmm="07:00")
    assert k["value"] == "Late"
    assert k["severity"] == "bad"
    assert "past 07:00" in k["delta"]


def test_regressing_when_forecast_medium() -> None:
    k = _kpi(severity="Medium", nights_to_breach=3, target_hhmm="07:00")
    assert k["value"] == "Regressing"
    assert k["severity"] == "warn"
    assert "3 night" in k["delta"]


def test_on_track_shows_margin_before_target() -> None:
    # GREEN only when the LATEST night actually COMPLETED on time
    k = _kpi(severity="OK", latest_margin_sec=4800, target_hhmm="07:00", latest_state="COMPLETE")
    assert k["value"] == "On track"
    assert k["severity"] == "ok"
    assert "before 07:00" in k["delta"]


def test_incomplete_overdue_is_bad_not_green() -> None:
    # a hung latest cycle past the target must NOT read green off an older night's margin (HIGH bug)
    k = _kpi(severity="OK", latest_margin_sec=7200, target_hhmm="07:00",
             latest_state="INCOMPLETE", live_runway_sec=-1200)
    assert k["value"] == "Overdue"
    assert k["severity"] == "bad"
    assert "past 07:00" in k["delta"]


def test_incomplete_in_flight_is_neutral() -> None:
    k = _kpi(severity="OK", latest_margin_sec=7200, target_hhmm="07:00",
             latest_state="INCOMPLETE", live_runway_sec=1800)
    assert k["value"] == "In flight"
    assert k["severity"] == "info"


def test_failed_latest_reads_failures_without_wf_fail_count() -> None:
    # a FAILED terminal night must show red even when the separately-scoped wf_fail_n read is 0
    k = _nightly_cycle_kpi({"severity": "OK", "latest_state": "FAILED", "latest_failed": True,
                            "latest_margin_sec": 7200, "target_hhmm": "07:00"}, 0)
    assert k["value"] == "Failures"
    assert k["severity"] == "bad"


def test_ok_severity_without_completed_latest_is_neutral() -> None:
    # sev OK but no COMPLETE latest night (no completed baseline) -> neutral, never green
    k = _kpi(severity="OK", latest_margin_sec=4800, latest_state=None, target_hhmm="07:00")
    assert k["value"] == "—"
    assert k["severity"] == "info"


def test_no_forecast_data_is_neutral_not_green() -> None:
    # a missing/silent probe must NOT read as a green 'on track' all-clear
    k = _nightly_cycle_kpi({}, 0)
    assert k["value"] == "—"
    assert k["severity"] == "info"


def test_failures_still_win_without_a_forecast() -> None:
    k = _nightly_cycle_kpi({}, 1)
    assert k["value"] == "Failures"
    assert k["severity"] == "bad"
    assert "1 failed task" in k["delta"]


def test_label_is_nightly_cycle() -> None:
    assert _nightly_cycle_kpi({}, 0)["label"] == "Nightly cycle"
