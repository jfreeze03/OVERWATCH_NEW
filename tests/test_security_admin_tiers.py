"""Security signal for the 2026-10-05 access model (decision D14).

Membership of SNOW_PRI_GFR_PRD_ALFA_DSA now makes an OVERWATCH admin (full parity with the named
admins, account-level levers included), so a DSA grant must show up where the app watches admin-role
holders: ADMIN_HOLDER_ROLES feeds the privileged-role-holder panel and both new-network-login readers
(live and fact), and the alert parity constant ALERT_ADMIN_ROLES carries DSA after SNOW_SYSADMINS
(SP_ALERT_SCAN's SEC_LOGIN_TAKEOVER / SEC_ADMIN_GRANT arms gain it in V174; tests/test_security_alert_parity.py
locks the two together). SNOW_PRI_GFR_PRD_ALFA_DTI is view-only, so it joins no admin tier, and the
other tiers (break-glass, elevated, reaches-admin) answer different questions and stay as they were.
"""

from __future__ import annotations

from app.data import security_sql
from tests._source import read

_DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"
_DTI = "SNOW_PRI_GFR_PRD_ALFA_DTI"
_HOLDERS_IN = "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')"


def test_admin_holder_roles_append_dsa():
    assert security_sql.ADMIN_HOLDER_ROLES == ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", _DSA)


def test_every_admin_holder_reader_counts_dsa_members_and_never_dti():
    for sql in (security_sql.admin_role_holders(), security_sql.admin_role_holders("ALFA"),
                security_sql.new_network_logins(7), security_sql.new_network_logins(30, "TREXIS"),
                security_sql.new_network_logins_fact(7), security_sql.new_network_logins_fact(30, "ALFA")):
        assert _HOLDERS_IN in sql
        assert _DTI not in sql                                   # view-only is never an admin tier


def test_alert_admin_roles_append_dsa_after_the_owner_list():
    v162_then_dsa = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN",
                     "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", _DSA)
    assert v162_then_dsa == security_sql.ALERT_ADMIN_ROLES
    assert _DTI not in security_sql.ALERT_ADMIN_ROLES


def test_the_other_admin_tiers_are_not_widened():
    assert security_sql.BREAK_GLASS_ROLES == ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS")
    assert security_sql.ELEVATED_ROLES == (
        "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", "ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN")
    assert security_sql.REACHES_ADMIN_ROLES == (
        "SNOW_ACCOUNTADMINS", "ACCOUNTADMIN", "SNOW_SYSADMINS", "SECURITYADMIN")
    for role in (_DSA, _DTI):
        assert role not in security_sql.user_auth_inventory()          # ELEVATED_ROLES reader
        assert role not in security_sql.effective_access()             # REACHES_ADMIN_ROLES reader


def test_security_page_empty_holder_list_names_the_constant():
    """The privileged-role-holder panel's empty message used to hard-code the two SNOW_* roles."""
    page = read("app/ui/pages/security.py")
    assert "No SNOW_ACCOUNTADMINS/SNOW_SYSADMINS grants visible to this role." not in page
    assert '"/".join(security_sql.ADMIN_HOLDER_ROLES)' in page


def test_admin_role_holders_docstring_tracks_the_access_model():
    doc = " ".join((security_sql.admin_role_holders.__doc__ or "").split())
    assert _DSA in doc and "2026-10-05" in doc
    assert "the only roles with access are SNOW_ACCOUNTADMINS / SNOW_SYSADMINS" not in doc
