"""Playbooks + health strip contracts."""

from app.data import mart_sql
from app.logic import navigate, playbooks


def test_every_deep_link_rule_has_a_specific_playbook():
    for rule in navigate._RULE_TARGETS:
        text = playbooks.playbook_for(rule)
        assert ("1." in text and rule not in text.upper()) or "**Means:**" in text
        assert text != playbooks.playbook_for("TOTALLY_UNKNOWN_RULE")


def test_retired_and_weekly_rule_playbooks_say_so():
    # SEC_BREAK_GLASS_USE: rule row deleted at V034 (owner ask 2026-07-10), scan arm [15] gone in V157;
    # its playbook must not read as a live rule in an old event's drawer (V034 closed OPEN/ACK only).
    bg = playbooks.PLAYBOOKS["SEC_BREAK_GLASS_USE"]
    assert bg.startswith("**Retired (V034") and "**Means:**" not in bg and "snoozed" not in bg
    # OPS_CANARY_FAIL: the canary runs weekly (Mondays 05:30 CT; the rule's window is 168h), so the
    # first-step query must look back the week, not 1 day (zero rows by Tuesday read as "cleared").
    canary = playbooks.PLAYBOOKS["OPS_CANARY_FAIL"]
    assert "DATEADD('day', -1," not in canary and "DATEADD('day', -7, CURRENT_TIMESTAMP())" in canary


def test_playbook_family_fallback():
    # v4.49: playbooks name exact pill labels, not the old "Cost > Spend" shorthand
    assert "Cost Intelligence > Spend & Attribution" in playbooks.playbook_for("COST_BRAND_NEW_RULE")
    assert "Security" in playbooks.playbook_for("SEC_SOMETHING")
    assert "add one" in playbooks.playbook_for("XYZ")


def test_health_strip_builder():
    sql = mart_sql.health_strip()
    assert "'OPEN_CRITICAL'" in sql and "'STALEST_SOURCE_H'" in sql and "'MTD_CREDITS'" in sql
    # SOURCE_FRESHNESS_STATE since V040 (r13 #2): the strip reads the 10-min
    # snapshot table, not the 19-aggregate view, every 30s per viewer.
    assert "ALERT_EVENTS" in sql and "SOURCE_FRESHNESS_STATE" in sql and "FACT_METERING_DAILY" in sql
    # rec #3 (v4.159.0): still critical-only, but OPEN_CRITICAL now counts OPEN+ACK
    # to match open_alert_severity_counts / the pages / the score.
    assert "UPPER(e.SEVERITY) = 'CRITICAL'" in sql
    assert "STATUS IN ('OPEN', 'ACK')" in sql
    # rec #2 (v4.159.0): MTD month boundary anchored to the account timezone so it
    # agrees with the account_today()-anchored Overview MTD, not the session clock.
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())" in sql
    assert "DATE_TRUNC('month', CURRENT_DATE())" not in sql
