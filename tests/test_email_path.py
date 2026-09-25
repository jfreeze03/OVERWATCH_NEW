"""Next-Fifty #4 (v4.589.0): Alerts > Native delivery shows an 'Email path' health row for the opt-in
EMAIL alerts. It goes red ONLY on positive failure evidence (a send, or an alert evaluation, that failed
more recently than the last success); a privilege gap or an uninstalled opt-in reads unverifiable / not
visible — never a false red."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic.email_path import email_path_verdict

_ROOT = Path(__file__).resolve().parents[1]
_NAMES = mart_sql.EMAIL_ALERT_NAMES


def _alerts(states: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"name": list(states), "state": list(states.values())})


_ALL_STARTED = _alerts(dict.fromkeys(_NAMES, "started"))


def _notif(sent_n=3, sent="2026-09-24 06:10", failed_at=None, err=None) -> pd.DataFrame:
    return pd.DataFrame([{"SENT_N": sent_n, "FAILED_N": 0 if failed_at is None else 1,
                          "LAST_SENT_AT": sent, "LAST_FAILED_AT": failed_at, "LAST_ERROR": err}])


def test_unreadable_and_not_visible_are_never_red():
    v = email_path_verdict(None, None, None, _NAMES)
    assert (v.state, v.severity) == ("UNVERIFIABLE", "info")
    v = email_path_verdict(pd.DataFrame(), None, None, _NAMES)
    assert (v.state, v.severity) == ("NOT_VISIBLE", "info")


def test_a_newer_send_failure_is_failing():
    v = email_path_verdict(_ALL_STARTED, None, _notif(failed_at="2026-09-24 07:10", err="recipient not verified"),
                           _NAMES)
    assert (v.state, v.severity) == ("FAILING", "bad") and "recipient not verified" in v.detail


def test_an_older_send_failure_is_not_red():
    v = email_path_verdict(_ALL_STARTED, None, _notif(failed_at="2026-09-20 07:10"), _NAMES)
    assert v.severity != "bad"


def test_a_newer_evaluation_failure_is_failing():
    hist = pd.DataFrame([{"NAME": "NATIVE_ALERT_SCAN_HEARTBEAT", "LAST_OK_AT": "2026-09-24 05:10",
                          "LAST_FAIL_AT": "2026-09-24 06:10", "LAST_FAIL_ERROR": "ACTION_FAILED: x"}])
    v = email_path_verdict(_ALL_STARTED, hist, _notif(), _NAMES)
    assert (v.state, v.severity) == ("FAILING", "bad") and "NATIVE_ALERT_SCAN_HEARTBEAT" in v.headline


def test_all_suspended_and_partial():
    v = email_path_verdict(_alerts(dict.fromkeys(_NAMES, "suspended")), None, None, _NAMES)
    assert (v.state, v.severity) == ("SUSPENDED", "warn") and "RESUME" in v.headline
    v = email_path_verdict(_alerts({_NAMES[0]: "started", _NAMES[1]: "started"}), None, None, _NAMES)
    assert (v.state, v.severity) == ("PARTIAL", "warn") and "2 of 4" in v.headline


def test_live_quiet_and_unverifiable_histories():
    v = email_path_verdict(_ALL_STARTED, pd.DataFrame(), _notif(sent_n=0, sent=None), _NAMES)
    assert (v.state, v.severity) == ("LIVE", "ok") and "no email needed" in v.headline
    v = email_path_verdict(_ALL_STARTED, None, None, _NAMES)
    assert (v.state, v.severity) == ("LIVE", "ok")
    assert "send history unverifiable" in v.headline and "evaluation history unverifiable" in v.headline


def test_builders_read_information_schema_and_clamp():
    hist, notif = mart_sql.email_alert_history(999), mart_sql.email_notification_history(999)
    for sql in (hist, notif):
        assert "ACCOUNT_USAGE" not in sql and "CONVERT_TIMEZONE('America/Chicago'" in sql
    assert "INFORMATION_SCHEMA.ALERT_HISTORY" in hist and "DATEADD('day', -7," in hist
    # owner probe R2: NOTIFICATION_HISTORY rejects a range over 336h, so 999 clamps to 13 days, not 14
    assert "NOTIFICATION_HISTORY" in notif and "'OVERWATCH_EMAIL'" in notif and "DATEADD('day', -13," in notif
    # owner probe 2026-09-24: NOTIFICATION_HISTORY rejects START_TIME_RANGE_START (ALERT_HISTORY's name)
    assert "START_TIME => DATEADD" in notif and "START_TIME_RANGE_START" not in notif
    assert "SCHEDULED_TIME_RANGE_START =>" in hist
    assert mart_sql.email_alert_objects().startswith("SHOW ALERTS")


def test_runbook_preflight_avoids_reserved_aliases():
    # owner probe 2026-09-24: 'AS SAMPLE' is a Snowflake syntax error (SAMPLE is reserved)
    doc = (_ROOT / "docs/EMAIL_RECIPIENT_RUNBOOK.md").read_text(encoding="utf-8")
    assert "AS SAMPLE_MSG" in doc
    assert not re.search(r"\bAS\s+(SAMPLE|TABLESAMPLE|QUALIFY|ILIKE|REGEXP)\s*$", doc, re.MULTILINE)


def test_alerts_page_wires_the_email_path_row():
    src = (_ROOT / "app/ui/pages/alerts.py").read_text(encoding="utf-8")
    render = src.split("def render(", 1)[1]
    assert render.index("_delivery_status()") < render.index("_email_path_status()") < render.index(
        "_last_delivery_card()")
    assert render.count("_email_path_status()") == 1       # called once, in render
    body = src.split("def _email_path_status(", 1)[1].split("\ndef ", 1)[0]
    assert body.count("probe=True") == 3 and "max_rows=0" in body
    assert "st.error(" not in body            # red goes through the severity map, never directly
