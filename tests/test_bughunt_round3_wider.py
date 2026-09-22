"""Locks for bug-hunt round 3 (wf_90058f36): served-window on the wasted-spend tile, the last
Overview driver-caption label straggler, and the Security effective-access sticky-selection guard.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_wasted_spend_monthlyizes_and_labels_by_the_served_window():
    # the live wasted-spend scan clamps a trailing window to 90d, so the monthly divisor AND the
    # tile label must use the SERVED window, not the raw 180/365 pick (else ~2-4x understated + a
    # served-window label lie). Bounded presets scan the full range, so days stays right there.
    src = _read("app/ui/pages/operations.py")
    assert "_waste_served = days if bounds is not None else min(int(days), MAX_LIVE_WINDOW_DAYS)" in src
    assert "monthly = _wasted_total / max(_waste_served, 1) * 30.0" in src
    assert "window_label(bounds, _waste_served)" in src
    # the raw-days divisor + label are gone
    assert "_wasted_total / max(days, 1) * 30.0" not in src
    assert "else (window_label(bounds, days))" not in src


def test_overview_driver_caption_names_all_three_calendar_presets():
    # the last straggler of the systemic idiom: `"through today" if _ov_bounds is None else
    # "last month"` collapsed Current-month/Current-year to "last month".
    src = _read("app/ui/pages/overview.py")
    assert "else window_phrase(_ov_bounds, days)" in src
    assert 'if _ov_bounds is None else "last month"' not in src


def test_security_effective_access_selection_is_change_guarded():
    # st.dataframe selection is sticky; an unconditional write reverts the user's selectbox pick.
    src = _read("app/ui/security_center.py")
    assert 'selection != st.session_state.get("_sec_effective_sel_last")' in src
    assert 'st.session_state["_sec_effective_sel_last"] = selection' in src
