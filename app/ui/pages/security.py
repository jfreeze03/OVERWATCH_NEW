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
from app.data import insights_sql, mart27_sql, security_sql
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
    run_mart_first,
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
        {"key": "creds", "sql": security_sql.expiring_credentials(10, company),
         "source": "ACCOUNT_USAGE.CREDENTIALS"},
        {"key": "logins", "sql": security_sql.failed_logins(days, company),
         "source": "ACCOUNT_USAGE.LOGIN_HISTORY"},
        {"key": "admins", "sql": security_sql.admin_role_holders(),
         "source": "ACCOUNT_USAGE.GRANTS_TO_USERS"},
        {"key": "grants", "sql": security_sql.recent_role_grants(days),
         "source": "ACCOUNT_USAGE.GRANTS_TO_USERS"},
        {"key": "newnet", "sql": security_sql.new_network_logins(days),
         "source": "ACCOUNT_USAGE.LOGIN_HISTORY (admin users, 90d baseline)"},
    ], page=_PAGE, tier="historical")

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
        st.success("No active user logs in with a password but no MFA. SSO/key-pair users are excluded by design.")
    elif guard(mfa, ""):
        kpi_row([{
            "label": "Users needing MFA now",
            "value": f"{len(mfa.df)}",
            "help": "Password logins in the last 30 days and no MFA. SSO/key-pair-only users are not listed.",
        }])
        styled_table(mfa.df)
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
            styled_table(res.df)
    with right:
        st.markdown("**Break-glass role holders**")
        res = batch.get("admins") or run(security_sql.admin_role_holders(), page=_PAGE, key="admins",
                  tier="metadata", source="ACCOUNT_USAGE.GRANTS_TO_USERS")
        if guard(res, "No SNOW_ACCOUNTADMINS/SNOW_SYSADMINS grants visible to this role."):
            styled_table(res.df)
            st.caption("This list should be short and every name should be expected.")

    st.markdown("**New networks for privileged users (90-day baseline)**")
    nn = batch.get("newnet") or run(security_sql.new_network_logins(days), page=_PAGE,
              key=f"newnet_{days}", tier="recent",
              source="LOGIN_HISTORY x admin grants (90d baseline)")
    if nn.ok and nn.empty:
        st.success("No break-glass account logged in from a network unseen in the last 90 days.")
    elif guard(nn, ""):
        kpi_row([{
            "label": "New networks", "value": f"{len(nn.df)}", "delta_color": "inverse",
            "help": "An admin-role user's (user, IP) pair first appeared inside this window. "
                    "An IP quiet for 90+ days re-flags on purpose — better a stale re-flag "
                    "than a silent novel network.",
        }])
        styled_table(nn.df)
        st.caption("Expected after travel, VPN changes, or a new automation host — anything else is the finding.")
        result_caption(nn)

    st.markdown("**Expiring credentials (10-day horizon)**")
    creds = batch.get("creds") or run(security_sql.expiring_credentials(10, company), page=_PAGE,
                key=f"creds_{company}", tier="recent",
                source="ACCOUNT_USAGE.CREDENTIALS")
    if creds.ok and creds.empty:
        st.success("No credentials expiring within 10 days for this scope.")
    elif guard(creds, "", setup_hint="Needs the ACCOUNT_USAGE.CREDENTIALS view (newer accounts expose it by default)."):
        cdf = creds.df.copy()
        expired = int((cdf["STATUS"].astype(str).str.upper() == "EXPIRED").sum())
        kpi_row([
            {"label": "Expiring ≤10d", "value": f"{len(cdf) - expired}",
             "delta_color": "inverse" if len(cdf) - expired else "off"},
            {"label": "Already expired", "value": f"{expired}",
             "delta_color": "inverse" if expired else "off",
             "help": "Still-active rows past EXPIRES_AT — jobs using these will start failing."},
        ])
        styled_table(cdf, height=280)
        st.caption("The hourly scan raises SEC_CRED_EXPIRY for these — re-raised weekly until rotated.")
        result_caption(creds)

    st.markdown("**Dormant users still holding access (90d+)**")
    from app.ui.components import toggle_cost_hint
    st.caption(toggle_cost_hint("dormant"))
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
        styled_table(res.df)
        result_caption(res)


def _egress_tab(company: str, days: int) -> None:
    """r25 #7 (owner pick): data leaving the account. Two lenses — the
    outbound transfer bill (DATA_TRANSFER_HISTORY) and who unloads to stages
    (QUERY_TYPE='UNLOAD') — because exfiltration and a surprise egress bill
    both start as 'bytes moved that nobody was watching'."""
    st.caption("Data leaving the account: outbound transfer by destination, and who unloads to stages.")
    st.markdown("**Outbound transfer (account-wide)**")
    xfer = run(security_sql.egress_daily(days), page=_PAGE, key=f"egress_{days}",
               tier="recent", source="ACCOUNT_USAGE.DATA_TRANSFER_HISTORY")
    if xfer.ok and xfer.empty:
        st.success("No cross-cloud or cross-region transfer in this window.")
    elif guard(xfer, "", setup_hint="Needs the ACCOUNT_USAGE.DATA_TRANSFER_HISTORY view."):
        xdf = xfer.df.copy()
        _by_tgt = xdf.groupby("TARGET_REGION")["GB"].sum().sort_values(ascending=False)
        kpi_row([
            {"label": f"Egress GB · {days}d", "value": f"{float(xdf['GB'].sum()):,.1f}"},
            {"label": "Top destination", "value": str(_by_tgt.index[0]) if len(_by_tgt) else "—",
             "help": "Region receiving the most bytes. An unexpected destination is the finding."},
            {"label": "Destinations", "value": f"{int((_by_tgt > 0).sum())}"},
        ])
        charts.daily_stacked_count(xdf, "DAY", "TARGET_REGION", "GB", title="GB by destination region")
        styled_table(xdf.sort_values("GB", ascending=False).head(50), height=240)
        result_caption(xfer)

    st.markdown("**Unload activity (COPY INTO stage)**")
    panel_help(
        "QUERY_HISTORY filtered to QUERY_TYPE='UNLOAD' — every successful COPY INTO "
        "<location>, grouped per user/day. GB_OUT sums the query's byte counters (for "
        "an unload exactly one carries the payload). SAMPLE_TARGET previews the newest "
        "statement so the destination is visible without a drill."
    )
    unl = run(security_sql.unload_activity(days, company), page=_PAGE,
              key=f"unload_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (UNLOAD only)")
    if unl.ok and unl.empty:
        st.success("No unloads to stages in this window for this scope.")
    elif guard(unl, ""):
        udf = unl.df.copy()
        kpi_row([
            {"label": "Unload runs", "value": f"{int(udf['UNLOADS'].sum())}"},
            {"label": "GB written out", "value": f"{float(udf['GB_OUT'].sum()):,.1f}"},
            {"label": "Users unloading", "value": f"{udf['USER_NAME'].nunique()}"},
        ])
        styled_table(udf, height=320)
        st.caption("Every name here should have a business reason to move data out. New names are the finding.")
        result_caption(unl)


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


def _governance_score_panel():
    """Governance debt as a number with named deductions (CoCo item 14a)."""
    # Posture-snapshot-first (live round 6: gov_counts topped the fleet
    # slow-fetch board — 13 hits, p95 12.3s). The daily 06:30 posture mart
    # carries all four score inputs since V030; the 4-subquery live scan
    # stays as the fallback and the pre-V030 path. Hygiene counts a day old
    # are fine — the source label says which path served.
    inputs: dict = {}
    # One 90-day read serves BOTH this score (latest day) and the trend
    # panel below (r14 #18) — the 3d + 90d double-read collapsed.
    post = run(mart27_sql.security_posture(90), page=_PAGE, key="gov_posture", tier="recent",
               source="MART_SECURITY_POSTURE_DAILY (daily 06:30 snapshot, 90d shared)")
    if post.usable():
        pdf_ = post.df.copy()
        snap = pdf_[pdf_["DAY"] == pdf_["DAY"].max()].set_index("METRIC")["VALUE"]
        if {"MFA_GAP_USERS", "EXPIRED_CRED", "BREAKGLASS_GRANTS_30D"} <= set(snap.index.astype(str)):
            inputs = {
                "mfa_gap_users": snap.get("MFA_GAP_USERS"),
                "expired_credentials": snap.get("EXPIRED_CRED"),
                "expiring_credentials": snap.get("EXPIRING_CRED_10D", 0),
                "breakglass_grants_30d": snap.get("BREAKGLASS_GRANTS_30D"),
            }
            if "WH_NO_AUTOSUSPEND" in set(snap.index.astype(str)):
                # V041 R11 posture row; the WH_NO_MONITOR twin is ignored
                # since v4.45 (owner runs no resource monitors).
                inputs["warehouses_no_autosuspend"] = int(float(snap.get("WH_NO_AUTOSUSPEND") or 0))
    whs = None
    if "warehouses_no_autosuspend" not in inputs:
        whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="gov_show_wh",
                  tier="metadata", source="SHOW WAREHOUSES (pre-V041 fallback)", max_rows=0)
    if not inputs:
        counts = run(security_sql.governance_counts(), page=_PAGE, key="gov_counts",
                     tier="historical", source="USERS + CREDENTIALS + GRANTS_TO_USERS (live fallback)")
        if counts.usable():
            row = counts.df.iloc[0]
            inputs = {
                "mfa_gap_users": row.get("MFA_GAP_USERS"),
                "expired_credentials": row.get("EXPIRED_CREDENTIALS"),
                "expiring_credentials": row.get("EXPIRING_CREDENTIALS"),
                "breakglass_grants_30d": row.get("BREAKGLASS_GRANTS_30D"),
            }
    if whs is not None and whs.ok and not whs.empty:
        wdf = whs.df.copy()
        wdf.columns = [str(c).lower() for c in wdf.columns]
        if "auto_suspend" in wdf.columns:
            asus = pd.to_numeric(wdf["auto_suspend"], errors="coerce").fillna(0)
            inputs["warehouses_no_autosuspend"] = int((asus <= 0).sum())
    if not inputs:
        return post
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
    return post


def _export_pack(company: str, days: int) -> None:
    """One-click access-review bundle: CSVs zipped in memory, stdlib only."""
    st.markdown("**Auditor export pack**")
    st.caption("Dormant users, MFA gaps, break-glass holders, and window grants as a timestamped zip of CSVs.")
    if not st.button("Build access-review pack", key="sec_pack_build"):
        return
    from app.ui.components import log_ui_event
    log_ui_event("csv_export", page="Security")
    import io
    import zipfile
    from datetime import datetime

    sheets = {
        "dormant_users": insights_sql.dormant_users(90, company),
        "mfa_gaps_password_login": security_sql.users_without_mfa(company),
        "break_glass_holders": security_sql.admin_role_holders(),
        "role_grants_window": security_sql.recent_role_grants(days),
        "failed_logins_window": security_sql.failed_logins(days, company),
        "expiring_credentials_10d": security_sql.expiring_credentials(10, company),
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
        key="sec_pack_dl", on_click="ignore",
    )
    st.caption(f"{sum(rows_written.values()):,} rows across {len(sheets)} files.")


def _change_kind(qt: object) -> str:
    """Collapse QUERY_TYPE into four readable change families."""
    s = str(qt or "").upper()
    if s.startswith("CREATE"):
        return "Create"
    if s.startswith("ALTER"):
        return "Alter"
    if s in ("DROP", "TRUNCATE_TABLE", "RENAME_TABLE", "RENAME"):
        return "Drop / truncate"
    if s in ("GRANT", "REVOKE"):
        return "Grants"
    return "Other"


def _posture_trend_panel(trend) -> None:
    """Posture as direction, not just today (Codex r6 #15) — shares the
    header's single 90-day posture read (r14 #18); renders nothing until
    the daily loader has 2+ days of history."""
    if trend is None or not trend.usable():
        return
    pdf = trend.df.copy()
    if pdf["DAY"].nunique() < 2:
        st.caption("Posture trend unlocks after the daily loader has 2+ days of history.")
        return
    with st.expander("Posture trend (90d) — is hygiene getting better or worse?"):
        metrics = sorted(pdf["METRIC"].astype(str).unique())
        default_ix = metrics.index("EXPIRING_CRED_10D") if "EXPIRING_CRED_10D" in metrics else 0
        metric = st.selectbox("Metric", metrics, index=default_ix, key="posture_metric")
        one = pdf[pdf["METRIC"].astype(str) == metric][["DAY", "VALUE"]].sort_values("DAY")
        charts.daily_metric_line(one, "DAY", "VALUE", title=metric)
        latest = float(one["VALUE"].iloc[-1]) if len(one) else 0.0
        first = float(one["VALUE"].iloc[0]) if len(one) else 0.0
        arrow = "flat" if latest == first else ("down " + f"{first - latest:,.0f}" if latest < first
                                                else "up " + f"{latest - first:,.0f}")
        st.caption(f"{metric}: {first:,.0f} -> {latest:,.0f} over the window ({arrow}). "
                   "Loaded daily at 06:30.")


def _clients_tab(company: str, days: int) -> None:
    """Driver/version inventory — the 'when do we need to upgrade' sheet."""
    st.markdown("**Client drivers & versions — who connects with what**")
    panel_help(
        "Source: ACCOUNT_USAGE.SESSIONS (lags up to ~3h, 365d retention). DRIVER and "
        "VERSION parse from CLIENT_APPLICATION_ID; PROGRAM is whatever the client "
        "self-reports (VS Code, DBeaver and most JDBC/Python tools do; many ODBC "
        "tools such as Erwin do not — '(not reported)' means exactly that). "
        "BEHIND = an older version than the newest of the same driver seen in this "
        "account this window: those rows are the upgrade list."
    )
    res = run(security_sql.client_drivers(days, company), page=_PAGE,
              key=f"clients_{company}_{days}", tier="historical",
              source="ACCOUNT_USAGE.SESSIONS")
    if res.ok and res.empty:
        st.info("No sessions recorded in this window for this scope.")
        return
    if not guard(res, "", setup_hint="Needs the ACCOUNT_USAGE.SESSIONS view (IMPORTED PRIVILEGES on the SNOWFLAKE db)."):
        return
    df = res.df.copy()
    behind = int((df["STATUS"].astype(str) == "BEHIND").sum())
    kpi_row([
        {"label": "Driver families", "value": f"{df['DRIVER'].nunique()}"},
        {"label": "Driver+version combos", "value": f"{len(df)}"},
        {"label": "Versions behind newest", "value": f"{behind}",
         "delta_color": "inverse" if behind else "off",
         "help": "Older than the newest version of the SAME driver seen here — "
                 "Snowflake's support policy drops drivers older than ~2 years, "
                 "so stale LAST_SEEN + BEHIND is the upgrade shortlist."},
    ])
    styled_table(df, height=380)
    st.download_button("Download driver inventory (CSV)", data=df.to_csv(index=False),
                       file_name=f"overwatch_client_drivers_{company}_{days}d.csv",
                       mime="text/csv", key="sec_drivers_csv", on_click="ignore")
    result_caption(res)


def _changes_tab(company: str, days: int, database: str = "", schema_contains: str = "") -> None:
    # r23 #4 (the Access-tab pattern): the tab's two independent live reads
    # submit server-side async in one shot; any failure falls back to the
    # serial per-query calls below, unchanged.
    _cb = run_batch([
        {"key": "ddl", "sql": security_sql.recent_ddl_changes(days, company, database, schema_contains),
         "source": "QUERY_HISTORY (DDL/DCL types)"},
        {"key": "login_reasons", "sql": security_sql.failed_login_reasons(days, company),
         "source": "LOGIN_HISTORY (failure reasons)"},
    ], page=_PAGE, tier="recent")

    st.markdown("**Who changed what (DDL/DCL)**")
    res = _cb.get("ddl") or run(security_sql.recent_ddl_changes(days, company, database, schema_contains), page=_PAGE,
              key=f"ddl_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (DDL/DCL types)")
    if res.ok and res.empty:
        st.success("No DDL/DCL changes recorded in this window for this scope.")
        return
    if guard(res, ""):
        # Redesign 2026-07-09: the flat total/day bar answered neither of the
        # questions people ask ("what kind of change?", "who?"). Stack by
        # change kind; put the who right beside it.
        ddl_df = res.df.copy()
        ddl_df["CHANGE_KIND"] = ddl_df["QUERY_TYPE"].map(_change_kind)
        left, right = st.columns((3, 2))
        with left:
            daily = ddl_df.groupby(["DAY", "CHANGE_KIND"], as_index=False)["STATEMENTS"].sum()
            charts.daily_stacked_count(daily.sort_values("DAY"), "DAY", "CHANGE_KIND",
                                       "STATEMENTS", title="Change statements/day")
        with right:
            by_user = (ddl_df.groupby("USER_NAME", as_index=False)["STATEMENTS"].sum()
                       .sort_values("STATEMENTS", ascending=False))
            charts.bar_count(by_user, "USER_NAME", "STATEMENTS",
                             title="Statements by user", top_n=8)
        st.caption("Left: what kind of change landed each day (create / alter / "
                   "drop / grants). Right: who made them. Rows below have the objects.")
        styled_table(res.df)
        result_caption(res)

    st.markdown("**Failed-login reasons (network policy vs credentials)**")
    reasons = _cb.get("login_reasons") or run(security_sql.failed_login_reasons(days, company), page=_PAGE,
                  key=f"login_reasons_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.LOGIN_HISTORY")
    if reasons.ok and reasons.empty:
        st.success("No failed logins in the window.")
    elif guard(reasons, ""):
        styled_table(reasons.df)
        result_caption(reasons)

    st.markdown("**Unused roles (90d) — revoke candidates**")
    ur = run_mart_first(
        mart27_sql.unused_roles_via_fact(90), security_sql.unused_roles(90),
        page=_PAGE, key="unused_roles",
        mart_source="ROLES x FACT_QUERY_ROLE_HOURLY (mart — active once 90d coverage exists)",
        live_source="ROLES x QUERY_HISTORY (90d, live fallback)")
    if ur.ok and ur.empty:
        st.success("Every active role was assumed in the last 90 days.")
    elif guard(ur, ""):
        styled_table(ur.df)
        st.caption("Also in the quarterly export pack with the full grant matrix and 90d diff.")
        result_caption(ur)

    st.markdown("**Break-glass role activity (should hug zero)**")
    bga = run(security_sql.admin_role_activity(days), page=_PAGE,
              key=f"breakglass_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY (admin roles)")
    if bga.ok and bga.empty:
        st.success("No statements ran under ACCOUNTADMIN / SNOW_ACCOUNTADMINS in the window.")
    elif guard(bga, ""):
        styled_table(bga.df)
        st.caption("Evidence only — no alert fires on admin-role use. Routine work "
                   "still belongs on SNOW_SYSADMINS.")
        result_caption(bga)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Security & Governance", "Hygiene and governance posture — not a threat-detection SOC.", icon_name="security",
                scope_note=f"{f['company']} · last {f['days']} days")
    st.caption(
        "Access control is Snowflake RBAC — this page reports posture; it does not grant or "
        "revoke anything. Company scoping is a shared-account view filter, not isolation."
    )
    _post90 = _governance_score_panel()
    _posture_trend_panel(_post90)
    section = lazy_sections(["Access", "Changes", "Clients", "Egress", "Trust Center"], key="sec_section")
    if section == "Access":
        _access_tab(f["company"], f["days"])
        st.divider()
        _export_pack(f["company"], f["days"])
    elif section == "Changes":
        _changes_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    elif section == "Clients":
        _clients_tab(f["company"], f["days"])
    elif section == "Egress":
        _egress_tab(f["company"], f["days"])
    else:
        _trust_center_tab()
