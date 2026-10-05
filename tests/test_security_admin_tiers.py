"""Security signal for the 2026-10-05 access model (decision D14).

Membership of SNOW_PRI_GFR_PRD_ALFA_DSA now makes an OVERWATCH admin (full parity with the named
admins, account-level levers included), so a DSA grant must show up where the app watches admin-role
holders: ADMIN_HOLDER_ROLES feeds the privileged-role-holder panel and both new-network-login readers
(live and fact). The alert parity constant ALERT_ADMIN_ROLES gains DSA together with V174 (SP_ALERT_SCAN's
SEC_LOGIN_TAKEOVER / SEC_ADMIN_GRANT arms), because tests/test_security_alert_parity.py pins the constant to
the LATEST SP_ALERT_SCAN; that lock and V174's own tests own the append. SNOW_PRI_GFR_PRD_ALFA_DTI is
view-only, so it joins no admin tier. The other tiers (break-glass, elevated, reaches-admin) answer
different questions and stay as they were (owner-scoped D14); the two ELEVATED_ROLES checks say out loud
which admin-holder role they skip.
"""

from __future__ import annotations

import pandas as pd

from app import companies
from app.data import security_sql
from app.logic.least_privilege import recommend_for_sheet
from tests._source import read

_DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"
_DTI = "SNOW_PRI_GFR_PRD_ALFA_DTI"
_HOLDERS_IN = "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')"


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def test_admin_holder_roles_append_dsa():
    assert security_sql.ADMIN_HOLDER_ROLES == ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", _DSA)


def test_every_admin_holder_reader_counts_dsa_members_and_never_dti():
    readers = (security_sql.admin_role_holders, security_sql.new_network_logins,
               security_sql.new_network_logins_fact)
    for build in readers:
        args = (7,) if build is not security_sql.admin_role_holders else ()
        sql = build(*args)                                              # the ALL scope: no company filter
        assert _HOLDERS_IN in sql
        assert "COMPANY_FOR_USER(GRANTEE_NAME)" not in sql
        assert _DTI not in sql                                          # view-only is never an admin tier
        # the company spellings come from companies.COMPANIES ('Trexis', not 'TREXIS', which user_clause
        # silently treats as ALL); DSA must survive the scoping, so the filter sits right beside the IN list
        for company in ("ALFA", "Trexis"):
            assert company in companies.COMPANIES
            scoped = build(*args, company)
            assert (_HOLDERS_IN + f" AND {companies.COMPANY_FOR_USER_FN}(GRANTEE_NAME) = '{company}'") in scoped
            assert _DTI not in scoped


def test_alert_admin_roles_never_carry_dti_and_no_tier_repeats_a_role():
    # DSA's arrival in ALERT_ADMIN_ROLES is V174's (test_security_alert_parity + test_v174 pin it); whatever
    # the list holds, the view-only role is never in it and no tier lists a role twice
    assert _DTI not in security_sql.ALERT_ADMIN_ROLES
    for tier in (security_sql.ADMIN_HOLDER_ROLES, security_sql.ALERT_ADMIN_ROLES, security_sql.BREAK_GLASS_ROLES,
                 security_sql.ELEVATED_ROLES, security_sql.REACHES_ADMIN_ROLES):
        assert len(set(tier)) == len(tier)


def test_the_other_admin_tiers_are_not_widened():
    # owner-scoped D14 (2026-10-05): only ADMIN_HOLDER_ROLES (+ ALERT_ADMIN_ROLES via V174) gain DSA; widening
    # the ELEVATED_ROLES checks (admin MFA, admin network policy) is a separate owner decision -- until then
    # their help text names the gap (test_elevated_checks_name_the_admin_holders_they_skip)
    assert security_sql.BREAK_GLASS_ROLES == ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS")
    assert security_sql.ELEVATED_ROLES == (
        "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", "ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN")
    assert security_sql.REACHES_ADMIN_ROLES == (
        "SNOW_ACCOUNTADMINS", "ACCOUNTADMIN", "SNOW_SYSADMINS", "SECURITYADMIN")
    for role in (_DSA, _DTI):
        assert role not in security_sql.user_auth_inventory()          # ELEVATED_ROLES reader
        assert role not in security_sql.admin_network_policy_coverage()  # ELEVATED_ROLES reader
        assert role not in security_sql.effective_access()             # REACHES_ADMIN_ROLES reader


def test_elevated_checks_name_the_admin_holders_they_skip():
    """'Admins with password and no MFA' and 'Admins without a user network policy' read ELEVATED_ROLES, which
    leaves out the DSA admins; a clean count must not read as covering every OVERWATCH admin."""
    import app.ui.pages.security as sec
    assert security_sql.ADMIN_HOLDERS_OUTSIDE_ELEVATED == (_DSA,)
    note = sec._elevated_gap_note()
    assert _DSA in note and "Not checked here" in note and "Privileged role holders" in note
    page = read("app/ui/pages/security.py")
    # both KPI helps append it: the MFA KPI and the network-policy KPI
    assert page.count("+ _elevated_gap_note()") == 2


def test_elevated_gap_note_is_silent_once_the_tiers_agree(monkeypatch):
    import app.ui.pages.security as sec
    monkeypatch.setattr(security_sql, "ADMIN_HOLDERS_OUTSIDE_ELEVATED", ())
    assert sec._elevated_gap_note() == ""


def test_security_page_empty_holder_list_names_the_constant():
    """The privileged-role-holder panel's empty message used to hard-code the two SNOW_* roles."""
    page = read("app/ui/pages/security.py")
    assert "No SNOW_ACCOUNTADMINS/SNOW_SYSADMINS grants visible to this role." not in page
    assert '"/".join(security_sql.ADMIN_HOLDER_ROLES)' in page


def test_holder_panel_states_the_grants_to_users_lag():
    """In-app DSA admin is resolved live each session, while GRANTS_TO_USERS lags up to ~2 hours, so an empty or
    short holder list must not read as the live admin list (both the empty and the filled path say so)."""
    import app.ui.pages.security as sec
    note = _flat(sec._HOLDER_LAG_NOTE)
    assert "2 hours" in note and "live" in note
    page = read("app/ui/pages/security.py")
    assert page.count("_HOLDER_LAG_NOTE") == 3                         # the definition + both render paths


def test_privileged_holder_labels_do_not_say_break_glass():
    """admin_role_holders lists ADMIN_HOLDER_ROLES (SNOW_SYSADMINS and DSA are not BREAK_GLASS_ROLES), so the
    auditor sheet, its RECOMMEND map, the new-network clean message and the builder docstring say privileged."""
    page = read("app/ui/pages/security.py")
    assert '"privileged_role_holders": security_sql.admin_role_holders(company)' in page
    assert "break_glass_holders" not in page
    assert "privileged_role_holders: direct holders of " in page        # the manifest names the role list
    assert "No break-glass account logged in" not in page
    assert "No privileged (admin-role) account logged in from a network unseen" in page
    frame = recommend_for_sheet("privileged_role_holders", pd.DataFrame({"USER_NAME": ["A"]}))
    assert frame is not None and frame.columns[0] == "RECOMMEND"
    assert recommend_for_sheet("break_glass_holders", pd.DataFrame({"USER_NAME": ["A"]})).columns.tolist() == [
        "USER_NAME"]
    doc = _flat(security_sql.new_network_logins.__doc__)
    assert "for break-glass users" not in doc and "ADMIN_HOLDER_ROLES" in doc


def test_every_recommend_key_is_an_export_pack_sheet():
    """A renamed pack sheet must take its RECOMMEND entry with it (the rename silently drops the column)."""
    from app.logic.least_privilege import _ACCESS_REVIEW_RECOMMEND
    page = read("app/ui/pages/security.py")
    for sheet in _ACCESS_REVIEW_RECOMMEND:
        assert f'        "{sheet}": ' in page, sheet

def test_admin_role_holders_docstring_tracks_the_access_model():
    doc = _flat(security_sql.admin_role_holders.__doc__)
    assert _DSA in doc and "2026-10-05" in doc
    assert "the only roles with access are SNOW_ACCOUNTADMINS / SNOW_SYSADMINS" not in doc

