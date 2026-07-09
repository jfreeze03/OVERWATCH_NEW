"""Security — access posture and change evidence.

Navigation profiles are cosmetic; Snowflake RBAC is the boundary. This page
says so out loud instead of pretending otherwise (old-app review point).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run, run_batch
from app.core.state import filters
from app.data import insights_sql, security_sql
from app.logic.governance import governance_drift, resolve_gov_weights
from app.logic.insights import dormant_severity
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    page_header,
    panel_help,
    result_caption,
    styled_table,
)

_PAGE = "Security"


def _access_tab(company: str, days: int) -> None:
    # Parallel path: the tab's five independent queries submit server-side
    # async in one shot (~1 round trip instead of five serial). Any failure
    # falls back to the serial per-query calls below, unchanged.
    batch = run_batch([
        {"key": "mfa", "sql": security_sql.users_without_mfa(company),
         "source": "ACCOUNT_USAGE.USERS + LOGIN_HISTORY"},
        {"key": "creds", "sql": security_sql.expiring_credentials(30, company),
         "source": "ACCOUNT_USAGE.CREDENTIALS"},
        {"key": "logins", "sql": security_sql.failed_logins(days, company),
         "source": "ACCOUNT_USAGE.LOGIN_HISTORY"},
        {"key": "admins", "sql": security_sql.admin_role_holders(),
         "source": "ACCOUNT_USAGE.GRANTS_TO_USERS"},
        {"key": "grants", "sql": security_sql.recent_role_grants(days),
         "source": "ACCOUNT_USAGE.GRANTS_TO_USERS"},
    ], page=_PAGE, tier="historical") or {}

    mfa = batch.get("mfa") or run(security_sql.users_without_mfa(company), page=_PAGE, key=f"mfa_{company}",
              tier="historical", source="USERS + FACT_LOGIN_DAILY (mart-first)")
    if not mfa.ok or mfa.empty:
        # Fact empty/undeployed: an empty evidence set must never read as
        # "all clear" — prove it against live LOGIN_HISTORY before celebrating.
        live_mfa = run(security_sql.users_without_mfa_live(company), page=_PAGE,
                       key=f"mfa_live_{company}", tier="historical",
                       source="ACCOUNT_USAGE.USERS + LOGIN_HISTORY (live fallback)")
        if live_mfa.ok:
            mfa = live_mfa
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
        res = batch.get("logins") or run(security_sql.failed_logins(days, company), page=_PAGE,
                  key=f"faillog_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.LOGIN_HISTORY")
        if res.ok and res.empty:
            st.success("No failed logins in this window.")
        elif guard(res, ""):
            st.dataframe(res.df, hide_index=True, use_container_width=True)
    with right:
        st.markdown("**Break-glass role holders**")
        res = batch.get("admins") or run(security_sql.admin_role_holders(), page=_PAGE, key="admins",
                  tier="metadata", source="ACCOUNT_USAGE.GRANTS_TO_USERS")
        if guard(res, "No ACCOUNTADMIN/SECURITYADMIN/ORGADMIN grants visible to this role."):
            st.dataframe(res.df, hide_index=True, use_container_width=True)
            st.caption("This list should be short and every name should be expected.")

    st.markdown("**Expiring credentials (30-day horizon)**")
    creds = batch.get("creds") or run(security_sql.expiring_credentials(30, company), page=_PAGE,
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
        st.caption("The hourly alert scan raises SEC_CRED_EXPIRY events for these weekly until rotated.")
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
    res = batch.get("grants") or run(security_sql.recent_role_grants(days), page=_PAGE, key=f"grants_{days}",
              tier="recent", source="ACCOUNT_USAGE.GRANTS_TO_USERS")
    if res.ok and res.empty:
        st.success("No new role grants in this window.")
    elif guard(res, ""):
        st.dataframe(res.df, hide_index=True, use_container_width=True)
        result_caption(res)


def _trust_center_tab() -> None:
    st.caption("Latest Trust Center scanner results — the account already pays for these scans.")
    panel_help(
        "Source: SNOWFLAKE.TRUST_CENTER.FINDINGS (latest run per scanner). Needs the "
        "TRUST_CENTER_VIEWER application role. CRITICAL/HIGH rows list at-risk entities "
        "in Snowsight's Trust Center; fix there, then re-scan."
    )
    tcf = run(security_sql.trust_center_findings(), page=_PAGE, key="trust_center",
              tier="historical", source="SNOWFLAKE.TRUST_CENTER.FINDINGS")
    if tcf.ok and tcf.empty:
        st.success("No findings — every scanner came back clean.")
    elif guard(tcf, "", setup_hint="Grant SNOWFLAKE.TRUST_CENTER_VIEWER to your role and enable Trust Center scanners."):
        fdf = tcf.df.copy()
        sev = fdf["SEVERITY"].astype(str).str.upper()
        kpi_row([
            {"label": "Scanners reporting", "value": f"{len(fdf)}"},
            {"label": "Critical", "value": f"{int((sev == 'CRITICAL').sum())}",
             "delta_color": "inverse" if (sev == "CRITICAL").any() else "off"},
            {"label": "High", "value": f"{int((sev == 'HIGH').sum())}",
             "delta_color": "inverse" if (sev == "HIGH").any() else "off"},
        ])
        styled_table(fdf, height=300)
        result_caption(tcf)


def _governance_score_panel() -> None:
    """Governance debt as a number with named deductions (CoCo item 14a)."""
    counts = run(security_sql.governance_counts(), page=_PAGE, key="gov_counts",
                 tier="historical", source="USERS + CREDENTIALS + GRANTS_TO_USERS")
    whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="gov_show_wh",
              tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    inputs: dict = {}
    if counts.usable():
        row = counts.df.iloc[0]
        inputs = {
            "mfa_gap_users": row.get("MFA_GAP_USERS"),
            "expired_credentials": row.get("EXPIRED_CREDENTIALS"),
            "expiring_credentials": row.get("EXPIRING_CREDENTIALS"),
            "breakglass_grants_30d": row.get("BREAKGLASS_GRANTS_30D"),
        }
    if whs.ok and not whs.empty:
        wdf = whs.df.copy()
        wdf.columns = [str(c).lower() for c in wdf.columns]
        if "resource_monitor" in wdf.columns:
            rm = wdf["resource_monitor"].astype(str).str.strip().str.lower()
            inputs["warehouses_no_monitor"] = int(((rm == "null") | (rm == "") | (rm == "none")).sum())
        if "auto_suspend" in wdf.columns:
            asus = pd.to_numeric(wdf["auto_suspend"], errors="coerce").fillna(0)
            inputs["warehouses_no_autosuspend"] = int((asus <= 0).sum())
    if not inputs:
        return
    settings = load_settings(_PAGE)
    drift = governance_drift(inputs, weights=resolve_gov_weights(settings))
    kpi_row([
        {"label": "Governance drift score", "value": f"{drift.score}/100",
         "delta": drift.state, "delta_color": "off",
         "help": "Countable hygiene debt: MFA gaps, credential rotation, break-glass "
                 "grants, unmonitored warehouses. Fixed weights, capped per category."},
        {"label": "Deductions", "value": f"{len(drift.drivers)}"},
    ])
    if drift.drivers:
        with st.expander(f"Governance deductions ({drift.score}/100 · {drift.state})"):
            for d in drift.drivers:
                st.markdown(f"- **{d.driver}** −{d.penalty:.1f} pts — {d.evidence}")
    st.divider()


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
        "role_privilege_matrix": security_sql.role_privilege_matrix(),
        "unused_roles_90d": security_sql.unused_roles(90),
        "direct_role_grants": security_sql.direct_role_grants(),
        "grant_changes_90d": security_sql.grant_changes(90),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    buffer = io.BytesIO()
    rows_written = {}
    # Ten sheets, one parallel batch (Codex r3 #12); any batch failure falls
    # back to the original serial per-sheet path with its own caching.
    _pack_batch = run_batch(
        [{"key": name, "sql": sql, "source": name, "max_rows": 10_000}
         for name, sql in sheets.items()],
        page=_PAGE, tier="recent")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, sql in sheets.items():
            res = (_pack_batch or {}).get(name) or run(
                sql, page=_PAGE, key=f"pack_{name}", tier="recent",
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
        charts.daily_count_bars(daily.sort_values("DAY"), "DAY", "STATEMENTS", title="Change statements/day")
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

    st.markdown("**Unused roles (90d) — revoke fodder**")
    ur = run(security_sql.unused_roles(90), page=_PAGE, key="unused_roles", tier="historical",
             source="ROLES x QUERY_HISTORY (90d)")
    if ur.ok and ur.empty:
        st.success("Every active role was assumed in the last 90 days.")
    elif guard(ur, ""):
        st.dataframe(ur.df, hide_index=True, use_container_width=True)
        st.caption("Also in the quarterly export pack with the full grant matrix and 90d diff.")
        result_caption(ur)

    st.markdown("**Break-glass role activity (should hug zero)**")
    bga = run(security_sql.admin_role_activity(days), page=_PAGE,
              key=f"breakglass_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (admin roles)")
    if bga.ok and bga.empty:
        st.success("No statements ran under ACCOUNTADMIN / SNOW_ACCOUNTADMINS in the window.")
    elif guard(bga, ""):
        st.dataframe(bga.df, hide_index=True, use_container_width=True)
        st.caption("SEC_BREAK_GLASS_USE alerts when a user exceeds the daily threshold. "
                   "Routine work belongs on SNOW_SYSADMINS.")
        result_caption(bga)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Security & Governance", "Hygiene and governance posture — not a threat-detection SOC (that scope is roadmap, deliberately).", icon_name="security",
                scope_note=f"{f['company']} · last {f['days']} days")
    st.caption(
        "Access control is Snowflake RBAC — this page reports posture; it does not grant or "
        "revoke anything. Company scoping is a shared-account view filter, not isolation."
    )
    _governance_score_panel()
    section = lazy_sections(["Access", "Changes", "Trust Center"], key="sec_section")
    if section == "Access":
        _access_tab(f["company"], f["days"])
        st.divider()
        _export_pack(f["company"], f["days"])
    elif section == "Changes":
        _changes_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    else:
        _trust_center_tab()
