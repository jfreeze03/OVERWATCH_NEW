"""Security operating-model UI shared by the Security page.

The readers in this module are V075-aware and probe-gated. Until the owner
applies that migration, the established read-only Security panels continue to
work and these controls render one setup state instead of a wall of failures.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from app.core.query import execute_statement, run
from app.core.session import is_operator
from app.core.state import request_navigation
from app.data import security_sql, workbench_sql
from app.logic.formulas import account_today, safe_float
from app.logic.security import (
    POSTURE_METRICS,
    domain_posture,
    escalation_flags,
    grant_anomaly_flags,
    posture_alert_rule_sql,
)
from app.logic.verdict import Signal, page_verdict
from app.logic.workbench import create_action_sql
from app.ui.components import (
    alarm_health,
    empty_state,
    kpi_row,
    notify,
    page_verdict_line,
    panel_help,
    result_caption,
    section_header,
    selectable_table,
    stamp_write,
    stash_section_count,
    styled_table,
    write_gate_open,
)

_PAGE = "Security"


def _cell_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    try:
        if bool(pd.isna(value)):
            return default
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text if text else default


def _optional_result(sql: str, key: str, source: str, *, tier: str = "hourly"):
    return run(sql, page=_PAGE, key=key, tier=tier, source=source, probe=True)


def _setup_state(label: str) -> None:
    empty_state(
        "needs_setup",
        f"{label} becomes available after V075 is applied by the Snowflake owner.",
        hint="The existing read-only evidence below remains available meanwhile.",
    )


def _action_entity_type(value: object) -> str:
    kind = _cell_text(value).upper().replace(" ", "_")
    return {
        "USER": "USER",
        "ROLE": "ROLE",
        "QUERY": "QUERY_FINGERPRINT",
        "ACTION": "ACTION",
        "ALERT": "ALERT",
    }.get(kind, "OBJECT")


def _render_change_risk_diagnostic() -> None:
    """Diagnostic expander: the actors/objects behind the CHANGE RISK DESTRUCTIVE
    flood (owner 2026-08-17). Reads FACT_SECURITY_CHANGE directly so it's visible
    exactly which roles/databases drive the noise — and whether a TF_* service-role
    exclusion would clear it — before any change-risk exclusion is written."""
    with st.expander("Change-risk noise — what's actually driving the DESTRUCTIVE flood"):
        res = _optional_result(
            security_sql.change_risk_destructive_breakdown(7),
            "sec_change_risk_breakdown",
            "FACT_SECURITY_CHANGE (DESTRUCTIVE, risk>=70, 7d)",
        )
        if not res.ok:
            _setup_state("The change-risk breakdown")
            return
        if res.empty:
            empty_state("clean", "No DESTRUCTIVE change-risk events over threshold in the last 7 days.")
            return
        df = res.df.copy()
        # `EVENTS` sums only the top-200 groups (the SQL's LIMIT bounds the table
        # below). The headline count must be the TRUE population, so read the pre-LIMIT
        # window total; the per-class SHARES stay relative to the shown groups (their
        # denominator), disclosed when the two differ.
        _shown = int(df["EVENTS"].sum())
        total = (int(safe_float(df["TOTAL_EVENTS"].iloc[0])) if "TOTAL_EVENTS" in df.columns
                 else _shown)

        def _events_where(mask: pd.Series) -> int:
            return int(df.loc[mask, "EVENTS"].sum()) if _shown else 0

        def _pct(n: int) -> str:
            return f"{(100.0 * n / _shown):.0f}%" if _shown else "0%"

        tf = _events_where(df["ROLE_CLASS"] == "TF_* service")
        no_role = _events_where(df["ROLE_NAME"] == "(no role attributed)")
        app_db = _events_where(df["DATABASE_NAME"] == "DBA_MAINT_DB")
        kpi_row([
            {"label": "Destructive events (7d)", "value": f"{total:,}",
             "help": "DROP/TRUNCATE at RISK_SCORE>=70 — the rows the CHANGE RISK queue counts."},
            {"label": "By TF_* roles", "value": _pct(tf), "delta": f"{tf:,} events",
             "help": "Share attributable to Terraform service roles. If this is most of the "
                     "flood a TF_* exclusion clears it; if it's small, the drivers are elsewhere."},
            {"label": "No role attributed", "value": _pct(no_role), "delta_color": "off",
             "help": "Unattributed drops. These stay visible on purpose — a security queue must "
                     "not silently hide an actor-less destructive event."},
            {"label": "On app's own DB", "value": _pct(app_db), "delta": f"{app_db:,} events",
             "help": "Events on DBA_MAINT_DB (OVERWATCH's own database)."},
        ])
        st.caption(
            "Account-wide (regardless of the company filter) — this tunes the account-level "
            "TF_* / role exclusion, which is not per-company. Raw evidence behind the CHANGE RISK "
            "score: if a few named roles dominate, exclude those roles; if it's TF_*-dominated a "
            "pattern works; unattributed and human drops on real data should stay visible."
        )
        if total > _shown:
            st.caption(f"Showing the top 200 role/database/schema groups of {total:,} total "
                       f"destructive events; the shares above are of the {_shown:,} shown.")
        styled_table(df.drop(columns=["TOTAL_EVENTS"], errors="ignore"), height=300)
        result_caption(res)


def render_security_overview(company: str) -> None:
    """Exceptions-first domain posture with Action Center and Entity 360 drills."""
    queue = _optional_result(
        security_sql.security_exception_queue(company, 100),
        f"sec_exception_queue_{company}",
        "V_SECURITY_EXCEPTION_QUEUE",
    )
    coverage = _optional_result(
        security_sql.security_domain_coverage(),
        "sec_domain_coverage",
        "Security domain coverage contract",
    )
    if not queue.ok and not coverage.ok:
        section_header("Security decision queue", "", "security")
        _setup_state("The security decision queue")
        return
    if not queue.ok:
        # The exception queue IS the per-domain evidence. If it did not resolve, scoring
        # off an empty frame would read every COMPLETE-coverage domain as 100/Healthy —
        # a false all-clear painted above this notice. Surface the unresolved state and
        # stop, before any posture/verdict/KPI is rendered.
        section_header("Security decision queue", "", "security")
        empty_state("no_data_yet", "The domain contract loaded, but the exception queue did not resolve.")
        return

    posture = domain_posture(
        queue.df,
        coverage.df if coverage.ok else pd.DataFrame(),
    )
    # C17: one "should I worry?" line from the posture the queue already computed,
    # and (C23) the section header's severity derives from the same data.
    _act = [p for p in posture if p.state == "Act"]
    _open_n = sum(int(safe_float(getattr(p, "findings", 0))) for p in posture)
    page_verdict_line(page_verdict([
        Signal("bad", f"{len(_act)} domain(s) need action: "
                      + ", ".join(p.domain.title() for p in _act[:3])) if _act else None,
        Signal("warn", f"{_open_n} open finding(s)") if _open_n else None,
    ], healthy="no security domain needs action"))
    section_header("Security decision queue",
                   alarm_health(len(_act) or _open_n), "security")
    kpi_row([
        {
            "label": item.domain.title(),
            "value": str(item.score) if item.score is not None else "--",
            "delta": f"{item.state} | {item.findings} open",
            "delta_color": "inverse" if item.state == "Act" else "off",
            "help": (
                f"Coverage: {item.coverage}. A numeric score is shown only when "
                "the domain's evidence source reports complete coverage."
            ),
        }
        for item in posture
    ])
    st.caption(
        "Domain scores are evidence-qualified. Each exception deducts severity points; "
        "its affected-entity count increases that deduction up to 3x. Unknown and "
        "on-demand domains do not silently receive a perfect score."
    )

    # Owner diagnostic (2026-08-17): when CHANGE RISK is flooded by routine DESTRUCTIVE
    # DDL, surface WHO/WHAT actually drives it so any exclusion is precise, not a guess.
    _render_change_risk_diagnostic()

    # (queue.ok is guaranteed here — the unresolved-queue case is handled above, before any
    # posture/verdict/KPI render, so a read failure can never paint a green all-clear.)
    # C16: park the open-exception count for the section bar's "Decision queue (n)"
    # badge — stashed only once the queue actually resolved, so 0 means clean, not unknown.
    # review fix: the queue SQL is company-filtered but window-independent —
    # keying by days would clear the badge on a window flip for no reason.
    stash_section_count(_PAGE, "Decision queue", len(queue.df), dims=("company",))
    if queue.empty:
        unresolved = [
            item.domain for item in posture
            if item.coverage not in ("COMPLETE", "ON_DEMAND")
        ]
        if unresolved:
            empty_state(
                "no_data_yet",
                "No exceptions are queued from the evidence that resolved, but coverage is "
                "not complete for: " + ", ".join(unresolved) + ".",
            )
        else:
            empty_state("clean", "No open security exceptions in the materialized queue.")
        result_caption(queue)
        return

    frame = queue.df.copy().reset_index(drop=True)

    def _scope_label(row: pd.Series) -> str:
        actor = _cell_text(row.get("ACTOR_COMPANY"))
        obj = _cell_text(row.get("OBJECT_COMPANY"))
        if actor and obj and actor.upper() != obj.upper():
            return f"{actor} to {obj}"
        return obj or actor or _cell_text(row.get("COMPANY"), "ALL")

    frame["SCOPE"] = frame.apply(_scope_label, axis=1)
    visible = [
        col for col in (
            "SEVERITY", "DOMAIN", "TITLE", "IMPACT_COUNT", "STATUS",
            "OWNER", "SCOPE", "DETECTED_AT",
        ) if col in frame
    ]
    selection = selectable_table(
        frame[visible],
        key="sec_exception_pick",
        height=330,
        days=None,
        sort_label="severity then detection time",
    )
    result_caption(queue)
    if selection is None:
        st.caption("Select an exception to open its evidence, entity, or tracked work item.")
        return
    try:
        row = frame.iloc[int(selection)]
    except (IndexError, TypeError, ValueError):
        return

    st.markdown(f"**{_cell_text(row.get('TITLE'), 'Security exception')}**")
    st.caption(_cell_text(row.get("DETAIL"), "No additional evidence was recorded."))
    c1, c2 = st.columns(2)
    action_id = _cell_text(row.get("ACTION_ID"))
    with c1:
        if action_id and st.button("Open tracked action", key="sec_exception_action", width="stretch"):
            request_navigation(
                "Control Room", "Action Center", context={"action_id": action_id}
            )
    with c2:
        entity_key = _cell_text(row.get("ENTITY_KEY"))
        if entity_key and st.button("Open entity", key="sec_exception_entity", width="stretch"):
            request_navigation(
                "Control Room",
                "Entity 360",
                context={
                    "entity_type": _action_entity_type(row.get("ENTITY_TYPE")),
                    "entity_key": entity_key,
                },
            )

    if not action_id and is_operator() and entity_key:
        with st.expander("Track as work item"):
            owner = st.text_input("Owner", value="DBA", key="sec_exception_owner", max_chars=200)
            due = st.date_input(
                "Due", value=account_today() + timedelta(days=7), key="sec_exception_due"
            )
            statement = create_action_sql(
                title=_cell_text(row.get("TITLE"), "Security exception"),
                detail=_cell_text(row.get("DETAIL")),
                company=_cell_text(row.get("COMPANY"), company),
                severity=_cell_text(row.get("SEVERITY"), "MEDIUM"),
                owner=owner,
                due_date=due,
                source="Security decision queue",
                entity_type=_action_entity_type(row.get("ENTITY_TYPE")),
                entity_key=entity_key,
                confidence=safe_float(row.get("CONFIDENCE")),
            )
            # rec19: warn (don't block) if this entity already has an open work item —
            # this path and Action Center both write to ACTION_QUEUE with no dedupe.
            _dupes = run(
                workbench_sql.related_actions(_action_entity_type(row.get("ENTITY_TYPE")), entity_key),
                page=_PAGE, key=f"sec_dupe_{entity_key.strip().upper()}", tier="recent",
                source="ACTION_QUEUE")
            if _dupes.usable():
                _open_dupes = _dupes.df[_dupes.df["STATUS"].astype(str).str.upper()
                                        .isin(("OPEN", "IN_PROGRESS"))]
                if not _open_dupes.empty:
                    st.warning(f"{len(_open_dupes)} open work item(s) already track this entity — "
                               "open one from Action Center instead of duplicating, or continue if "
                               "this is genuinely separate.")
            st.code(statement, language="sql")
            if (st.button("Create work item", key="sec_exception_create")
                    and write_gate_open("sec_exception_create")):
                ok, message = execute_statement(statement, page=_PAGE)
                stamp_write("sec_exception_create", ok)  # C48
                notify(ok, message)
                # rec48: non-idempotent INSERT into OVERWATCH's own queue with no other
                # rerun — rerun so the surface refreshes and the form/button reset, or a
                # second click double-inserts (the persistent success banner used to be
                # the only cue suppressing that).
                if ok:
                    st.rerun()

    if is_operator():
        with st.expander("Monitor as a posture rule (Sec35)"):
            st.caption(
                "Turn this finding into a self-monitoring rule: pick the posture metric to "
                "watch and a threshold, and generate an ALERT_CONFIG rule. The hourly scan "
                "then re-alerts whenever that metric is at or over the threshold — so posture "
                "degrading again pages, instead of relying on a one-off review. Upsert: "
                "re-running for the same metric updates its threshold/severity."
            )
            _pm = st.selectbox("Posture metric", POSTURE_METRICS, key="sec_posture_metric")
            _pt = st.number_input("Alert when the count is at or over", min_value=1, value=1,
                                  step=1, key="sec_posture_threshold")
            _psev = st.selectbox("Severity", ("HIGH", "CRITICAL", "MEDIUM", "LOW"),
                                 index=0, key="sec_posture_sev")
            try:
                _prule = posture_alert_rule_sql(_pm, float(_pt), severity=_psev)
            except ValueError as exc:
                st.warning(str(exc))
            else:
                st.code(_prule, language="sql")
                if (is_operator() and st.button("Create posture monitor rule",
                                                key="sec_posture_create")
                        and write_gate_open("sec_posture_create")):
                    ok, message = execute_statement(_prule, page=_PAGE)
                    stamp_write("sec_posture_create", ok)  # C48
                    notify(ok, message)
                    if ok:
                        st.rerun()


def _dot_text(value: object) -> str:
    return _cell_text(value).replace("\\", "\\\\").replace('"', '\\"')


def render_effective_access(company: str) -> None:
    """On-demand inherited-role graph and risk-ranked access paths."""
    section_header("Effective access paths", "info", "security", anchor="sec-effective")
    panel_help(
        "Direct grants are only the first hop. This recursive view expands inherited roles, "
        "counts sensitive privileges, and exposes the exact path used to reach them."
    )
    if not st.toggle("Load effective-access graph", key="sec_effective_access_on"):
        return
    result = run(
        security_sql.effective_access(company),
        page=_PAGE,
        key=f"sec_effective_{company}",
        tier="metadata",
        source="ACCOUNT_USAGE grants (recursive, on demand)",
    )
    if result.ok and result.empty:
        empty_state("clean", "No effective role paths are visible for this company scope.")
        return
    if not result.usable():
        empty_state("no_data_yet", "Effective-access evidence did not resolve for this scope.")
        return
    frame = escalation_flags(result.df.copy().reset_index(drop=True))
    summary = (
        frame.groupby("USER_NAME", as_index=False)
        .agg(
            DIRECT_ROLES=("DIRECT_ROLE", "nunique"),
            EFFECTIVE_ROLES=("EFFECTIVE_ROLE", "nunique"),
            MAX_RISK=("RISK_SCORE", "max"),
            MAX_ESCALATION=("ESCALATION_SCORE", "max"),
            SELF_ESCALATE=("SELF_ESCALATION", "any"),
            SENSITIVE_PRIVILEGES=("SENSITIVE_PRIVILEGES", "sum"),
        )
        .sort_values(["MAX_ESCALATION", "MAX_RISK", "EFFECTIVE_ROLES"], ascending=False)
        .reset_index(drop=True)
    )
    self_escalators = int(summary["SELF_ESCALATE"].sum())
    kpi_row([
        {"label": "Users", "value": f"{len(summary):,}"},
        {"label": "Effective paths", "value": f"{len(frame):,}"},
        {"label": "High-risk users", "value": f"{int((summary['MAX_RISK'] >= 70).sum()):,}", "delta_color": "inverse"},
        {
            "label": "Can self-escalate to admin",
            "value": f"{self_escalators:,}",
            "help": "Holds MANAGE GRANTS on some effective role — the privilege that "
            "can grant any role (including admin) to anyone, including itself.",
            "delta_color": "inverse" if self_escalators else "off",
        },
    ])
    selection = selectable_table(
        summary, key="sec_effective_users", height=260, sort_label="escalation risk"
    )
    users = summary["USER_NAME"].astype(str).tolist()
    if not users:
        return
    if selection is not None:
        try:
            chosen = str(summary.iloc[int(selection)]["USER_NAME"])
            # Drive the selectbox by ITS OWN key: a keyed widget reads session_state[key]
            # and ignores index= after its first render, so writing the widget key is the
            # only way a table-row click can move the selectbox (and the graph/detail it
            # feeds). Writing a separate key left the row-click a dead affordance.
            if chosen in users:
                st.session_state["sec_effective_user_pick"] = chosen
        except (IndexError, TypeError, ValueError):
            pass
    # Seed the box on the highest-risk user before it first renders, and reset a stale
    # selection when the scope changes and the prior user drops out of the list.
    if st.session_state.get("sec_effective_user_pick") not in users:
        st.session_state["sec_effective_user_pick"] = users[0]
    user = st.selectbox("Access path for", users, key="sec_effective_user_pick")
    _uslice = frame[frame["USER_NAME"].astype(str) == user]
    # `frame` arrives RISK_SCORE-DESC. A MANAGE-GRANTS-only path (a self-escalation
    # surface, but low RISK_SCORE ~25) would otherwise sort to the bottom and fall
    # outside head(80) for a >80-path super-admin — the graph would then omit the red
    # admin node and the caption while the summary flags the user as self-escalating.
    # Float the escalation-critical paths in first (stable → RISK_SCORE order preserved
    # within each group) so head(80) can never drop the evidence for the flag it shows.
    _esc_cols = [c for c in ("SELF_ESCALATION", "REACHES_ADMIN") if c in _uslice.columns]
    if _esc_cols:
        _uslice = _uslice.sort_values(_esc_cols, ascending=False, kind="stable")
    one = _uslice.head(80)
    dot = ["digraph access {", "rankdir=LR;", 'node [shape=box, style="rounded"];']
    dot.append(f'"{_dot_text(user)}" [shape=ellipse];')
    admin_nodes: set[str] = set()
    self_escalates = False
    for _, row in one.iterrows():
        direct = _dot_text(row.get("DIRECT_ROLE"))
        effective = _dot_text(row.get("EFFECTIVE_ROLE"))
        dot.append(f'"{_dot_text(user)}" -> "{direct}";')
        if direct != effective:
            dot.append(f'"{direct}" -> "{effective}";')
        if bool(row.get("REACHES_ADMIN")):
            admin_nodes.add(effective)
        if bool(row.get("SELF_ESCALATION")):
            self_escalates = True
    # Admin-reaching effective roles are the escalation surface — mark them.
    dot.extend(
        f'"{node}" [style="rounded,filled", fillcolor="#f8d7da", color="#c0392b"];'
        for node in sorted(admin_nodes)
    )
    if self_escalates:
        dot.append(f'"{_dot_text(user)}" [shape=ellipse, color="#c0392b", penwidth=2];')
    dot.append("}")
    st.graphviz_chart("\n".join(dot), width="stretch")
    if self_escalates:
        st.caption(
            "⚠ This user holds MANAGE GRANTS on an effective role — it can grant itself "
            "any role, including admin (a self-escalation surface)."
        )
    if st.button("Open user in Entity 360", key="sec_effective_entity"):
        request_navigation(
            "Control Room", "Entity 360", context={"entity_type": "USER", "entity_key": user}
        )
    styled_table(one, height=300, sort_label="escalation, then risk")
    result_caption(result)


def render_admin_grant_anomalies(company: str) -> None:
    """Sec2 time-context: admin-role grants flagged by when + to whom they landed."""
    section_header("Admin grants — timing check", "warn", "admin", anchor="sec-admin-grants")
    panel_help(
        "A count of admin grants is volume; the *context* is what matters. This flags a "
        "grant of an admin role to a user who has none on record (a first-ever elevation) "
        "and grants that landed on a weekend or outside business hours — outside a normal "
        "deploy window. Evidence only; no alert fires from here."
    )
    if not st.toggle("Load admin-grant timing check", key="sec_admin_grant_ctx_on"):
        return
    result = run(
        security_sql.admin_grant_context(90, company),
        page=_PAGE,
        key=f"sec_admin_grant_ctx_{company}",
        tier="metadata",
        source="admin-role grants + prior-tenure history (on demand)",
    )
    if result.ok and result.empty:
        empty_state("clean", "No admin-role grants landed in the last 90 days for this scope.")
        return
    if not result.usable():
        empty_state("no_data_yet", "Admin-grant evidence did not resolve for this scope.")
        return
    frame = grant_anomaly_flags(result.df.copy().reset_index(drop=True))
    first_time = int(frame["FIRST_TIME"].sum())
    off_hours = int(frame["OFF_HOURS"].sum())
    kpi_row([
        {"label": "Admin grants (90d)", "value": f"{len(frame):,}"},
        {
            "label": "First-ever elevations",
            "value": f"{first_time:,}",
            "help": "Grantee had no prior grant of this role on record.",
            "delta_color": "inverse" if first_time else "off",
        },
        {
            "label": "Off-hours / weekend",
            "value": f"{off_hours:,}",
            "help": "Landed outside business hours (account-local) or on a weekend.",
            "delta_color": "inverse" if off_hours else "off",
        },
    ])
    display = frame.sort_values(
        ["ANOMALY", "FIRST_TIME", "GRANTED_AT"], ascending=[False, False, False]
    ).reset_index(drop=True)
    display = display[[
        "SEVERITY", "ADMIN_ROLE", "USER_NAME", "GRANTED_AT", "GRANTED_BY", "REASON",
    ]]
    styled_table(display, height=300, sort_label="anomaly then most recent")
    result_caption(result)


