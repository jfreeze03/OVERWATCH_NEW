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
    domain_posture,
    escalation_flags,
    grant_anomaly_flags,
)
from app.logic.workbench import create_action_sql
from app.ui.components import (
    empty_state,
    kpi_row,
    notify,
    panel_help,
    result_caption,
    section_header,
    selectable_table,
    styled_table,
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


def render_security_overview(company: str) -> None:
    """Exceptions-first domain posture with Action Center and Entity 360 drills."""
    section_header("Security decision queue", "warn", "security")
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
        _setup_state("The security decision queue")
        return

    posture = domain_posture(
        queue.df if queue.ok else pd.DataFrame(),
        coverage.df if coverage.ok else pd.DataFrame(),
    )
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

    if not queue.ok:
        empty_state("no_data_yet", "The domain contract loaded, but the exception queue did not resolve.")
        return
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
        if action_id and st.button("Open tracked action", key="sec_exception_action", use_container_width=True):
            request_navigation(
                "Control Room", "Action Center", context={"action_id": action_id}
            )
    with c2:
        entity_key = _cell_text(row.get("ENTITY_KEY"))
        if entity_key and st.button("Open entity", key="sec_exception_entity", use_container_width=True):
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
            if st.button("Create work item", key="sec_exception_create"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)
                # rec48: non-idempotent INSERT into OVERWATCH's own queue with no other
                # rerun — rerun so the surface refreshes and the form/button reset, or a
                # second click double-inserts (the persistent success banner used to be
                # the only cue suppressing that).
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
    if selection is not None:
        try:
            chosen = str(summary.iloc[int(selection)]["USER_NAME"])
            st.session_state["sec_effective_user"] = chosen
        except (IndexError, TypeError, ValueError):
            pass
    users = summary["USER_NAME"].astype(str).tolist()
    if not users:
        return
    preferred = str(st.session_state.get("sec_effective_user") or users[0])
    index = users.index(preferred) if preferred in users else 0
    user = st.selectbox("Access path for", users, index=index, key="sec_effective_user_pick")
    one = frame[frame["USER_NAME"].astype(str) == user].head(80)
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
    st.graphviz_chart("\n".join(dot), use_container_width=True)
    if self_escalates:
        st.caption(
            "⚠ This user holds MANAGE GRANTS on an effective role — it can grant itself "
            "any role, including admin (a self-escalation surface)."
        )
    if st.button("Open user in Entity 360", key="sec_effective_entity"):
        request_navigation(
            "Control Room", "Entity 360", context={"entity_type": "USER", "entity_key": user}
        )
    styled_table(one, height=300, sort_label="risk then path")
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


