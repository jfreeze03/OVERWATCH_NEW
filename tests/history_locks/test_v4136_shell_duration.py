"""Regression locks for v4.136 shell routing and duration consistency."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _src(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_global_pulse_uses_native_navigation_not_raw_links():
    main = _src("app/main.py")
    components = _src("app/ui/components.py")
    pulse = main.split("def _persistent_status_bar", 1)[1].split("\ndef ", 1)[0]
    status = components.split("def status_bar", 1)[1].split("\ndef ", 1)[0]

    assert "_page_href" not in main
    assert '"target": _target(' in pulse
    assert "page in pages" in pulse
    assert "st.button(" in status
    assert "request_navigation(page, section)" in status
    assert "href=" not in status


def test_control_room_activity_shape_is_checked_before_column_reads():
    control = _src("app/ui/pages/control_room.py")
    pulse = control.split('if section == "Pulse":', 1)[1].split(
        'elif section == "Incidents & triage":', 1)[0]

    guard = '_activity_ready = act.usable() and _activity_cols.issubset(act.df.columns)'
    assert '_activity_cols = {"DAY", "QUERIES", "FAILS"}' in pulse
    assert guard in pulse
    assert pulse.index(guard) < pulse.index('act.df["QUERIES"]')
    assert 'charts.daily_metric_line(act.df, "DAY", "QUERIES", "Queries", unit="count")' in pulse
    assert 'charts.daily_metric_line(act.df, "DAY", "FAILS", "Failed queries", unit="count")' in pulse


def test_page_kpis_use_shared_duration_formatter():
    control = _src("app/ui/pages/control_room.py")
    operations = _src("app/ui/pages/operations.py")
    alerts = _src("app/ui/pages/alerts.py")

    assert 'humanize_duration(row.get("P95_ELAPSED_SEC"), "s")' in control
    queue_read = 'queue_sec = safe_float(row.get("QUEUED_SEC"))'
    queue_display = 'humanize_duration(queue_sec, "s")'
    assert queue_read in control and queue_display in control
    assert control.index(queue_read) < control.index(queue_display)
    assert 'humanize_duration(row.get("P95_ELAPSED_SEC"), "s")' in operations
    assert 'humanize_duration(row.get("QUEUED_SEC"), "s")' in operations
    assert "humanize_duration(row0.get('P95_MIN'), 'min')" in alerts
