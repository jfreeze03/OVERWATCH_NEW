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


# --- Next-Fifty #1: the whole-night roll-up feeds the tile too ------------------------------------
_FC_OK = {"severity": "OK", "latest_state": "COMPLETE", "latest_margin_sec": 3600, "target_hhmm": "07:00"}


def test_missing_runs_after_failures() -> None:
    k = _nightly_cycle_kpi(_FC_OK, 0, 2)
    assert k["value"] == "Missing runs" and k["severity"] == "bad" and "2 workflows" in k["delta"]


def test_failures_outrank_missing() -> None:
    assert _nightly_cycle_kpi(_FC_OK, 1, 3)["value"] == "Failures"


def test_not_started_is_bad() -> None:
    k = _nightly_cycle_kpi(_FC_OK, 0, 0, not_started=True)
    assert k["value"] == "Not started" and k["severity"] == "bad"


# --- Next-Fifty #36: tonight's projected finish on the In-flight tile ------------------------------
_FC_FLIGHT = {"severity": "OK", "latest_state": "INCOMPLETE", "live_runway_sec": 7200, "target_hhmm": "07:00"}
_ETA = {"ok": True, "risk": "ok", "phase": "on_schedule", "risk_from_pace": False, "projected_hhmm": "05:12",
        "band_lo_hhmm": "04:50", "band_hi_hhmm": "05:35", "worst_hhmm": "05:12"}


def test_in_flight_shows_projected_finish() -> None:
    k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta=_ETA)
    assert (k["value"], k["severity"]) == ("In flight", "info")
    assert k["delta"] == "projected ~05:12 (04:50–05:35)"
    assert "projected finish" in k["help"] and "PIPE_ETL_CYCLE_LATE" in k["help"]


def test_at_risk_when_projection_misses_target() -> None:
    k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta={**_ETA, "risk": "miss", "worst_hhmm": "07:25"})
    assert (k["value"], k["severity"]) == ("At risk", "warn")
    assert k["delta"] == "projected ~07:25, past 07:00"
    k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta={**_ETA, "risk": "breach", "worst_hhmm": "08:10",
                                               "risk_from_pace": True})
    assert k["value"] == "At risk" and k["delta"] == "projected ~08:10 at tonight's pace, past 07:00"


def test_running_long_warns() -> None:
    k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta={**_ETA, "risk": "running_long", "phase": "running_long"})
    assert (k["value"], k["severity"]) == ("Running long", "warn")
    assert k["delta"] == "usually done by 05:35; still running"


def test_due_now_is_info() -> None:
    k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta={**_ETA, "phase": "due"})
    assert (k["value"], k["severity"], k["delta"]) == ("In flight", "info", "due now (usual 04:50–05:35)")


def test_overdue_and_failures_outrank_the_eta() -> None:
    at_risk = {**_ETA, "risk": "miss", "worst_hhmm": "07:25"}
    k = _nightly_cycle_kpi({**_FC_FLIGHT, "live_runway_sec": -600}, 0, eta=at_risk)
    assert k["value"] == "Overdue" and k["severity"] == "bad"
    assert _nightly_cycle_kpi(_FC_FLIGHT, 2, eta=at_risk)["value"] == "Failures"
    assert _nightly_cycle_kpi(_FC_FLIGHT, 0, 1, eta=at_risk)["value"] == "Missing runs"


def test_eta_not_ok_keeps_cycle_still_running() -> None:
    for eta in (None, {}, {"ok": False, "reason": "short_history"}, {"ok": False, "reason": "terminal_not_due"}):
        k = _nightly_cycle_kpi(_FC_FLIGHT, 0, eta=eta)
        assert (k["value"], k["severity"], k["delta"]) == ("In flight", "info", "cycle still running"), eta
    # a completed night ignores any eta (the ETA only speaks while the cycle is in flight)
    assert _nightly_cycle_kpi(_FC_OK, 0, eta={**_ETA, "risk": "miss"})["value"] == "On track"
