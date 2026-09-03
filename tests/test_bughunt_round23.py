"""Regression locks for bug-hunt round 23 — phantom-success writes, false-all-clear, chart precision.

The write-path sweep found the round-22 control_room phantom-count class recurring on write paths it
didn't reach: execute_statement/execute_action never surface the affected-row count, so a success
message that asserts a count (from a stale render-time read or by counting non-raising statements)
overstates what actually happened.

BULK-ACK   alerts bulk ack/resolve receipt asserted the stale selected count; now reports the proc's
           true 'OK: N event(s)' count (parsed by _proc_event_count).
CLEAR      alerts clear-queue RESOLVE receipt asserted the stale cached _open_total; now the proc count.
QUEUE      ai_chargeback governance-queue message counted non-raising idempotent inserts as "queued".
BRIEF      brief page verdict now warns "open-incident count unavailable" on a failed incident read
           instead of a green "no incidents" all-clear.
INC-CLOSE  control_room single incident-close now pre-checks the incident is still open before claiming
           a close + firing incident_close telemetry.
SNOOZE     alerts snoozed backstop discloses a failed read instead of silently hiding a pending snooze.
CHART      bar_count keeps a fractional avg-queue-per-query metric at 1dp (was integer -> 0).
"""

from __future__ import annotations

from pathlib import Path

from app.ui.charts import _share_note
from app.ui.pages.alerts import _proc_event_count
from app.ui.pages.control_room import _incident_open_check_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- helpers (behavioral) ------------------------------------------------------------------------
def test_proc_event_count_parses_the_true_count():
    assert _proc_event_count("OK: 3 event(s) RESOLVE") == 3
    assert _proc_event_count("OK: 0 event(s) ACK") == 0           # externally-resolved -> honest 0
    assert _proc_event_count("DUPLICATE — already applied") is None
    assert _proc_event_count("") is None
    assert _proc_event_count(None) is None
    assert _proc_event_count("weird reply") is None


def test_incident_open_check_sql_is_a_forward_only_status_probe():
    sql = _incident_open_check_sql("INC-abc")
    assert "STILL_OPEN" in sql
    assert "STATUS IN ('OPEN', 'MITIGATED')" in sql
    assert "'INC-abc'" in sql                                     # id literal, injection-safe form
    assert "UPDATE" not in sql.upper()                            # read-only pre-check


def test_share_note_honors_a_fractional_value_fmt():
    # the round-23 chart bug: a real 0.4s/query rounded to "0"; with value_fmt it survives
    note = _share_note("WH_X", 0.4, 1.0, dollars=False, value_fmt=",.1f")
    assert "0.4" in note and "Top: WH_X" in note
    # default (no value_fmt) still renders integers for the count callers
    assert "5" in _share_note("WH_Y", 5.0, 10.0, dollars=False)


# --- wirings (source-lock) -----------------------------------------------------------------------
def test_alerts_receipts_report_the_proc_count_not_the_stale_count():
    src = _src("app/ui/pages/alerts.py")
    assert "_n_moved = _proc_event_count(msg_u)" in src
    assert "_n_res = _proc_event_count(_c_m)" in src
    # the stale render-time / cached counts are no longer asserted as the outcome
    assert 'f"Bulk {b_action} recorded — {len(_bulk_ids)} event(s)"' not in src
    assert 'f"Open queue cleared — {_open_total:,} event(s) resolved."' not in src


def test_snoozed_backstop_discloses_a_failed_read():
    src = _src("app/ui/pages/alerts.py")
    assert "if not _snz.ok:" in src
    assert "Snoozed-events check unavailable" in src


def test_ai_governance_queue_message_is_honest_about_idempotency():
    src = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert 'f"{count}/{len(statements)} action(s) queued."' not in src
    assert "governance insert(s) ran" in src and "idempotent" in src


def test_brief_verdict_guards_a_failed_incident_read():
    src = _src("app/ui/pages/brief.py")
    assert 'if not _inc.ok:' in src
    assert 'Signal("warn", "open-incident count unavailable")' in src
    assert "elif _n_inc > 0:" in src
    # the old unconditional success-only branch is gone
    assert "if _inc.ok and _n_inc > 0:" not in src


def test_incident_single_close_prechecks_before_claiming_a_close():
    src = _src("app/ui/pages/control_room.py")
    assert "_incident_open_check_sql(_iid)" in src
    assert "Already resolved — no change" in src
    # the log_ui_event now sits inside the still-open else branch, not fired on a no-op
    assert 'log_ui_event("incident_close"' in src


def test_bar_count_takes_a_value_format_and_queue_chart_uses_one_decimal():
    charts = _src("app/ui/charts.py")
    assert 'value_fmt: str = ",.0f"' in charts
    assert "axis=alt.Axis(format=value_fmt)" in charts
    ops = _src("app/ui/pages/operations.py")
    assert 'value_fmt=",.1f" if _chart_metric == "AVG_QUEUE_SEC" else ",.0f"' in ops
