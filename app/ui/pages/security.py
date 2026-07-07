"""Security — access posture and change evidence.

Navigation profiles are cosmetic; Snowflake RBAC is the boundary. This page
says so out loud instead of pretending otherwise (old-app review point).
"""

from __future__ import annotations

import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.state import filters
from app.data import insights_sql, security_sql
from app.logic.insights import dormant_severity
from app.ui import charts
from app.ui.components import guard, kpi_row, page_header, result_caption, styled_table

_PAGE = "Security"


def _access_tab(company: str, days: int) -> None:
    mfa = run(security_sql.users_without_mfa(company), page=_PAGE, key=f"mfa_{company}",
              tier="historical", source="ACCOUNT_USAGE.USERS + LOGIN_HISTORY")
    st.markdown("**MFA gaps with password-login evidence (30d)**")
    if mfa.ok and mfa.empty:
        st.success("No active users are password-logging-in without MFA. SSO/key-pair users are excluded by design.")
    elif guard(mfa, ""):
        kpi_row([{
            "label": "Users needing MFA now",
            "value": f"{len(mfa.df)}",
            "help": "Password logins in the last 30 days and no MFA. SSO/key-pair-only users are not listed.",
        }])
        st.dataframe(mfa.df, hide_index=True, use_container_width=True)
        result_caption(mfa)

    left, right = st.columns(2)
    with left:
        st.markdown("**Failed logins**")
        res = run(security_sql.failed_logins(days, company), page=_PAGE,
                  key=f"faillog_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.LOGIN_HISTORY")
        if res.ok and res.empty:
            st.success("No failed logins in this window.")
        elif guard(res, ""):
            st.dataframe(res.df, hide_index=True, use_container_width=True)
    with right:
        st.markdown("**Break-glass role holders**")
        res = run(security_sql.admin_role_holders(), page=_PAGE, key="admins",
                  tier="metadata", source="ACCOUNT_USAGE.GRANTS_TO_USERS")
        if guard(res, "No ACCOUNTADMIN/SECURITYADMIN/ORGADMIN grants visible to this role."):
            st.dataframe(res.df, hide_index=True, use_container_width=True)
            st.caption("This list should be short and every name should be expected.")

    st.markdown("**Dormant users still holding access (90d+)**")
    res = run(insights_sql.dormant_users(90, company), page=_PAGE, key=f"dormant_{company}",
              tier="historical", source="ACCOUNT_USAGE.USERS + GRANTS_TO_USERS")
    if res.ok and res.empty:
        st.success("No enabled users dormant 90+ days in this scope.")
    elif guard(res, ""):
        ranked = dormant_severity(res.df)
        high = ranked[ranked["SEVERITY"] == "High"]
        kpi_row([
            {"label": "Dormant users", "value": f"{len(ranked)}"},
            {"label": "High severity", "value": f"{len(high)}",
             "help": "180+ days dormant, or 5+ roles still granted.",
             "delta_color": "inverse" if len(high) else "off"},
        ])
        styled_table(
            ranked[["SEVERITY", "USER_NAME", "EMAIL", "DAYS_DORMANT", "ROLE_COUNT", "ROLES", "LAST_SUCCESS_LOGIN"]],
        )
        st.caption("Review with the owner before disabling; service accounts may log in rarely by design.")
        result_caption(res)

    st.markdown("**Role grants in the window (account-wide)**")
    res = run(security_sql.recent_role_grants(days), page=_PAGE, key=f"grants_{days}",
              tier="recent", source="ACCOUNT_USAGE.GRANTS_TO_USERS")
    if res.ok and res.empty:
        st.success("No new role grants in this window.")
    elif guard(res, ""):
        st.dataframe(res.df, hide_index=True, use_container_width=True)
        result_caption(res)


def _changes_tab(company: str, days: int, database: str = "", schema_contains: str = "") -> None:
    st.markdown("**Who changed what (DDL/DCL)**")
    res = run(security_sql.recent_ddl_changes(days, company, database, schema_contains), page=_PAGE,
              key=f"ddl_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (DDL/DCL types)")
    if res.ok and res.empty:
        st.success("No DDL/DCL changes recorded in this window for this scope.")
        return
    if guard(res, ""):
        daily = res.df.groupby("DAY", as_index=False)["STATEMENTS"].sum()
        charts.bar_count(daily.sort_values("DAY"), "DAY", "STATEMENTS", title="Change statements/day")
        st.dataframe(res.df, hide_index=True, use_container_width=True)
        result_caption(res)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Security", "Access posture and change evidence.",
                scope_note=f"{f['company']} · last {f['days']} days")
    st.caption(
        "Access control is Snowflake RBAC — this page reports posture; it does not grant or "
        "revoke anything. Company scoping is a shared-account view filter, not isolation."
    )
    tab_access, tab_changes = st.tabs(["Access", "Changes"])
    with tab_access:
        _access_tab(f["company"], f["days"])
    with tab_changes:
        _changes_tab(f["company"], f["days"], f["database"], f["schema_contains"])
