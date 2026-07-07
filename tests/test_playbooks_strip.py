"""Playbooks + health strip contracts."""

from app.data import mart_sql
from app.logic import navigate, playbooks


def test_every_deep_link_rule_has_a_specific_playbook():
    for rule in navigate._RULE_TARGETS:
        text = playbooks.playbook_for(rule)
        assert ("1." in text and rule not in text.upper()) or "**Means:**" in text
        assert text != playbooks.playbook_for("TOTALLY_UNKNOWN_RULE")


def test_playbook_family_fallback():
    assert "Cost > Spend" in playbooks.playbook_for("COST_BRAND_NEW_RULE")
    assert "Security" in playbooks.playbook_for("SEC_SOMETHING")
    assert "add one" in playbooks.playbook_for("XYZ")


def test_health_strip_builder():
    sql = mart_sql.health_strip()
    assert "'OPEN_CRITICAL'" in sql and "'STALEST_SOURCE_H'" in sql and "'MTD_CREDITS'" in sql
    assert "ALERT_EVENTS" in sql and "MART_SOURCE_FRESHNESS" in sql and "FACT_METERING_DAILY" in sql
    assert "SEVERITY = 'CRITICAL'" in sql
    assert "DATE_TRUNC('month', CURRENT_DATE())" in sql
