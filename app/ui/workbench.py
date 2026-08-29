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
from app.data import graph_sql, mart27_sql, mart_sql, workbench_sql
from app.logic import lineage
from app.logic.actions import rank_actions
from app.logic.formulas import (
    account_today,
    credits_to_usd,
    format_usd,
    humanize_duration,
    humanize_gb,
    md_dollars,
    safe_float,
)
from app.logic.sizing import size_recommendations
from app.logic.watch_monitor import watch_summary, watched_status
from app.logic.wh_health import warehouse_health
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
    watchlist_threshold_status,
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
    master_detail,
    notify,
    read_model_caption,
    section_header,
    selectable_table,
    served_days,
    snowsight_object_url,
    snowsight_profile_column,
    stamp_write,
    status_chips,
    styled_table,
    write_gate_open,
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
        display = humanize_gb(value)
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
        # F58/F54: a plain-English effect line derived from a diff of the form
        # against the current row — it names exactly what the write ACTUALLY
        # does, and its emptiness is the dirty check (nothing changed and no
        # comment = nothing to save; a no-op save would otherwise write an empty
        # audit row). Review fix: SP_ACTION_LIFECYCLE uses COALESCE-keep
        # semantics, so a BLANK owner and a toggled-OFF defer are no-ops it
        # cannot perform — the line must not promise an unassign / un-defer the
        # write silently drops, and an undated row must not be sent a fabricated
        # due (the widget defaults to today+7). Clearing an owner/defer needs an
        # explicit SP path (a follow-up migration).
        _cur_owner = str(row.get("OWNER") or "").strip()
        _cur_defer_on = bool(row.get("DEFER_UNTIL") and not pd.isna(row.get("DEFER_UNTIL")))
        _had_due = not pd.isna(row.get("DUE_DATE"))
        _due_changed = due != _date_value(row.get("DUE_DATE"))
        # only send a due when it's real (the row had one, or the operator picked
        # one on a previously-undated row) — else NULL, so the SP keeps NULL.
        _due_arg = due if (_had_due or _due_changed) else None
        _effects: list[str] = []
        if status != current_status:
            _effects.append(f"set status {current_status} → {status}")
        if owner.strip() and owner.strip() != _cur_owner:
            _effects.append(f"assign to {owner.strip()}")
        if _due_changed:
            _effects.append(f"set due → {due}")
        if defer_on and (not _cur_defer_on or defer != _date_value(row.get("DEFER_UNTIL"), 1)):
            _effects.append(f"defer until {defer}")
        if str(note or "").strip():
            _effects.append("add a comment")
        _dirty = bool(_effects)
        statement = action_transition_sql(
            action_id,
            status=status,
            owner=owner,
            due_date=_due_arg,
            defer_until=defer,
            note=note,
            actor=viewer_name(),
            request_key=f"ui:{action_id}:{uuid4()}",
        )
        st.caption("This will " + ", ".join(_effects) + " — audited." if _dirty
                   else "No changes to save yet — edit a field or add a comment.")
        with st.expander("SQL preview"):
            st.code(statement, language="sql")
        if (st.button("Save work item", key=f"action_save_{action_id}", type="primary",
                      disabled=not _dirty)
                and write_gate_open(f"action_save_{action_id}")):
            ok, msg = execute_statement(statement, page=_PAGE)
            stamp_write(f"action_save_{action_id}", ok)  # C48
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
                if (st.button("Add evidence link", key=f"ev_add_{action_id}")
                        and write_gate_open(f"ev_add_{action_id}")):
                    ok, msg = execute_statement(link_sql, page=_PAGE)
                    stamp_write(f"ev_add_{action_id}", ok)  # C48
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
                if (st.button("Create experiment", key=f"exp_create_{action_id}")
                        and write_gate_open(f"exp_create_{action_id}")):
                    ok, msg = execute_statement(exp_sql, page=_PAGE)
                    stamp_write(f"exp_create_{action_id}", ok)  # C48
                    notify(ok, msg)
                    # rec48: this INSERT is non-idempotent and the surface does not
                    # otherwise rerun — rerun so the table re-renders as the receipt and
                    # the form/button reset, or a second click double-inserts.
                    if ok:
                        st.rerun()


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
            mart_sql.action_queue(500, company), page=_PAGE,
            key=f"action_center_legacy_{company}",
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
             "help": "Sum of open ESTIMATED_USD. Each estimate may carry a time basis "
                     "(the PERIOD column below; NULL = unspecified): monthly run-rates, "
                     "one-time savings, and unlabeled figures are added at face value, so "
                     "read the per-row basis before trusting the total. Never mixed with "
                     "verified savings."},
        ])
        display = frame.reset_index(drop=True)
        if not include_closed:
            ranked = rank_actions(display, limit=1000)
            if not ranked.empty:
                display = ranked.reset_index(drop=True)
        # C47: ranked work LEFT, the selected item's editor RIGHT (was stacked).
        # master_detail owns the column split + identity-based sticky selection;
        # the table flavor (decision_rows) and the editor (_render_action_detail,
        # closing over `extended`) stay here. A deep-link action_id preselects it.
        def _ac_list(display_df, list_key):
            return decision_rows(
                display_df, key=list_key,
                decision_col="TITLE", why_col="DETAIL", impact_col="ESTIMATED_USD",
                confidence_col="CONFIDENCE", owner_col="OWNER", status_col="STATUS",
                next_col="DUE_DATE",
                context_cols=("SEVERITY", "PERIOD", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY"),
                height=340, sort_label="severity, overdue, estimated value, then age",
                impact_help="Authored ESTIMATE (modeled, not billed). Scenarios de-duplicate "
                            "these by entity and never mix them with verified savings.",
                confidence_label="Confidence (authored)",
                confidence_help="Authored confidence (0-1) set when the action was created "
                                "(operator or recommendation engine) — "
                                "a stated belief, not a measurement.")

        # Deliver the deep-link action_id ONCE per arrival (review fix): consume
        # it from the nav context so a later manual click sticks, and a repeat
        # navigation to the SAME id re-focuses it (the id is re-populated then).
        _ctx_id = str(navigation_context().get("action_id") or "").strip()
        _preselect = _ctx_id if _ctx_id in set(display["ACTION_ID"].astype(str)) else ""
        if _preselect:
            _nav = st.session_state.get("_ow_nav_context")
            if isinstance(_nav, dict) and _nav.get("action_id"):
                st.session_state["_ow_nav_context"] = {
                    k: v for k, v in _nav.items() if k != "action_id"}
        master_detail(
            display, key="action_center", id_col="ACTION_ID",
            list_render_fn=_ac_list,
            detail_render_fn=lambda r: _render_action_detail(r, extended=extended),
            preselect_id=_preselect,
            empty_detail_msg="Select a work item on the left to see and edit its detail.")

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
            # rec19: warn (don't block) if this entity already has an open work item —
            # both Action Center and Security can create against the same entity, and the
            # INSERT does not dedupe. Reuses related_actions (open/in-progress first).
            if entity_key.strip():
                _dupes = run(workbench_sql.related_actions(entity_type, entity_key), page=_PAGE,
                             key=f"action_dupe_{entity_type}_{entity_key.strip().upper()}",
                             tier="recent", source="ACTION_QUEUE")
                if _dupes.usable():
                    _open_dupes = _dupes.df[_dupes.df["STATUS"].astype(str).str.upper()
                                            .isin(("OPEN", "IN_PROGRESS"))]
                    if not _open_dupes.empty:
                        st.warning(f"{len(_open_dupes)} open work item(s) already track "
                                   f"{entity_type} {entity_key.strip()} — open one from the queue "
                                   "above instead of duplicating, or continue if this is separate.")
            confidence = st.slider("Confidence", 0.0, 1.0, 0.7, 0.05, key="action_new_conf")
            ec1, ec2 = st.columns(2)
            with ec1:
                estimated = st.number_input("Estimated USD", min_value=0.0, step=50.0, key="action_new_usd")
            with ec2:
                # DS #7: label the estimate's time basis so it is never summed with a
                # different clock. Defaults to Monthly (the common optimization run-rate);
                # "Unspecified" stores NULL for a genuinely unlabeled figure.
                period_label = st.selectbox(
                    "Estimate basis", ("Monthly", "One-time", "Annual", "Unspecified"),
                    key="action_new_period",
                    help="How to read the dollar figure: a monthly run-rate, a one-time "
                         "saving, an annual figure, or unspecified (no stated basis).")
            if title and entity_key:
                insert_sql = create_action_sql(
                    title=title, detail=detail, company=company, severity=severity,
                    owner=owner, due_date=due, source="Action Center",
                    entity_type=entity_type, entity_key=entity_key,
                    confidence=confidence, estimated_usd=estimated,
                    period={"Monthly": "MONTHLY", "One-time": "ONE_TIME",
                            "Annual": "ANNUAL", "Unspecified": ""}[period_label],
                )
                st.code(insert_sql, language="sql")
                if (st.button("Create work item", key="action_new_exec")
                        and write_gate_open("action_new_exec")):
                    ok, msg = execute_statement(insert_sql, page=_PAGE)
                    stamp_write("action_new_exec", ok)  # C48
                    notify(ok, msg)
                    # rec48: non-idempotent INSERT with no other rerun — rerun so the
                    # queue re-renders as the receipt and the form/button reset.
                    if ok:
                        st.rerun()


def _seed_entity_context() -> None:
    ctx = navigation_context()
    kind = str(ctx.get("entity_type") or "").strip().upper()
    key = str(ctx.get("entity_key") or "").strip()
    signature = f"{kind}:{key}"
    if kind in ENTITY_TYPES and key and st.session_state.get("_ow_entity_context_applied") != signature:
        st.session_state["entity_360_type"] = kind
        st.session_state["entity_360_key"] = key
        # rec12: clear this kind's catalog pick so a stale earlier pick cannot shadow
        # the drilled key (the picker writes into entity_360_key, so a leftover pick
        # would otherwise re-populate the text box on the next render).
        st.session_state[f"entity_360_pick_{kind}"] = ""
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
        if st.button("Save ownership", key="entity_save") and write_gate_open("entity_save"):
            ok, msg = execute_statement(sql, page=_PAGE)
            stamp_write("entity_save", ok)  # C48
            notify(ok, msg)


def _render_data_product_detail(product: str) -> None:
    """#14: a real Entity 360 view for a data product — its constituent catalog
    entities and a rollup — instead of the empty catalog-record page it used to show
    (DATA_PRODUCT is a catalog attribute, not an entity type)."""
    detail = run(
        workbench_sql.product_detail(product), page=_PAGE,
        key=f"product_detail_{product}", tier="live",
        source="ENTITY_CATALOG (by data product)",
    )
    if not detail.ok:
        empty_state("needs_setup", "V074 is required for the ownership catalog.")
        return
    if detail.empty:
        empty_state("no_data_yet",
                    f"No catalog entities are mapped to the '{product}' data product yet.")
        return
    df = detail.df
    crit = str(df.iloc[0].get("CRITICALITY") or "STANDARD")   # ORDERed most-severe first
    owners = sorted({str(o).strip() for o in df["OWNER_NAME"].dropna() if str(o).strip()})
    owner_label = owners[0] if len(owners) == 1 else (", ".join(owners) if owners else "unassigned")
    status_chips([
        (f"Data product: {product}", ""),
        (f"{len(df)} entities", ""),
        (crit, "bad" if crit.upper() == "CRITICAL" else ""),
        (f"Owner: {owner_label}", "warn" if len(owners) > 1 else ""),
    ])
    if len(owners) > 1:
        st.caption("This product spans multiple owners — resolve ownership in the catalog "
                   "(Decision Studio > Products flags the conflict).")
    st.markdown("**Constituent entities**")
    styled_table(df, height=360, slug="product-detail",
                 sort_label="most-severe criticality, then type")


def render_entity_360(company: str) -> None:
    """One context surface for ownership, work, changes, savings and evidence."""
    _seed_entity_context()
    section_header("Entity 360", "info", "search")
    c1, c2 = st.columns([0.85, 2.15])
    with c1:
        kind = st.selectbox("Entity type", ENTITY_TYPES, key="entity_360_type")
    with c2:
        # rec12: seed a picker from the catalog for this kind — a QUERY_FINGERPRINT is a
        # hash nobody memorizes. The picker POPULATES the free-text box (the single
        # source of truth + drill target), so a catalog pick and a drill never fight
        # over which key wins; the text box stays the escape hatch for un-catalogued
        # entities (e.g. a fingerprint).
        cat = run(workbench_sql.entity_catalog(entity_type=kind, limit=200), page=_PAGE,
                  key=f"entity_pick_{kind}", tier="recent", source="ENTITY_CATALOG", probe=True)
        if cat.usable() and "ENTITY_KEY" in cat.df.columns:
            _labels = {str(r["ENTITY_KEY"]): str(r.get("LABEL") or r["ENTITY_KEY"])
                       for _, r in cat.df.iterrows()}

            def _apply_pick(_k: str = kind) -> None:
                _p = str(st.session_state.get(f"entity_360_pick_{_k}") or "")
                if _p:
                    st.session_state["entity_360_key"] = _p

            st.selectbox(
                f"Pick a catalogued {kind.lower()}", ["", *list(_labels)],
                format_func=lambda k: "— or type a key below —" if not k else _labels.get(k, k),
                key=f"entity_360_pick_{kind}", on_change=_apply_pick)
        key = st.text_input("Entity key", key="entity_360_key", max_chars=500,
                            help="For an entity not in the catalog (e.g. a query fingerprint). "
                                 "Picking from the catalog above fills this in.")
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

    if kind == "DATA_PRODUCT":
        # #14: a data product isn't a catalog entity — render its constituents, not an
        # empty ownership record.
        _render_data_product_detail(key)
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
    # F27: the native-console complement to this in-app 360 — warehouses,
    # databases and table objects have stable Snowsight pages; other kinds
    # (fingerprints, tasks, users) don't, so no link renders for them.
    _ss_url = snowsight_object_url(kind, key, _PAGE)
    if _ss_url:
        # link_button keeps the URL out of markdown parsing entirely — a hostile
        # or quoted-identifier key can't break the link or smuggle live markdown.
        st.link_button("Open in Snowsight ↗", _ss_url)

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
    else:
        # rec26: ALERT/INCIDENT/ACTION/DATA_PRODUCT have no metric snapshot defined —
        # say so, so the absent KPI block does not read as a broken/empty load.
        st.caption(f"No metric snapshot is defined for {kind} entities — the ownership "
                   "above and linked work below are the evidence for this type.")

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
        # C48: encode the toggle direction in both keys — the label flip changes the
        # widget's element identity (Streamlit drops a queued phantom click from a slow
        # write), and the per-direction/per-entity latch never swallows a legit undo.
        _dir = "rm" if is_watched else "add"
        if (st.button("Unwatch" if is_watched else "Watch", key=f"entity_watch_toggle_{_dir}", type="tertiary")
                and write_gate_open(f"entity_watch_{_dir}:{kind}:{key}")):
            try:
                sql = watchlist_sql(viewer, kind, key, label, remove=is_watched)
                ok, msg = execute_statement(sql, page=_PAGE)
            except ValueError as exc:
                ok, msg = False, str(exc)
            stamp_write(f"entity_watch_{_dir}:{kind}:{key}", ok)  # C48
            notify(ok, msg)
            if ok:
                st.rerun()

    _render_catalog_editor(kind, key, catalog_row, company)

    # CR15: the "changes" this panel's docstring promises (ownership, work,
    # CHANGES, savings, evidence). Reuses the change registries the Operations
    # Change impact tab surfaces; types with no registry get a precise note.
    st.markdown("**Recent changes**")
    if kind in workbench_sql.ENTITY_CHANGE_TYPES:
        changes = run(
            workbench_sql.entity_recent_changes(kind, key), page=_PAGE,
            key=f"entity_changes_{kind}_{key}", tier="live",
            source="OBJECT_CHANGE_REGISTRY / WAREHOUSE_CHANGE_REGISTRY",
        )
        if changes.ok and not changes.empty:
            styled_table(changes.df, height=200)
        elif changes.ok:
            empty_state("no_data_yet", "No tracked change in the last 90 days — the "
                        "change-impact scans fill this (Operations → Change impact).")
        else:
            empty_state("needs_setup", "Change tracking needs the change-impact scan (V010).")
    else:
        st.caption(f"Change tracking is not defined for {kind} entities — warehouse settings "
                   "and proc/task deploys are tracked; other types are not.")

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

        if kind == "OBJECT":
            _object_blast_radius_panel(key)


_BLAST_WINDOW_DAYS = 30


def _object_blast_radius_panel(key: str) -> None:
    """#19: downstream blast radius for a table/view — its DECLARED dependents
    (OBJECT_DEPENDENCIES) paired with the OBSERVED consumers who actually touch them
    (ACCESS_HISTORY). Answers "if I ALTER this — or it breaks — what depends on it?"
    as a count of recorded facts, never a 'safe to change' verdict. Both sources are
    probe-gated (OBJECT_DEPENDENCIES is unverified here; ACCESS_HISTORY is Enterprise-
    only), so each half degrades on its own."""
    st.markdown("**Downstream blast radius**")
    # max_rows honors the builder's own 50k clamp instead of the 5k default, and
    # `truncated` is surfaced below — the declared-dependent count is never silently cut.
    edges = run(graph_sql.object_dependency_edges(), page=_PAGE, key="object_dep_edges",
                tier="historical", source="ACCOUNT_USAGE.OBJECT_DEPENDENCIES",
                probe=True, max_rows=50000)
    if not edges.ok:
        st.caption("Declared object lineage needs ACCOUNT_USAGE.OBJECT_DEPENDENCIES, "
                   "which isn't available to this role/account yet — blast radius hidden.")
        return
    if edges.truncated:
        st.warning("The account-wide dependency graph hit the row cap, so 'Declared "
                   "dependents' is a LOWER BOUND and may omit real downstream dependents.")
    deps = lineage.downstream_dependents(edges.df if edges.usable() else pd.DataFrame(), key)
    if deps.empty:
        # review fix: split the fused message — the zero-rows FACT takes the
        # vocabulary's quiet caption, but the safety caveat gates a destructive
        # decision (the safe-to-ALTER inference this empty invites) and keeps
        # its info weight. Deliberate exception to the C25 single-call shape.
        empty_state("no_data_yet",
                    "No DECLARED downstream dependents recorded for this object.")
        st.info("OBJECT_DEPENDENCIES records view/matview/policy references, not "
                "stored-procedure or dynamic-SQL usage — this is not proof nothing "
                "depends on it, and not a 'safe to ALTER' verdict.")
        # The declared graph is blind exactly in the proc/dynamic-SQL case — still fetch
        # the object's OWN observed consumers, the evidence that matters most here.
        root_cons = run(graph_sql.object_blast_consumers((key,), _BLAST_WINDOW_DAYS),
                        page=_PAGE, key=f"object_blast_root_{key}", tier="recent",
                        source="ACCOUNT_USAGE.ACCESS_HISTORY (Enterprise)", probe=True)
        if root_cons.usable() and not root_cons.empty:
            r = root_cons.df.iloc[0]
            q, u = int(safe_float(r.get("QUERIES"))), int(safe_float(r.get("USERS")))
            st.caption(f"Observed directly: {q} queries by {u} user(s) touched this object "
                       f"in {_BLAST_WINDOW_DAYS}d (ACCESS_HISTORY, includes proc/dynamic SQL) "
                       "— a count of recorded facts, not a safe-to-ALTER verdict.")
        elif not root_cons.ok:
            st.caption("Observed-consumer evidence (ACCESS_HISTORY, Enterprise-only) is "
                       "unavailable here, so proc/dynamic-SQL usage can't be measured.")
        return
    # observed consumers for the object itself + its declared dependents
    fqns = (key, *deps["FQN"].tolist())
    cons = run(graph_sql.object_blast_consumers(tuple(fqns), _BLAST_WINDOW_DAYS),
               page=_PAGE, key=f"object_blast_cons_{key}", tier="recent",
               source="ACCOUNT_USAGE.ACCESS_HISTORY (Enterprise)", probe=True)
    measured_half = cons.ok   # False => could NOT measure (never conflate with "measured zero")
    consumers_df = cons.df if cons.usable() else pd.DataFrame()
    summary = lineage.blast_summary(edges.df, consumers_df, key, window_days=_BLAST_WINDOW_DAYS)
    radius = lineage.build_blast_radius(edges.df, consumers_df, key, window_days=_BLAST_WINDOW_DAYS)
    kpi_row([
        {"label": "Declared dependents",
         "value": f"{summary['dependents']}" + (" (lower bound)" if edges.truncated else ""),
         "help": "Objects that reference this one (transitively) per OBJECT_DEPENDENCIES "
                 "— declared view/matview/policy refs only, not procs/dynamic SQL."},
        {"label": "Observed in last 30d",
         "value": (f"{summary['measured']}" if measured_half else "n/a"),
         "help": ("Dependents actually touched in ACCESS_HISTORY in the window. The rest "
                  "are recorded but unqueried here."
                  if measured_half else
                  "Not measured — ACCESS_HISTORY (Enterprise-only) is unavailable on this "
                  "account/role, so this is 'could not measure', not 'measured zero'.")},
        {"label": "Deepest chain", "value": f"{summary['deepest_level']} hop(s)"},
    ])
    if measured_half and not consumers_df.empty:
        view = radius[[c for c in ("FQN", "DOMAIN", "DEPTH", "QUERIES", "USERS", "LAST_TOUCH",
                                   "MEASURED") if c in radius.columns]]
    else:
        view = radius[[c for c in ("FQN", "DOMAIN", "DEPTH") if c in radius.columns]]
    styled_table(view, height=300, sort_label="observed first, then depth")
    if measured_half:
        st.caption(
            f"{summary['dependents']} recorded dependents; "
            f"{summary['measured']} observed touching in {_BLAST_WINDOW_DAYS}d "
            f"({summary['observed_queries']} queries), {summary['unmeasured']} recorded but "
            "not seen. Declared dependents (OBJECT_DEPENDENCIES) miss procs/dynamic SQL; "
            "observed consumers (ACCESS_HISTORY) catch them but lag a few hours. "
            "A count of what depends on this — never a 'safe to ALTER' verdict.")
    else:
        st.caption(
            f"{summary['dependents']} recorded dependents. The OBSERVED half could not be "
            "measured here — ACCESS_HISTORY (Enterprise-only) is unavailable on this "
            "account/role — so whether these dependents are actually queried is UNKNOWN, "
            "not zero. Declared dependents also miss procs/dynamic SQL. A count of what "
            "depends on this — never a 'safe to ALTER' verdict.")


# Watch automation (owner ask 2026-08-17): "when I click Watch, does it do
# something behind the scenes or is it waiting for me? I need that automated."
# Watching used to be a passive bookmark. Now each watched WAREHOUSE is evaluated
# for a cost spike/drop + a health-grade drop and surfaced on the Brief badge and
# the Watchlist tab. Read-only; no external push. 30d window; cross-company (a
# personal watchlist spans companies) so it ignores the page's company filter.
_WATCH_WINDOW_DAYS = 30


def watched_attention(viewer: str, rate: float) -> pd.DataFrame:
    """Per watched entity: cost spike/drop + health-grade status (attention first).

    Cheap and degradation-safe: cost from FACT_WAREHOUSE_DAILY (mart), health from
    the efficiency MART only (probe — never the heavy live sizing scan). An absent
    source degrades to cost-only, or to a steady list, rather than loading a scan.
    Cross-company (company='ALL') so a watched entity is evaluated no matter which
    company filter the viewer is on, and both surfaces share one cache identity."""
    cols = ["ENTITY_TYPE", "ENTITY_KEY", "LABEL", "ATTENTION", "STATUS", "SEVERITY"]
    if not viewer:
        return pd.DataFrame(columns=cols)
    wl = run(workbench_sql.watchlist(viewer), page=_PAGE, key="watch_auto_list",
             tier="live", source="USER_WATCHLIST", probe=True)
    if not wl.usable():
        return pd.DataFrame(columns=cols)
    daily = run(mart_sql.fact_warehouse_daily(_WATCH_WINDOW_DAYS, "ALL"), page=_PAGE,
                key="watch_auto_cost", tier="recent", source="FACT_WAREHOUSE_DAILY", probe=True)
    health = pd.DataFrame()
    prof = run(mart27_sql.eff_sizing_profile(_WATCH_WINDOW_DAYS, "ALL"), page=_PAGE,
               key="watch_auto_health", tier="recent",
               source="MART_WAREHOUSE_EFFICIENCY_DAILY", probe=True)
    if prof.usable():
        sized = size_recommendations(prof.df, rate, served_days(prof, _WATCH_WINDOW_DAYS))
        health = warehouse_health(sized)
    _cal = str(load_settings(_PAGE).get("EXPECTED_SPIKE_CALENDAR") or "")
    return watched_status(wl.df, daily.df if daily.usable() else None, health, rate, calendar=_cal)


def render_watch_badge(viewer: str, rate: float) -> None:
    """Brief in-app badge: how many watched entities moved, and which — the
    proactive half of 'watch'. Quiet (a one-line caption) when nothing moved, so
    the phone-sized Brief stays uncluttered; a warning with a jump only on movement."""
    status = watched_attention(viewer, rate)
    if status.empty:
        return
    summary = watch_summary(status)
    if not summary["attention"]:
        st.caption(f"👁 {summary['watched']} watched "
                   + ("entity is" if summary["watched"] == 1 else "entities are") + " steady.")
        return
    # Preview the MOST SEVERE movers, not the first-added: rank warn > watch > other before
    # truncating, so a real spend spike isn't dropped below soft health-watches added earlier.
    _sev_rank = {"warn": 0, "watch": 1}
    att = (status[status["ATTENTION"]]
           .assign(_o=lambda d: d["SEVERITY"].map(lambda s: _sev_rank.get(str(s), 2)))
           .sort_values("_o", kind="stable").head(4))
    lines = [f"**{r['LABEL']}** — {r['STATUS']}" for _, r in att.iterrows()]
    st.warning("👁 " + str(summary["attention"]) + " of " + str(summary["watched"])
               + " watched " + ("entity has" if summary["attention"] == 1 else "entities have")
               + " moved:\n\n" + "\n\n".join(md_dollars(x) for x in lines))
    if st.button("Open Watchlist", key="brief_watch_jump", type="secondary"):
        st.session_state["_ow_entity_view_pending"] = "Watchlist"
        request_navigation("Control Room", "Entity 360")


def _watchlist_browse_catalog() -> None:
    """F56: next best action for an empty watchlist — land on the Entity sub-tab
    with no key chosen, which renders the catalog browser. Same jump idiom as
    render_watch_badge and the watchlist row-click (request_navigation reruns)."""
    st.session_state["_ow_entity_view_pending"] = "Entity"
    # Clear any lingering key so the Entity sub-tab opens on the catalog table,
    # not a previously viewed entity (pre-render seeding, rec12's own pattern —
    # the Entity widgets are not mounted while the Watchlist sub-tab renders).
    st.session_state["entity_360_key"] = ""
    request_navigation("Control Room", "Entity 360")


def render_watchlist() -> None:
    viewer = viewer_name()
    if not viewer:
        empty_state("needs_setup", "Viewer identity is unavailable, so a personal watchlist cannot be loaded.")
        return
    result = run(
        workbench_sql.watchlist(viewer), page=_PAGE, key="watchlist_all",
        tier="live", source="USER_WATCHLIST",
    )
    if result.ok and result.empty:
        # F56: the empty watchlist is a doorway, not a dead end — offer the
        # catalog browser as the next step (guard's empty branch cannot carry
        # the action params, so the empty case is intercepted here; guard below
        # still owns the error and truncation branches).
        empty_state(
            "no_data_yet", "No entities are on your watchlist yet.",
            hint="Open an entity from the catalog and press Watch to add it here.",
            action_label="Browse the catalog",
            on_action=_watchlist_browse_catalog,
            action_key="es_watchlist_browse",
        )
        return
    if not guard(result, "No entities are on your watchlist yet."):
        return
    frame = result.df.reset_index(drop=True)
    # CR16 (surfacing half): reuse the already-evaluated SLO objectives so a watched
    # entity that has crossed its configured threshold reads as such here, not only
    # in Decision Studio. Probe-gated — degrades to the plain list before V074 or
    # when no objective covers a watched entity. slo_cockpit reads core marts, not
    # ACCOUNT_USAGE, and this is a nested non-first-paint sub-tab.
    slo = run(
        workbench_sql.slo_cockpit(), page=_PAGE, key="watchlist_slo",
        tier="recent", source="SLO_OBJECTIVES + metric marts", probe=True,
    )
    if slo.usable() and not slo.empty:
        annotated = watchlist_threshold_status(frame, slo.df)
        crossed = int(annotated["BREACHING"].sum())
        frame = annotated.drop(columns=["BREACHING"])
        if crossed:
            st.warning(
                f"{crossed} watched "
                + ("entity has" if crossed == 1 else "entities have")
                + " crossed a configured threshold (an SLO objective is in breach)."
            )
    # Watch automation: annotate each watched entity with its cost spike/drop +
    # health-grade status (the "behind the scenes" work), so the list is a live
    # monitor, not a passive bookmark. Merged on the entity key; a STATUS column
    # rides along and an attention banner leads with what moved. Cross-company by
    # design (a personal watchlist spans companies), so no company filter here.
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    status = watched_attention(viewer, rate)
    if not status.empty:
        frame = frame.merge(status[["ENTITY_TYPE", "ENTITY_KEY", "STATUS", "SEVERITY", "ATTENTION"]],
                            on=["ENTITY_TYPE", "ENTITY_KEY"], how="left")
        moved = status[status["ATTENTION"]]
        if not moved.empty:
            names = ", ".join(f"{r['LABEL']} ({r['STATUS']})" for _, r in moved.head(4).iterrows())
            more = "" if len(moved) <= 4 else f", +{len(moved) - 4} more"
            st.warning(md_dollars(
                f"👁 {len(moved)} watched "
                + ("entity has" if len(moved) == 1 else "entities have")
                + f" moved: {names}{more}."))
        frame = frame.drop(columns=["SEVERITY", "ATTENTION"])
    selected = selectable_table(frame, key="watchlist_table", height=260, sort_label="newest watched")
    # BUGFIX (infinite-rerun): navigate ONLY on a CHANGED selection. A sticky
    # st.dataframe selection re-fired request_navigation every rerun; the target
    # (Entity 360 + a context) never no-ops and the nested sub-tab stayed on
    # Watchlist, so it looped forever and forced a disconnect. Mirror the
    # selectable_nav_table / decision_rows seen-guard, and ask the dispatch to switch
    # to the Entity sub-tab (via a non-widget flag) so the click actually lands.
    if selected is None:
        # Fresh mount (the table unmounts when the sub-tab flips to Entity, so
        # every return renders unselected): re-arm the guard, else a click on
        # whatever row now sits at the remembered index is silently swallowed.
        # Same re-arm charts.clickable_bar_usd uses.
        st.session_state.pop("_ow_watchlist_seen", None)
    if selected is not None and selected != st.session_state.get("_ow_watchlist_seen"):
        st.session_state["_ow_watchlist_seen"] = selected
        st.session_state["_ow_entity_view_pending"] = "Entity"
        row = frame.iloc[int(selected)]
        request_navigation(
            "Control Room", "Entity 360",
            context={"entity_type": str(row["ENTITY_TYPE"]), "entity_key": str(row["ENTITY_KEY"])},
        )
