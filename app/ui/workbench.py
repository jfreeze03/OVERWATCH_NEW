"""Shared operating, entity, and evidence workbenches."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pandas as pd
import streamlit as st

from app.core.identity import viewer_name
from app.core.query import execute_statement, run
from app.core.session import is_operator
from app.core.state import filters, navigation_context, request_navigation
from app.data import mart_sql, workbench_sql
from app.logic.actions import rank_actions
from app.logic.formulas import account_today, credits_to_usd, format_usd, humanize_duration, safe_float
from app.logic.workbench import (
    ACTION_STATUSES,
    CRITICALITIES,
    ENTITY_TYPES,
    action_summary,
    action_transition_sql,
    create_action_sql,
    create_experiment_sql,
    entity_catalog_merge_sql,
    evidence_link_sql,
    watchlist_sql,
)
from app.ui.components import (
    confidence_badge,
    decision_rows,
    empty_state,
    evidence_gate,
    exception_summary,
    guard,
    kpi_row,
    load_settings,
    notify,
    read_model_caption,
    section_header,
    selectable_table,
    snowsight_profile_column,
    status_chips,
    styled_table,
)

_PAGE = "Control Room"


def _entity_metric_card(row: pd.Series, rate: float) -> dict[str, str]:
    label = str(row.get("METRIC") or "Metric")
    value = safe_float(row.get("VALUE"))
    unit = str(row.get("UNIT") or "").lower()
    basis = str(row.get("BASIS") or "").upper()
    if unit == "credits":
        display = format_usd(credits_to_usd(value, rate))
        detail = f"{value:,.2f} credits · {basis.lower()}"
    elif unit == "seconds":
        display = humanize_duration(value, "s")
        detail = basis.lower()
    elif unit == "gb":
        display = f"{value:,.2f} GB"
        detail = basis.lower()
    else:
        display = f"{value:,.0f}"
        detail = basis.lower()
    return {"label": label, "value": display, "delta": detail, "delta_color": "off"}


def _date_value(value: object, fallback_days: int = 7):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return account_today() + timedelta(days=fallback_days)
    return parsed.date()


def _apply_action_context(frame: pd.DataFrame) -> str:
    ctx = navigation_context()
    requested = str(ctx.get("action_id") or "").strip()
    if requested and requested in set(frame.get("ACTION_ID", pd.Series(dtype=str)).astype(str)):
        st.session_state["_ow_action_selected"] = requested
    return str(st.session_state.get("_ow_action_selected") or requested)


def _render_action_detail(row: pd.Series, *, extended: bool) -> None:
    action_id = str(row.get("ACTION_ID") or "")
    section_header(str(row.get("TITLE") or "Work item"),
                   "warn" if str(row.get("SEVERITY", "")).upper() in ("CRITICAL", "HIGH") else "info",
                   "action")
    status_chips([
        (str(row.get("SEVERITY") or "UNSET"),
         "bad" if str(row.get("SEVERITY", "")).upper() in ("CRITICAL", "HIGH") else ""),
        (str(row.get("STATUS") or "OPEN"), "ok" if str(row.get("STATUS", "")).upper() == "DONE" else ""),
        (f"Owner: {row.get('OWNER') or 'unassigned'}", ""),
        (f"Due: {row.get('DUE_DATE') or 'none'}", ""),
    ])
    if str(row.get("DETAIL") or "").strip():
        st.write(str(row.get("DETAIL")))
    confidence = row.get("CONFIDENCE") if extended else None
    if confidence is not None and not pd.isna(confidence):
        confidence_badge(confidence)
    entity_type = str(row.get("SOURCE_ENTITY_TYPE") or "").strip().upper()
    entity_key = str(row.get("SOURCE_ENTITY_KEY") or "").strip()
    if entity_type and entity_key and st.button(
        "Open entity 360", key=f"action_entity_{action_id}", type="tertiary"
    ):
        request_navigation(
            "Control Room", "Entity 360",
            context={"entity_type": entity_type, "entity_key": entity_key},
        )

    if not extended:
        empty_state(
            "needs_setup",
            "Apply V074 to assign, defer, comment on, and resolve work here. The current queue remains readable.",
        )
        return

    if is_operator():
        st.markdown("**Update work item**")
        c1, c2, c3 = st.columns([1, 1.2, 1])
        current_status = str(row.get("STATUS") or "OPEN").upper()
        with c1:
            status = st.selectbox(
                "Status", ACTION_STATUSES,
                index=ACTION_STATUSES.index(current_status) if current_status in ACTION_STATUSES else 0,
                key=f"action_status_{action_id}",
            )
        with c2:
            owner = st.text_input(
                "Owner", value=str(row.get("OWNER") or ""),
                key=f"action_owner_{action_id}", max_chars=200,
            )
        with c3:
            due = st.date_input(
                "Due", value=_date_value(row.get("DUE_DATE")),
                key=f"action_due_{action_id}",
            )
        defer_on = st.toggle(
            "Defer this item", value=bool(row.get("DEFER_UNTIL") and not pd.isna(row.get("DEFER_UNTIL"))),
            key=f"action_defer_on_{action_id}",
        )
        defer = None
        if defer_on:
            defer = st.date_input(
                "Resume on", value=_date_value(row.get("DEFER_UNTIL"), 1),
                key=f"action_defer_{action_id}",
            )
        note = st.text_area(
            "Comment or resolution evidence", key=f"action_note_{action_id}",
            max_chars=4000, height=90,
        )
        statement = action_transition_sql(
            action_id,
            status=status,
            owner=owner,
            due_date=due,
            defer_until=defer,
            note=note,
            actor=viewer_name(),
            request_key=f"ui:{action_id}:{uuid4()}",
        )
        with st.expander("SQL preview"):
            st.code(statement, language="sql")
        if st.button("Save work item", key=f"action_save_{action_id}", type="primary"):
            ok, msg = execute_statement(statement, page=_PAGE)
            notify(ok, msg)
            if ok:
                st.rerun()
    else:
        st.caption("Lifecycle changes require an operator; evidence remains visible to every viewer.")

    activity = run(
        workbench_sql.action_activity(action_id), page=_PAGE,
        key=f"action_activity_{action_id}", tier="live", source="ACTION_ACTIVITY",
    )
    if activity.ok and not activity.empty:
        st.markdown("**Activity and comments**")
        styled_table(activity.df, height=220, sort_label="newest")

    links = run(
        workbench_sql.evidence_links("ACTION", action_id), page=_PAGE,
        key=f"action_evidence_{action_id}", tier="live", source="EVIDENCE_LINKS",
    )
    st.markdown("**Evidence graph**")
    if links.ok and links.empty:
        empty_state("no_data_yet", "No typed evidence is linked to this action yet.")
    elif guard(links, ""):
        linked, cfg = snowsight_profile_column(links.df, _PAGE, id_col="QUERY_ID")
        styled_table(linked, height=240, column_config=cfg)

    if is_operator():
        with st.expander("Link evidence"):
            to_type = st.selectbox("Evidence type", ENTITY_TYPES, key=f"ev_type_{action_id}")
            to_id = st.text_input("Evidence ID", key=f"ev_id_{action_id}", max_chars=500)
            relationship = st.selectbox(
                "Relationship", ("SUPPORTS", "CAUSED_BY", "VERIFIED_BY", "RESULTED_IN", "RELATED_TO"),
                key=f"ev_rel_{action_id}",
            )
            query_id = st.text_input("Query ID", key=f"ev_qid_{action_id}", max_chars=80)
            evidence_note = st.text_area("Evidence note", key=f"ev_note_{action_id}", max_chars=4000)
            if to_id:
                link_sql = evidence_link_sql(
                    from_type="ACTION", from_id=action_id, to_type=to_type, to_id=to_id,
                    relationship=relationship, query_id=query_id, note=evidence_note,
                    actor=viewer_name(),
                )
                st.code(link_sql, language="sql")
                if st.button("Add evidence link", key=f"ev_add_{action_id}"):
                    ok, msg = execute_statement(link_sql, page=_PAGE)
                    notify(ok, msg)
                    if ok:
                        st.rerun()

        with st.expander("Start optimization experiment"):
            exp_title = st.text_input("Experiment", key=f"exp_title_{action_id}", max_chars=500)
            hypothesis = st.text_area("Hypothesis", key=f"exp_hyp_{action_id}", max_chars=4000)
            baseline = st.text_area("Baseline", key=f"exp_base_{action_id}", max_chars=4000)
            target = st.text_area("Target", key=f"exp_target_{action_id}", max_chars=4000)
            rollback = st.text_area("Rollback condition", key=f"exp_rollback_{action_id}", max_chars=4000)
            observe_to = st.date_input(
                "Observe through", value=account_today() + timedelta(days=14),
                key=f"exp_end_{action_id}",
            )
            if exp_title and hypothesis and entity_type and entity_key:
                exp_sql = create_experiment_sql(
                    action_id=action_id, entity_type=entity_type, entity_key=entity_key,
                    title=exp_title, hypothesis=hypothesis, baseline=baseline,
                    target=target, rollback=rollback, observation_end=observe_to,
                    actor=viewer_name(),
                )
                st.code(exp_sql, language="sql")
                if st.button("Create experiment", key=f"exp_create_{action_id}"):
                    ok, msg = execute_statement(exp_sql, page=_PAGE)
                    notify(ok, msg)


def render_action_center(company: str) -> None:
    """Persistent owner queue with exact-row navigation and lifecycle controls."""
    section_header("Action Center", "warn", "action")
    include_closed = st.toggle("Include completed work", key="action_include_closed")
    read_model_caption("action_center")
    extended_res = run(
        workbench_sql.action_center(company, include_closed, 500), page=_PAGE,
        key=f"action_center_{company}_{include_closed}", tier="live",
        source="ACTION_QUEUE + V074 lifecycle context",
    )
    extended = extended_res.ok
    if extended:
        frame = extended_res.df.copy()
    else:
        base = run(
            mart_sql.action_queue(500), page=_PAGE, key="action_center_legacy",
            tier="live", source="ACTION_QUEUE (legacy shape)",
        )
        if not base.ok:
            empty_state("needs_setup", "The action queue is not installed yet.")
            return
        frame = base.df.copy()
        empty_state(
            "needs_setup",
            "V074 is pending. Showing the existing read-only queue; lifecycle, evidence, ownership, and experiments unlock after the owner applies it.",
        )

    if frame.empty:
        empty_state("clean", "No work is waiting in the selected scope.")
    else:
        summary = action_summary(frame)
        exceptions = []
        if summary["critical_high"]:
            exceptions.append({
                "label": "Priority work",
                "value": f"{summary['critical_high']:,.0f}",
                "detail": "Critical or high-severity items need an owner decision.",
                "severity": "bad",
            })
        if summary["overdue"]:
            exceptions.append({
                "label": "Overdue",
                "value": f"{summary['overdue']:,.0f}",
                "detail": "Due dates have passed while the items remain open.",
                "severity": "warn",
            })
        if summary["unassigned"]:
            exceptions.append({
                "label": "Unassigned",
                "value": f"{summary['unassigned']:,.0f}",
                "detail": "Open work has no accountable owner.",
                "severity": "warn",
            })
        exception_summary(
            exceptions,
            "No critical/high, overdue, or unassigned open work in this scope.",
        )
        kpi_row([
            {"label": "Open work", "value": f"{summary['open']:,.0f}"},
            {"label": "Critical / high", "value": f"{summary['critical_high']:,.0f}",
             "severity": "bad" if summary["critical_high"] else "ok"},
            {"label": "Overdue", "value": f"{summary['overdue']:,.0f}",
             "severity": "warn" if summary["overdue"] else "ok"},
            {"label": "Unassigned", "value": f"{summary['unassigned']:,.0f}"},
            {"label": "Estimated opportunity", "value": format_usd(summary["estimated_usd"]),
             "help": "Open estimates only; never mixed with verified savings."},
        ])
        display = frame.reset_index(drop=True)
        if not include_closed:
            ranked = rank_actions(display, limit=1000)
            if not ranked.empty:
                display = ranked.reset_index(drop=True)
        selection = decision_rows(
            display,
            key="action_center_table",
            decision_col="TITLE",
            why_col="DETAIL",
            impact_col="ESTIMATED_USD",
            confidence_col="CONFIDENCE",
            owner_col="OWNER",
            status_col="STATUS",
            next_col="DUE_DATE",
            context_cols=("SEVERITY", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY"),
            height=340,
            sort_label="severity, overdue, estimated value, then age",
        )
        selected_id = _apply_action_context(display)
        if selection is not None:
            try:
                selected_id = str(display.iloc[int(selection)]["ACTION_ID"])
                st.session_state["_ow_action_selected"] = selected_id
            except (KeyError, IndexError, ValueError, TypeError):
                pass
        if selected_id:
            matches = display[display["ACTION_ID"].astype(str) == selected_id]
            if not matches.empty:
                _render_action_detail(matches.iloc[0], extended=extended)

    if is_operator() and extended:
        with st.expander("Create work item"):
            title = st.text_input("Title", key="action_new_title", max_chars=300)
            detail = st.text_area("Detail", key="action_new_detail", max_chars=2000)
            c1, c2, c3 = st.columns(3)
            with c1:
                severity = st.selectbox("Severity", ("HIGH", "MEDIUM", "LOW", "INFO"), key="action_new_sev")
            with c2:
                owner = st.text_input("Owner", value="DBA", key="action_new_owner", max_chars=200)
            with c3:
                due = st.date_input("Due", value=account_today() + timedelta(days=7), key="action_new_due")
            entity_type = st.selectbox("Entity type", ENTITY_TYPES, key="action_new_type")
            entity_key = st.text_input("Entity key", key="action_new_key", max_chars=500)
            confidence = st.slider("Confidence", 0.0, 1.0, 0.7, 0.05, key="action_new_conf")
            estimated = st.number_input("Estimated USD", min_value=0.0, step=50.0, key="action_new_usd")
            if title and entity_key:
                insert_sql = create_action_sql(
                    title=title, detail=detail, company=company, severity=severity,
                    owner=owner, due_date=due, source="Action Center",
                    entity_type=entity_type, entity_key=entity_key,
                    confidence=confidence, estimated_usd=estimated,
                )
                st.code(insert_sql, language="sql")
                if st.button("Create work item", key="action_new_exec"):
                    ok, msg = execute_statement(insert_sql, page=_PAGE)
                    notify(ok, msg)


def _seed_entity_context() -> None:
    ctx = navigation_context()
    kind = str(ctx.get("entity_type") or "").strip().upper()
    key = str(ctx.get("entity_key") or "").strip()
    signature = f"{kind}:{key}"
    if kind in ENTITY_TYPES and key and st.session_state.get("_ow_entity_context_applied") != signature:
        st.session_state["entity_360_type"] = kind
        st.session_state["entity_360_key"] = key
        st.session_state["_ow_entity_context_applied"] = signature


def _render_catalog_editor(kind: str, key: str, row: pd.Series | None, company: str) -> None:
    if not is_operator():
        return
    with st.expander("Ownership and service contract"):
        current = row if row is not None else pd.Series(dtype=object)
        label = st.text_input("Label", value=str(current.get("LABEL") or key), key="entity_label", max_chars=500)
        team = st.text_input("Team", value=str(current.get("TEAM") or ""), key="entity_team", max_chars=200)
        owner = st.text_input("Owner", value=str(current.get("OWNER_NAME") or ""), key="entity_owner", max_chars=200)
        steward = st.text_input("Steward", value=str(current.get("STEWARD") or ""), key="entity_steward", max_chars=200)
        on_call = st.text_input("On-call contact", value=str(current.get("ON_CALL") or ""), key="entity_oncall", max_chars=500)
        crit_now = str(current.get("CRITICALITY") or "STANDARD").upper()
        criticality = st.selectbox(
            "Criticality", CRITICALITIES,
            index=CRITICALITIES.index(crit_now) if crit_now in CRITICALITIES else 2,
            key="entity_criticality",
        )
        product = st.text_input("Data product", value=str(current.get("DATA_PRODUCT") or ""), key="entity_product", max_chars=300)
        slo_name = st.text_input("SLO", value=str(current.get("SLO_NAME") or ""), key="entity_slo", max_chars=300)
        notes = st.text_area("Notes", value=str(current.get("NOTES") or ""), key="entity_notes", max_chars=4000)
        sql = entity_catalog_merge_sql(
            entity_type=kind, entity_key=key, label=label, company=company,
            team=team, owner=owner, steward=steward, on_call=on_call,
            criticality=criticality, data_product=product, slo_name=slo_name,
            notes=notes, actor=viewer_name(),
        )
        st.code(sql, language="sql")
        if st.button("Save ownership", key="entity_save"):
            ok, msg = execute_statement(sql, page=_PAGE)
            notify(ok, msg)


def render_entity_360(company: str) -> None:
    """One context surface for ownership, work, changes, savings and evidence."""
    _seed_entity_context()
    section_header("Entity 360", "info", "search")
    c1, c2 = st.columns([0.85, 2.15])
    with c1:
        kind = st.selectbox("Entity type", ENTITY_TYPES, key="entity_360_type")
    with c2:
        key = st.text_input("Entity key", key="entity_360_key", max_chars=500)
    if not key:
        catalog = run(
            workbench_sql.entity_catalog(limit=100), page=_PAGE, key="entity_catalog_browse",
            tier="live", source="ENTITY_CATALOG",
        )
        if catalog.ok and not catalog.empty:
            st.markdown("**Catalog**")
            styled_table(catalog.df, height=320)
        else:
            empty_state("no_data_yet", "Choose an entity or open one from an action, table, or universal search.")
        return

    record = run(
        workbench_sql.entity_record(kind, key), page=_PAGE,
        key=f"entity_record_{kind}_{key}", tier="live", source="ENTITY_CATALOG",
    )
    catalog_row = record.df.iloc[0] if record.ok and not record.empty else None
    if not record.ok:
        empty_state("needs_setup", "V074 is required for the ownership catalog and watchlists.")
    if catalog_row is not None:
        status_chips([
            (str(catalog_row.get("CRITICALITY") or "STANDARD"),
             "bad" if str(catalog_row.get("CRITICALITY", "")).upper() == "CRITICAL" else ""),
            (f"Team: {catalog_row.get('TEAM') or 'unassigned'}", ""),
            (f"Owner: {catalog_row.get('OWNER_NAME') or 'unassigned'}", ""),
            (f"Product: {catalog_row.get('DATA_PRODUCT') or 'unmapped'}", ""),
            (f"SLO: {catalog_row.get('SLO_NAME') or 'not set'}", ""),
        ])
        if str(catalog_row.get("NOTES") or "").strip():
            st.caption(str(catalog_row.get("NOTES")))
    else:
        empty_state("no_data_yet", "This entity has no ownership record yet.")

    if kind in workbench_sql.ENTITY_METRIC_TYPES:
        scoped_days = int(filters().get("days") or 30)
        metrics = run(
            workbench_sql.entity_metric_snapshot(kind, key, scoped_days), page=_PAGE,
            key=f"entity_metrics_{kind}_{key}_{scoped_days}", tier="recent",
            source=f"existing OVERWATCH marts ({kind.lower()} grain)",
        )
        if metrics.ok and not metrics.empty:
            rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
            cards = [_entity_metric_card(row, rate) for _, row in metrics.df.head(5).iterrows()]
            kpi_row(cards)
            bases = ", ".join(sorted(set(metrics.df["BASIS"].dropna().astype(str))))
            as_of = pd.to_datetime(metrics.df["AS_OF"], errors="coerce").max()
            freshness = f" · through {as_of}" if pd.notna(as_of) else ""
            st.caption(f"{scoped_days}-day entity evidence · basis: {bases}{freshness}")
        elif metrics.ok:
            empty_state("no_data_yet", "No measured entity metrics exist in this window.")

    viewer = viewer_name()
    if viewer and record.ok:
        watched = run(
            workbench_sql.watchlist(viewer), page=_PAGE, key="entity_watchlist",
            tier="live", source="USER_WATCHLIST",
        )
        is_watched = bool(
            watched.ok and not watched.empty
            and ((watched.df["ENTITY_TYPE"].astype(str).str.upper() == kind)
                 & (watched.df["ENTITY_KEY"].astype(str).str.upper() == key.upper())).any()
        )
        label = str(catalog_row.get("LABEL") or key) if catalog_row is not None else key
        if st.button("Unwatch" if is_watched else "Watch", key="entity_watch_toggle", type="tertiary"):
            try:
                sql = watchlist_sql(viewer, kind, key, label, remove=is_watched)
                ok, msg = execute_statement(sql, page=_PAGE)
            except ValueError as exc:
                ok, msg = False, str(exc)
            notify(ok, msg)
            if ok:
                st.rerun()

    _render_catalog_editor(kind, key, catalog_row, company)

    related = run(
        workbench_sql.related_actions(kind, key), page=_PAGE,
        key=f"entity_actions_{kind}_{key}", tier="live", source="ACTION_QUEUE",
    )
    st.markdown("**Work and outcomes**")
    if related.ok and not related.empty:
        styled_table(related.df, height=240)
    else:
        empty_state("no_data_yet", "No action is linked to this entity.")

    if evidence_gate(
        "entity_360",
        key=f"entity_evidence_{kind}_{key}",
        label="Load evidence and outcome history",
    ):
        remediations = run(
            workbench_sql.related_remediations(key), page=_PAGE,
            key=f"entity_remed_{kind}_{key}", tier="live", source="REMEDIATION_LOG",
        )
        savings = run(
            workbench_sql.related_savings(key), page=_PAGE,
            key=f"entity_savings_{kind}_{key}", tier="live", source="SAVINGS_LEDGER",
        )
        links = run(
            workbench_sql.evidence_links(kind, key), page=_PAGE,
            key=f"entity_links_{kind}_{key}", tier="live", source="EVIDENCE_LINKS",
        )
        if remediations.ok and not remediations.empty:
            st.markdown("**Remediation history**")
            styled_table(remediations.df, height=220)
        if savings.ok and not savings.empty:
            st.markdown("**Savings outcomes**")
            styled_table(savings.df, height=220)
        if links.ok and not links.empty:
            st.markdown("**Evidence relationships**")
            linked, cfg = snowsight_profile_column(links.df, _PAGE, id_col="QUERY_ID")
            styled_table(linked, height=240, column_config=cfg)
        if all(r.ok and r.empty for r in (remediations, savings, links)):
            empty_state("no_data_yet", "No evidence, remediation, or savings outcome is linked yet.")


def render_watchlist() -> None:
    viewer = viewer_name()
    if not viewer:
        empty_state("needs_setup", "Viewer identity is unavailable, so a personal watchlist cannot be loaded.")
        return
    result = run(
        workbench_sql.watchlist(viewer), page=_PAGE, key="watchlist_all",
        tier="live", source="USER_WATCHLIST",
    )
    if not guard(result, "No entities are on your watchlist yet."):
        return
    frame = result.df.reset_index(drop=True)
    selected = selectable_table(frame, key="watchlist_table", height=260, sort_label="newest watched")
    if selected is not None:
        row = frame.iloc[int(selected)]
        request_navigation(
            "Control Room", "Entity 360",
            context={"entity_type": str(row["ENTITY_TYPE"]), "entity_key": str(row["ENTITY_KEY"])},
        )
