"""V164 app side (Next-Fifty #40): Alerts > Native delivery states the escalation policy and the last 7 days.

- mart_sql.escalation_summary is ONE row from tables that exist before V164 (ALERT_AUDIT ESCALATE rows, not the
  new ALERT_EVENTS.ESCALATED_AT column), so the Admin canary is green on either side of the apply; its SETTINGS
  expressions are the notifier's own (parity against the LATEST SP_NOTIFY_WEBHOOK body).
- The policy line and the routing-help sentence appear only once has_migration(164) says V164 is applied; before
  that the section issues no escalation read at all (shaped AppTest, both sides of the gate).
- _escalation_lines words the row: off / on with and without the email leg, the tally, failures as a warning.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_run,
    _stub_shaped,
)

from app.data import mart_sql
from tests._source import page_source

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")


def _latest_notifier() -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    return _latest_proc_bodies()["SP_NOTIFY_WEBHOOK"]


# -- the builder ----------------------------------------------------------------------------------------------

def test_escalation_summary_is_one_valid_read_on_pre_v164_tables():
    sql = mart_sql.escalation_summary(7)
    sqlglot = pytest.importorskip("sqlglot")
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert len(parsed) == 1
    assert parsed[0].named_selects == ["ESCALATED_COUNT", "LAST_ESCALATED_AT", "PASS_FAILURES", "EMAIL_FAILURES",
                                       "AFTER_MIN", "EMAIL_INTEGRATION_NAME", "WINDOW_DAYS"]
    assert "ACTION = 'ESCALATE'" in sql and "ERROR_TYPE = 'escalation_failed'" in sql
    assert "ERROR_TYPE = 'escalation_email_failed'" in sql and "PAGE = 'NotifyWebhook'" in sql
    assert "ESCALATED_AT" not in sql.replace("LAST_ESCALATED_AT", "")      # valid before the column exists
    assert "ACCOUNT_USAGE" not in sql
    tables = set(re.findall(r"DBA_MAINT_DB\.OVERWATCH\.(\w+)", sql))
    assert tables == {"ALERT_AUDIT", "APP_ERROR_LOG", "SETTINGS"}
    assert "DATEADD('day', -90," in mart_sql.escalation_summary(999)       # bounded
    assert "7 AS WINDOW_DAYS" in sql


def test_escalation_knobs_are_the_notifiers_own_expressions():
    """The page states what the proc will do: the same absent-row defaults and the same minutes parse."""
    body = _latest_notifier()
    after_read = re.search(r"SELECT (COALESCE\(MAX\(IFF\(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL\)\), '\d+'\)),\n",
                           body).group(1)
    parse = re.search(r"esc_after := (COALESCE\(TRY_TO_NUMBER\(TRIM\(:esc_after_s\)\), 0\));", body).group(1)
    assert parse.replace(":esc_after_s", after_read) == mart_sql.ESCALATE_AFTER_MIN_SQL
    assert mart_sql.ESCALATE_EMAIL_SQL in body
    assert "IF (COALESCE(TRIM(:esc_email), '') <> '') THEN" in body              # blank (after TRIM) = no email
    assert f"TRIM({mart_sql.ESCALATE_EMAIL_SQL}) FROM" in mart_sql.escalation_summary()
    from app.config import DEFAULT_SETTINGS
    assert f"'{DEFAULT_SETTINGS['ESCALATE_AFTER_MIN']}'" in after_read
    assert f"'{DEFAULT_SETTINGS['ESCALATE_EMAIL_INTEGRATION']}'" in mart_sql.ESCALATE_EMAIL_SQL


def test_escalation_summary_is_a_canary():
    from app.data.canary import CANARIES, EXPECTED_GAPS
    reg = dict(CANARIES)
    assert reg["riders.escalation_summary"]() == mart_sql.escalation_summary(7)
    assert "riders.escalation_summary" not in EXPECTED_GAPS          # green before and after V164: never a gap


# -- the wording ----------------------------------------------------------------------------------------------

def _row(**kw) -> pd.Series:
    base = {"ESCALATED_COUNT": 0, "LAST_ESCALATED_AT": None, "PASS_FAILURES": 0, "EMAIL_FAILURES": 0,
            "AFTER_MIN": 120, "EMAIL_INTEGRATION_NAME": "OVERWATCH_EMAIL", "WINDOW_DAYS": 7}
    base.update(kw)
    return pd.Series(base)


def _lines(**kw) -> list[tuple[str, str]]:
    from app.ui.pages.alerts import _escalation_lines
    return _escalation_lines(_row(**kw), pd.Timestamp("2026-09-29 10:00"))


def test_policy_on_with_email():
    (sev, policy), (_s2, tally) = _lines()
    assert sev == "info" and policy.startswith("Escalation: a CRITICAL nobody acknowledged within 2h ")
    assert "re-posted once to the route(s) that delivered it" in policy
    assert "emailed through the OVERWATCH_EMAIL notification integration (its DEFAULT_RECIPIENTS" in policy
    assert "snoozing" in policy and "incident" in policy
    # review W7: V164 counts only a human incident response after the alert joined; the V154 sweep's automatic
    # mitigation does not stop an escalation, so the policy line must not say it does
    assert "incident after the alert joined it, stops the escalation (an automatic mitigation does not)" in policy
    assert tally == "Last 7 days: 0 CRITICAL(s) escalated."
    assert "@" not in policy and "$" not in policy


@pytest.mark.parametrize("after", [0, -5, None, float("nan"), "abc"])
def test_policy_off(after):
    lines = _lines(AFTER_MIN=after)
    assert lines[0][1].startswith("Escalation is off (ESCALATE_AFTER_MIN is 0 or not a number)")


@pytest.mark.parametrize("integ", ["", "   ", None, float("nan")])
def test_policy_without_the_email_leg(integ):
    policy = _lines(EMAIL_INTEGRATION_NAME=integ)[0][1]
    assert "but not emailed (ESCALATE_EMAIL_INTEGRATION is blank)" in policy and "DEFAULT_RECIPIENTS" not in policy


def test_tally_and_failures():
    lines = _lines(ESCALATED_COUNT=3, LAST_ESCALATED_AT=pd.Timestamp("2026-09-29 07:00"), PASS_FAILURES=1,
                   EMAIL_FAILURES=2, AFTER_MIN=135)
    assert "within 2h 15m" in lines[0][1]
    assert lines[1] == ("info", "Last 7 days: 3 CRITICAL(s) escalated, the latest 3h ago.")
    sev, warn = lines[2]
    assert sev == "warn" and "1 escalation pass failure(s)" in warn and "2 escalation email failure(s)" in warn
    assert "DEFAULT_RECIPIENTS" in warn and "USAGE" in warn
    only_mail = _lines(EMAIL_FAILURES=1)[-1][1]
    assert "pass failure" not in only_mail and "1 escalation email failure(s)" in only_mail
    assert len(_lines()) == 2                                         # no failures -> no warning


def test_lines_never_raise_on_junk():
    from app.ui.pages.alerts import _escalation_lines
    for row in ({}, pd.Series(dtype=object), _row(ESCALATED_COUNT="x", WINDOW_DAYS=None, LAST_ESCALATED_AT="junk")):
        out = _escalation_lines(row, pd.Timestamp("2026-09-29"))
        assert out and all(sev in ("info", "warn") for sev, _t in out)


# -- the page wiring ------------------------------------------------------------------------------------------

def test_the_policy_is_gated_on_v164_before_any_read():
    src = page_source("alerts")
    body = src.split("def _escalation_status(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("if not has_migration(164, _PAGE):") < body.index("run(mart_sql.escalation_summary(7)")
    assert "tier=\"recent\"" in body
    render = src.split("def render(", 1)[1]
    assert render.count("_escalation_status()") == 1
    assert render.index("_last_delivery_card()") < render.index("_escalation_status()") < render.index(
        "**Routing (family → channel)**")
    help_block = render[render.index("**Routing (family → channel)**"):render.index("routes = run(mart_sql.alert_routes()")]
    assert "An escalation re-posts a CRITICAL only to the route(s) that already delivered it" in help_block
    assert "if has_migration(164, _PAGE) else \"\"" in help_block
    assert "from app.ui.schema_gate import has_migration" in src


def _texts(at) -> str:
    return " ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))


def _native_delivery(monkeypatch, applied: bool):
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import alerts
    seen: list[str] = []

    def _recording_run(*args, **kwargs):
        seen.append(str(args[0] if args else kwargs.get("sql", "")))
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(alerts, "run", _recording_run)
    if not applied:
        monkeypatch.setattr(alerts, "has_migration", lambda v, _page: int(v) < 164)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Alerts")
    at.session_state["alerts_section"] = "Native delivery"
    at.run()
    assert not at.exception, at.exception
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at, seen


@_SKIP
def test_native_delivery_states_the_policy_once_v164_is_applied(monkeypatch):
    at, seen = _native_delivery(monkeypatch, applied=True)
    assert sum("ACTION = 'ESCALATE'" in s for s in seen) == 1
    text = _texts(at)
    assert "Escalation: a CRITICAL nobody acknowledged within" in text
    assert "CRITICAL(s) escalated" in text
    assert "escalation email failure(s)" in text                        # shaped counts are non-zero -> the warning
    assert "**Routing (family → channel)**" in text


@_SKIP
def test_native_delivery_says_nothing_about_escalation_before_v164(monkeypatch):
    at, seen = _native_delivery(monkeypatch, applied=False)
    assert not any("ACTION = 'ESCALATE'" in s for s in seen)            # no read before the apply
    text = _texts(at)
    assert "scalation" not in text and "CRITICAL(s) escalated" not in text
    assert "**Routing (family → channel)**" in text                    # the section itself rendered
