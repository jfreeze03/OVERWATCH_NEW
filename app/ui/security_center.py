"""Security operating-model UI shared by the Security page.

The readers in this module are V075-aware and probe-gated. Until the owner
applies that migration, the established read-only Security panels continue to
work and these controls render one setup state instead of a wall of failures.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from app.core.identity import viewer_name
from app.core.query import execute_statement, run
from app.core.session import is_operator
from app.core.state import request_navigation
from app.data import security_sql
from app.logic.formulas import account_today, safe_float
from app.logic.security import (
    ACCESS_DECISIONS,
    EGRESS_TARGET_KINDS,
    IDENTITY_TYPES,
    access_review_decision_sql,
    create_access_review_sql,
    domain_posture,
    insert_egress_policy_sql,
    upsert_client_policy_sql,
    upsert_identity_policy_sql,
)
from app.logic.workbench import create_action_sql
from app.ui.components import (
    confirm_gate,
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
            st.code(statement, language="sql")
            if st.button("Create work item", key="sec_exception_create"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)


def render_identity_policy_manager(policies, company: str) -> pd.DataFrame:
    """Identity taxonomy, ownership, auth expectations, and policy editor."""
    section_header("Identity ownership policy", "info", "security")
    frame = policies.df.copy() if policies.ok else pd.DataFrame()
    if not policies.ok:
        _setup_state("Identity policy management")
        return frame

    classified = int(frame["IDENTITY_TYPE"].notna().sum()) if "IDENTITY_TYPE" in frame else 0
    kpi_row([
        {"label": "Classified identities", "value": f"{classified:,}"},
        {"label": "Service identities", "value": f"{int((frame.get('IDENTITY_TYPE', pd.Series(dtype=str)).astype(str) == 'SERVICE').sum()):,}"},
        {"label": "Emergency identities", "value": f"{int((frame.get('IDENTITY_TYPE', pd.Series(dtype=str)).astype(str) == 'EMERGENCY').sum()):,}"},
    ])
    if not frame.empty:
        styled_table(frame, height=280, sort_label="user")
    else:
        empty_state("no_data_yet", "No identity policies have been recorded yet.")

    if not is_operator():
        return frame
    with st.expander("Add or update identity policy"):
        user = st.text_input("User name", key="sec_identity_user", max_chars=200)
        c1, c2, c3 = st.columns(3)
        with c1:
            identity_type = st.selectbox("Identity type", IDENTITY_TYPES, key="sec_identity_type")
        with c2:
            owner = st.text_input("Owner", key="sec_identity_owner", max_chars=200)
        with c3:
            rotation = st.number_input(
                "Rotation days", min_value=1, max_value=3650, value=90, key="sec_identity_rotation"
            )
        auth = st.text_input(
            "Expected authentication", value="SSO_OR_KEYPAIR", key="sec_identity_auth", max_chars=80
        )
        network = st.text_input("Expected network", key="sec_identity_network", max_chars=500)
        exception_on = st.toggle("Time-bound exception", key="sec_identity_exception_on")
        exception_until = st.date_input(
            "Exception through",
            value=account_today() + timedelta(days=30),
            key="sec_identity_exception_until",
            disabled=not exception_on,
        )
        notes = st.text_area("Notes", key="sec_identity_notes", max_chars=4000)
        if user:
            statement = upsert_identity_policy_sql(
                user,
                identity_type=identity_type,
                owner=owner,
                auth_method=auth,
                network=network,
                rotation_days=int(rotation),
                exception_until=exception_until if exception_on else None,
                notes=notes,
                actor=viewer_name(),
            )
            st.code(statement, language="sql")
            if st.button("Save identity policy", key="sec_identity_save"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)
    return frame


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
    frame = result.df.copy().reset_index(drop=True)
    summary = (
        frame.groupby("USER_NAME", as_index=False)
        .agg(
            DIRECT_ROLES=("DIRECT_ROLE", "nunique"),
            EFFECTIVE_ROLES=("EFFECTIVE_ROLE", "nunique"),
            MAX_RISK=("RISK_SCORE", "max"),
            SENSITIVE_PRIVILEGES=("SENSITIVE_PRIVILEGES", "sum"),
        )
        .sort_values(["MAX_RISK", "EFFECTIVE_ROLES"], ascending=False)
        .reset_index(drop=True)
    )
    kpi_row([
        {"label": "Users", "value": f"{len(summary):,}"},
        {"label": "Effective paths", "value": f"{len(frame):,}"},
        {"label": "High-risk users", "value": f"{int((summary['MAX_RISK'] >= 70).sum()):,}", "delta_color": "inverse"},
    ])
    selection = selectable_table(
        summary, key="sec_effective_users", height=260, sort_label="maximum privilege risk"
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
    for _, row in one.iterrows():
        direct = _dot_text(row.get("DIRECT_ROLE"))
        effective = _dot_text(row.get("EFFECTIVE_ROLE"))
        dot.append(f'"{_dot_text(user)}" -> "{direct}";')
        if direct != effective:
            dot.append(f'"{direct}" -> "{effective}";')
    dot.append("}")
    st.graphviz_chart("\n".join(dot), use_container_width=True)
    if st.button("Open user in Entity 360", key="sec_effective_entity"):
        request_navigation(
            "Control Room", "Entity 360", context={"entity_type": "USER", "entity_key": user}
        )
    styled_table(one, height=300, sort_label="risk then path")
    result_caption(result)


def render_client_policy_editor(policies, observed: pd.DataFrame) -> None:
    if not policies.ok:
        _setup_state("Client support policies")
        return
    policy_frame = policies.df.copy()
    with st.expander("Client support policy"):
        if not policy_frame.empty:
            styled_table(policy_frame, height=220, sort_label="driver")
        else:
            empty_state("no_data_yet", "No minimum supported client versions are configured.")
        if not is_operator():
            return
        drivers = sorted(observed.get("DRIVER", pd.Series(dtype=str)).dropna().astype(str).unique())
        driver = st.selectbox("Driver", drivers, key="sec_client_policy_driver") if drivers else st.text_input(
            "Driver", key="sec_client_policy_driver_text", max_chars=200
        )
        minimum = st.text_input("Minimum approved version", key="sec_client_policy_min", max_chars=80)
        warn_on = st.toggle("Set support review date", key="sec_client_warn_on")
        warn_after = st.date_input(
            "Review after", value=account_today() + timedelta(days=90),
            key="sec_client_warn_after", disabled=not warn_on,
        )
        owner = st.text_input("Owner", key="sec_client_policy_owner", max_chars=200)
        notes = st.text_area("Notes", key="sec_client_policy_notes", max_chars=4000)
        if driver and minimum:
            statement = upsert_client_policy_sql(
                driver,
                minimum_version=minimum,
                warn_after=warn_after if warn_on else None,
                owner=owner,
                notes=notes,
                actor=viewer_name(),
            )
            st.code(statement, language="sql")
            if st.button("Save client policy", key="sec_client_policy_save"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)


def render_egress_policy_editor(policies, company: str) -> None:
    if not policies.ok:
        _setup_state("Data-movement policies")
        return
    with st.expander("Approved destinations"):
        if not policies.empty:
            styled_table(policies.df, height=220, sort_label="target kind and pattern")
        else:
            empty_state("no_data_yet", "No approved destination patterns are configured.")
        if not is_operator():
            return
        kind = st.selectbox("Destination kind", EGRESS_TARGET_KINDS, key="sec_egress_kind")
        pattern = st.text_input(
            "Destination pattern", key="sec_egress_pattern", max_chars=500,
            help="SQL LIKE pattern for an explicitly approved region or stage destination.",
        )
        owner = st.text_input("Owner", key="sec_egress_owner", max_chars=200)
        expires_on = st.toggle("Policy expires", key="sec_egress_expires_on")
        expires = st.date_input(
            "Expires", value=account_today() + timedelta(days=180),
            key="sec_egress_expires", disabled=not expires_on,
        )
        notes = st.text_area("Business purpose", key="sec_egress_notes", max_chars=4000)
        if pattern:
            statement = insert_egress_policy_sql(
                company=company,
                target_kind=kind,
                pattern=pattern,
                owner=owner,
                expires_on=expires if expires_on else None,
                notes=notes,
                actor=viewer_name(),
            )
            st.code(statement, language="sql")
            if st.button("Save approved destination", key="sec_egress_policy_save"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)


def render_access_reviews(company: str) -> None:
    """Durable access-review campaigns with item-level decisions."""
    section_header("Access review campaigns", "info", "security")
    campaigns = _optional_result(
        security_sql.access_review_campaigns(company=company),
        f"sec_access_review_campaigns_{company}",
        "ACCESS_REVIEW_CAMPAIGNS",
        tier="live",
    )
    if not campaigns.ok:
        _setup_state("Access review campaigns")
        return

    frame = campaigns.df.copy()
    if frame.empty:
        empty_state("no_data_yet", "No access review campaign has been created yet.")
    else:
        pending = int(pd.to_numeric(frame.get("PENDING"), errors="coerce").fillna(0).sum())
        kpi_row([
            {"label": "Campaigns", "value": f"{len(frame):,}"},
            {"label": "Open campaigns", "value": f"{int((frame['STATUS'].astype(str) != 'COMPLETE').sum()):,}"},
            {"label": "Pending decisions", "value": f"{pending:,}", "delta_color": "inverse" if pending else "off"},
        ])
        styled_table(frame, height=260, sort_label="open status then due date")

    if is_operator():
        with st.expander("Create review campaign"):
            default_id = f"AR-{account_today().strftime('%Y%m%d')}-{str(company).upper()}"
            campaign_id = st.text_input(
                "Campaign ID", value=default_id, key="sec_review_new_id", max_chars=80
            )
            title = st.text_input(
                "Title", value=f"{company} role access review", key="sec_review_new_title", max_chars=300
            )
            due = st.date_input(
                "Due", value=account_today() + timedelta(days=14), key="sec_review_new_due"
            )
            statement = create_access_review_sql(
                campaign_id,
                title=title,
                company=company,
                due_date=due,
                actor=viewer_name(),
            )
            st.code(statement, language="sql")
            if st.button("Create campaign snapshot", key="sec_review_create"):
                ok, message = execute_statement(statement, page=_PAGE)
                notify(ok, message)

    if frame.empty or "CAMPAIGN_ID" not in frame:
        return
    ids = frame["CAMPAIGN_ID"].astype(str).tolist()
    selected = st.selectbox("Review campaign", ids, key="sec_review_campaign")
    items = _optional_result(
        security_sql.access_review_items(selected),
        f"sec_access_review_items_{selected}",
        "ACCESS_REVIEW_ITEMS",
        tier="live",
    )
    if not items.ok or items.empty:
        empty_state("no_data_yet", "This campaign has no review items.")
        return
    item_frame = items.df.copy().reset_index(drop=True)
    styled_table(item_frame, height=360, sort_label="pending decision then user and role")
    if st.toggle("Show decision history", key="sec_review_history_on"):
        history = _optional_result(
            security_sql.access_review_decisions(selected),
            f"sec_access_review_history_{selected}",
            "ACCESS_REVIEW_DECISION_LOG",
            tier="live",
        )
        if history.ok and not history.empty:
            styled_table(history.df, height=240, sort_label="decision time descending")
        else:
            empty_state("no_data_yet", "No decisions have been recorded for this campaign.")
    if not is_operator():
        return
    pending_frame = item_frame[item_frame["DECISION"].astype(str) == "PENDING"]
    if pending_frame.empty:
        empty_state("clean", "Every item in this campaign has a decision.")
        return
    labels = (
        pending_frame["USER_NAME"].astype(str)
        + " | "
        + pending_frame["ROLE_NAME"].astype(str)
        + " | "
        + pending_frame["ITEM_ID"].astype(str)
    ).tolist()
    picked = st.selectbox("Pending grant", labels, key="sec_review_item")
    picked_row = pending_frame.iloc[labels.index(picked)]
    decision = st.segmented_control(
        "Decision", ACCESS_DECISIONS, default="KEEP", key="sec_review_decision"
    )
    decision = str(decision or "KEEP")
    reason = st.text_area("Decision reason", key="sec_review_reason", max_chars=4000)
    statement = access_review_decision_sql(
        selected,
        str(picked_row["ITEM_ID"]),
        decision=decision,
        reason=reason,
        actor=viewer_name(),
    )
    st.code(statement, language="sql")
    st.caption("REVOKE records a review decision; it does not execute a Snowflake REVOKE statement.")
    if confirm_gate(
        "DECIDE",
        "Record decision",
        key="sec_review_decide",
        prompt="Type DECIDE to record this review decision",
    ):
        ok, message = execute_statement(statement, page=_PAGE)
        notify(ok, message)
