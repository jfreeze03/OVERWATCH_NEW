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
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    page_header,
    result_caption,
    styled_table,
)

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

    st.markdown("**Expiring credentials (30-day horizon)**")
    creds = run(security_sql.expiring_credentials(30, company), page=_PAGE,
                key=f"creds_{company}", tier="recent",
                source="ACCOUNT_USAGE.CREDENTIALS")
    if creds.ok and creds.empty:
        st.success("No credentials expiring within 30 days for this scope.")
    elif guard(creds, "", setup_hint="Needs the ACCOUNT_USAGE.CREDENTIALS view (newer accounts expose it by default)."):
        cdf = creds.df.copy()
        expired = int((cdf["STATUS"].astype(str).str.upper() == "EXPIRED").sum())
        kpi_row([
            {"label": "Expiring ≤30d", "value": f"{len(cdf) - expired}",
             "delta_color": "inverse" if len(cdf) - expired else "off"},
            {"label": "Already expired", "value": f"{expired}",
             "delta_color": "inverse" if expired else "off",
             "help": "Still-active rows past EXPIRES_AT — jobs using these will start failing."},
        ])
        styled_table(cdf, height=280)
        st.caption("The hourly alert scan raises SEC_CRED_EXPIRY events for these weekly until rotated (V009).")
        result_caption(creds)

    st.markdown("**Dormant users still holding access (90d+)**")
    _dorm_on = st.toggle("Run dormant-user scan (90 days of login + grant history)",
                         key="sec_dormant_toggle",
                         help="The heaviest scan on this page — runs only when you ask. The export pack always includes it.")
    if _dorm_on:
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


def _export_pack(company: str, days: int) -> None:
    """One-click access-review bundle: CSVs zipped in memory, stdlib only."""
    st.markdown("**Auditor export pack**")
    st.caption("Dormant users, MFA gaps, break-glass holders, and window grants as a timestamped zip of CSVs.")
    if not st.button("Build access-review pack", key="sec_pack_build"):
        return
    import io
    import zipfile
    from datetime import datetime

    sheets = {
        "dormant_users": insights_sql.dormant_users(90, company),
        "mfa_gaps_password_login": security_sql.users_without_mfa(company),
        "break_glass_holders": security_sql.admin_role_holders(),
        "role_grants_window": security_sql.recent_role_grants(days),
        "failed_logins_window": security_sql.failed_logins(days, company),
        "expiring_credentials_30d": security_sql.expiring_credentials(30, company),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    buffer = io.BytesIO()
    rows_written = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, sql in sheets.items():
            res = run(sql, page=_PAGE, key=f"pack_{name}", tier="recent",
                      source=name, max_rows=10_000)
            frame = res.df if res.ok else __import__("pandas").DataFrame({"ERROR": [res.error]})
            bundle.writestr(f"{name}.csv", frame.to_csv(index=False))
            rows_written[name] = len(frame) if res.ok else 0
        manifest = "\n".join(
            [f"OVERWATCH access review pack — {company} — generated {stamp}",
             f"Window: last {days} days (dormant users fixed at 90d)",
             *(f"{k}.csv: {v} rows" for k, v in rows_written.items())]
        )
        bundle.writestr("MANIFEST.txt", manifest)
    st.download_button(
        "Download access-review pack (.zip)", data=buffer.getvalue(),
        file_name=f"overwatch_access_review_{company}_{stamp}.zip", mime="application/zip",
        key="sec_pack_dl",
    )
    st.caption(f"{sum(rows_written.values()):,} rows across {len(sheets)} files.")


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

    st.markdown("**Failed-login reasons (network policy vs credentials)**")
    reasons = run(security_sql.failed_login_reasons(days, company), page=_PAGE,
                  key=f"login_reasons_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.LOGIN_HISTORY")
    if reasons.ok and reasons.empty:
        st.success("No failed logins in the window.")
    elif guard(reasons, ""):
        st.dataframe(reasons.df, hide_index=True, use_container_width=True)
        result_caption(reasons)

    st.markdown("**Break-glass role activity (should hug zero)**")
    bga = run(security_sql.admin_role_activity(days), page=_PAGE,
              key=f"breakglass_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (admin roles)")
    if bga.ok and bga.empty:
        st.success("No statements ran under ACCOUNTADMIN / SNOW_ACCOUNTADMINS in the window.")
    elif guard(bga, ""):
        st.dataframe(bga.df, hide_index=True, use_container_width=True)
        st.caption("SEC_BREAK_GLASS_USE alerts when a user exceeds the daily threshold (V011). "
                   "Routine work belongs on SNOW_SYSADMINS.")
        result_caption(bga)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Security", "Access posture and change evidence.",
                scope_note=f"{f['company']} · last {f['days']} days")
    st.caption(
        "Access control is Snowflake RBAC — this page reports posture; it does not grant or "
        "revoke anything. Company scoping is a shared-account view filter, not isolation."
    )
    section = lazy_sections(["Access", "Changes"], key="sec_section")
    if section == "Access":
        _access_tab(f["company"], f["days"])
        st.divider()
        _export_pack(f["company"], f["days"])
    else:
        _changes_tab(f["company"], f["days"], f["database"], f["schema_contains"])
