"""Permanent parity lock: the hourly identity alerts vs the app's constants (Next-Fifty #39, V162 onward).

SP_ALERT_SCAN's arms [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT carry the owner's admin-tier role list and
off-hours window (20:00-06:00 America/Chicago plus weekends, owner 2026-09-29) as SQL literals. The app mirrors
them in app/data/security_sql.ALERT_ADMIN_ROLES / OFF_HOURS_*. This reads the LATEST SP_ALERT_SCAN (last-wins over
the migration set, like the live account), so a later re-derivation that edits either side without the other --
or drops an arm -- fails here, not in production.
"""

from __future__ import annotations

import re

from app.data import security_sql
from tests.test_alert_rule_consistency import _latest_proc_bodies


def _arm(body: str, head: str, nxt: str) -> str:
    i = body.index(head)
    return body[i:body.index(nxt, i)]


def _arms() -> tuple[str, str]:
    scan = _latest_proc_bodies()["SP_ALERT_SCAN"]
    return (_arm(scan, "    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT"),
            _arm(scan, "    -- [27] SEC_ADMIN_GRANT", "\n    END;\n"))


def _in_list(arm: str) -> tuple[str, ...]:
    (inlist,) = re.findall(r"\bROLE IN \(([^)]*)\)", arm)
    return tuple(re.findall(r"'(\w+)'", inlist))


def test_admin_role_list_matches_the_app_constant_in_both_arms():
    takeover, grant = _arms()
    assert _in_list(takeover) == security_sql.ALERT_ADMIN_ROLES
    assert _in_list(grant) == security_sql.ALERT_ADMIN_ROLES
    # the owner's seven (2026-09-29) plus SNOW_PRI_GFR_PRD_ALFA_DSA (2026-10-05, V174), no duplicates
    assert len(set(security_sql.ALERT_ADMIN_ROLES)) == len(security_sql.ALERT_ADMIN_ROLES) == 8
    assert security_sql.ALERT_ADMIN_ROLES[-1] == "SNOW_PRI_GFR_PRD_ALFA_DSA"
    # the app's older admin tiers are untouched by the alert list (different questions, different lists)
    assert set(security_sql.ELEVATED_ROLES) < set(security_sql.ALERT_ADMIN_ROLES)


def test_off_hours_window_matches_the_app_constants_in_both_arms():
    takeover, grant = _arms()
    for arm, col in ((takeover, "a.TS_CT"), (grant, "g.CREATED_CT")):
        expr = (f"HOUR({col}) >= {security_sql.OFF_HOURS_START_HOUR} OR HOUR({col}) < "
                f"{security_sql.OFF_HOURS_END_HOUR} OR DAYOFWEEKISO({col}) >= {min(security_sql.OFF_HOURS_WEEKEND_ISO)}")
        assert expr in arm, (col, expr)
        # every HOUR / DAYOFWEEKISO literal in the arm is one of the three constants (no stray second window)
        assert set(re.findall(r"HOUR\([\w.]+\) (>=|<) (\d+)", arm)) == {
            (">=", str(security_sql.OFF_HOURS_START_HOUR)), ("<", str(security_sql.OFF_HOURS_END_HOUR))}
    assert tuple(range(min(security_sql.OFF_HOURS_WEEKEND_ISO), 8)) == security_sql.OFF_HOURS_WEEKEND_ISO
    # the clocks are Central wall clocks of the event itself
    assert "CONVERT_TIMEZONE('America/Chicago', TS)::TIMESTAMP_NTZ AS TS_CT" in takeover
    assert "CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT" in grant


def test_new_admin_network_arm_keeps_its_own_three_roles():
    """[18] SEC_NEW_ADMIN_NETWORK predates the owner's list and deliberately keeps its narrower set."""
    scan = _latest_proc_bodies()["SP_ALERT_SCAN"]
    arm18 = _arm(scan, "    -- [18] SEC_NEW_ADMIN_NETWORK", "    -- [20] SEC_NEW_EXPOSURE")
    assert _in_list(arm18) == ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")


def test_the_parity_check_has_teeth():
    takeover, _ = _arms()
    assert takeover.count("'SECURITYADMIN', ") == 1
    assert _in_list(takeover.replace("'SECURITYADMIN', ", "")) != security_sql.ALERT_ADMIN_ROLES
    assert "HOUR(a.TS_CT) >= 21" not in takeover
