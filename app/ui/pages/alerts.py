"""Alerts — rules, open events, ack/resolve with audit, native templates.

Lifecycle writes are approval-shaped: the SQL is always shown; in-app
execution requires the operator profile and writes an ALERT_AUDIT row in the
same action. Rule changes are generate-only by design.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.ai import cortex_complete
from app.core.errors import safe_page
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.core.state import filters, request_navigation
from app.data import insights_sql, mart_sql, recheck_sql
from app.logic import remediation, tuning
from app.logic.ai_prompts import anomaly_explain_prompt
from app.logic.formulas import safe_float
from app.logic.navigate import fix_target, inline_fix_warehouse, investigation_target
from app.logic.playbooks import playbook_for
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    panel_help,
    result_caption,
    selectable_table,
    severity_sort,
    styled_table,
)

_PAGE = "Alerts"
_SETUP_HINT = "Alerting is not installed yet — an admin can verify on Admin → Migrations & freshness."


RESOLUTION_KINDS = ("ACTIONED", "NOISE", "EXPECTED")


def _lifecycle_sql(event_id: str, action: str, note: str, kind: str = "") -> str:
    """ACK/RESOLVE update + audit insert, executed as one script.

    ``kind`` (RESOLVE only, V021): ACTIONED = a real fix followed; NOISE =
    threshold cried wolf; EXPECTED = known/maintenance. Powers the per-rule
    precision score on the Rules section. Pre-V021 deployments: the caller
    retries without the column when Snowflake rejects it.
    """
    action = "RESOLVE" if action == "RESOLVE" else "ACK"
    kind = str(kind or "").upper()
    kind = kind if kind in RESOLUTION_KINDS else ""
    if action == "ACK":
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'ACK', ACK_BY = CURRENT_USER(), "
            f"ACK_AT = CURRENT_TIMESTAMP() WHERE EVENT_ID = {sql_literal(event_id)} AND STATUS = 'OPEN';"
        )
    else:
        set_kind = f", RESOLUTION_KIND = {sql_literal(kind)}" if kind else ""
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'RESOLVED', "
            f"RESOLVED_AT = CURRENT_TIMESTAMP(){set_kind} "
            f"WHERE EVENT_ID = {sql_literal(event_id)} "
            "AND STATUS IN ('OPEN', 'ACK');"
        )
    audit_note = f"[{kind}] {note}" if kind else note
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE) "
        f"VALUES ({sql_literal(event_id)}, {sql_literal(action)}, {sql_literal(audit_note)});"
    )
    return update + "\n" + audit


def _delivery_status() -> None:
    """Answers 'who gets paged at 2am?' in green or red, right on the page."""
    integ = run("SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH_WEBHOOK'", page=_PAGE,
                key="delivery_integ", tier="metadata", source="SHOW INTEGRATIONS", max_rows=0)
    task = run("SHOW TASKS LIKE 'TASK_ALERT_NOTIFY' IN SCHEMA DBA_MAINT_DB.OVERWATCH",
               page=_PAGE, key="delivery_task", tier="metadata", source="SHOW TASKS", max_rows=0)
    last = run(f"SELECT MAX(NOTIFIED_AT) AS LAST_SEND FROM {core_object('ALERT_EVENTS')}",
               page=_PAGE, key="delivery_last", tier="live", source="ALERT_EVENTS")
    has_integ = integ.ok and not integ.empty
    task_state = ""
    if task.ok and not task.empty:
        tdf = task.df.copy()
        tdf.columns = [str(c).lower() for c in tdf.columns]
        if "state" in tdf.columns:
            task_state = str(tdf["state"].iloc[0]).lower()
    last_send = ""
    if last.usable():
        val = last.df.iloc[0].get("LAST_SEND")
        last_send = str(val)[:16] if val is not None and str(val) != "NaT" else ""
    if has_integ and task_state == "started":
        st.success("Delivery LIVE — webhook integration up, notify task chained after the scan"
                   + (f" · last send {last_send}" if last_send else " · no sends yet"))
    elif has_integ:
        st.warning("Integration exists but the notify task is suspended — an admin can "
                   "resume TASK_ALERT_NOTIFY (one statement, see the runbook's delivery "
                   "section). Until then, 2am alerts wait for someone to look.")
    else:
        st.error("No webhook integration — alerts stay in-app only. One-time setup: "
                 "snowflake/webhook_delivery.sql (ACCOUNTADMIN pastes the channel URL).")


@st.fragment
def _open_events_section(events, is_operator: bool) -> None:
    """Fragment: drawer/bulk interactions rerun this section only, not the page."""
    if guard(events, "No open alert events — the scan ran and found nothing over threshold.",
             setup_hint=_SETUP_HINT):
        edf = severity_sort(events.df)  # worst first, newest within — triage order
        if st.toggle("Group by rule (storm view)", key="alert_rollup",
                     help="5 warehouses over budget = 1 row here. Toggle off for drawers."):
            sev_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
            g = edf.copy()
            g["_R"] = g["SEVERITY"].astype(str).map(sev_rank).fillna(0)
            rolled = (g.groupby("RULE_ID")
                       .agg(EVENTS=("EVENT_ID", "count"),
                            WORST=("_R", "max"),
                            NEWEST=("RAISED_AT", "max"),
                            SAMPLE=("TITLE", "first"))
                       .reset_index().sort_values(["WORST", "EVENTS"], ascending=False))
            rev = {v: k for k, v in sev_rank.items()}
            rolled["SEVERITY"] = rolled["WORST"].map(rev).fillna("LOW")
            sel_g = selectable_table(
                rolled[["SEVERITY", "RULE_ID", "EVENTS", "NEWEST", "SAMPLE"]],
                key="alert_rollup_sel", height=280)
            if sel_g is not None:
                rid_pick = str(rolled.iloc[int(sel_g)]["RULE_ID"])
                st.markdown(f"**Events for `{rid_pick}`**")
                styled_table(edf[edf["RULE_ID"].astype(str) == rid_pick]
                             [["RAISED_AT", "SEVERITY", "COMPANY", "TITLE", "STATUS"]], height=240)
            st.caption("Dedupe semantics are untouched — this is a display rollup. "
                       "Toggle off to open a drawer, bulk-ack, or investigate.")
            return
        sel = selectable_table(
            edf[["RAISED_AT", "SEVERITY", "COMPANY", "TITLE", "STATUS", "ACK_BY"]],
            key="alert_events_sel")
        result_caption(events)
        if sel is None:
            st.caption("Click a row to open its drawer: detail, rule, history, playbook, "
                       "one-click investigate, ack/resolve.")
        else:
            row = edf.iloc[sel]
            event_id = str(row["EVENT_ID"])
            st.divider()
            st.markdown(f"**[{row['SEVERITY']}] {row['TITLE']}**")
            st.caption(f"{row['RAISED_AT']} · {row['COMPANY']} · rule {row['RULE_ID']} · "
                       f"event {event_id[:8]} · status {row['STATUS']}")
            detail_text = str(row.get("DETAIL") or "").strip()
            if detail_text:
                # Plain text on purpose: DETAIL originates in Snowflake data;
                # rendering it as markdown let object names inject formatting.
                st.text(detail_text)
            rules_res = run(mart_sql.alert_rules(), page=_PAGE, key="rules_for_drawer",
                            tier="recent", source="ALERT_CONFIG")
            if rules_res.usable():
                rmatch = rules_res.df[rules_res.df["RULE_ID"].astype(str) == str(row["RULE_ID"])]
                if not rmatch.empty:
                    rrow = rmatch.iloc[0]
                    st.caption(f"Rule: {rrow.get('NAME', '')} · family {rrow.get('FAMILY', '')} · "
                               f"threshold {rrow.get('THRESHOLD_NUM', '')} · enabled {rrow.get('ENABLED', '')}")
            with st.expander("Playbook — what to do first", expanded=True):
                st.markdown(playbook_for(str(row["RULE_ID"])))
            _rid = str(row["RULE_ID"]).upper()
            _wh_guess = inline_fix_warehouse(_rid, f"{row['TITLE']} {detail_text}")
            _rc_sql = recheck_sql.recheck_sql(_rid, _wh_guess)
            if _rc_sql and st.button(
                    "Re-check condition now", key=f"recheck_{event_id[:8]}",
                    help="Runs this rule's condition against TODAY's data for "
                         f"{_wh_guess or 'the account'} — is this still true?"):
                rc = run(_rc_sql, page=_PAGE, key=f"recheck_{event_id[:8]}", tier="live",
                         source="live re-check (today)")
                if rc.usable():
                    current_v = safe_float(rc.df.iloc[0].get("CURRENT_VALUE"))
                    thr = None
                    if rules_res.usable():
                        _rm = rules_res.df[rules_res.df["RULE_ID"].astype(str) == str(row["RULE_ID"])]
                        if not _rm.empty:
                            thr = safe_float(_rm.iloc[0].get("THRESHOLD_NUM"))
                    label = recheck_sql.recheck_label(_rid)
                    if thr is not None and thr > 0:
                        if current_v < thr:
                            st.success(f"Condition clear: {label} = {current_v:,.2f} vs "
                                       f"threshold {thr:,.2f} — resolve as ACTIONED below "
                                       "with this as evidence.")
                        else:
                            st.warning(f"Still over: {label} = {current_v:,.2f} vs "
                                       f"threshold {thr:,.2f}.")
                    else:
                        st.info(f"{label}: {current_v:,.2f} (rule threshold unavailable).")
                else:
                    st.info("Re-check unavailable right now: " + (rc.error or "no data today."))
            hist = run(mart_sql.events_for_rule(str(row["RULE_ID"]), 90), page=_PAGE,
                       key=f"hist_rule_{event_id[:8]}", tier="recent",
                       source="ALERT_EVENTS (90d, this rule)")
            if hist.usable() and len(hist.df) > 1:
                with st.expander(f"This rule recently ({len(hist.df)} events)"):
                    styled_table(hist.df, height=220)
            target = investigation_target(str(row["RULE_ID"]),
                                          f"{row['TITLE']} {detail_text}")
            fix = fix_target(str(row["RULE_ID"]), f"{row['TITLE']} {detail_text}")
            wh_inline = inline_fix_warehouse(str(row["RULE_ID"]), f"{row['TITLE']} {detail_text}")
            if wh_inline:
                with st.expander(f"Respond — closed loop on {wh_inline}", expanded=False):
                    st.caption("Playbook above says what; this generates the how. Execute is "
                               "operator-gated, audited to REMEDIATION_LOG, and books an "
                               "ESTIMATED ledger item the monthly verifier proves or rejects.")
                    try:
                        prior = run(mart_sql.ledger_for_event(event_id[:8].lower()), page=_PAGE,
                                    key=f"clf_led_{event_id[:8]}", tier="live",
                                    source="SAVINGS_LEDGER")
                        if prior.ok and not prior.empty:
                            st.markdown("**Loop status — fixes already booked from this event:**")
                            for _, li in prior.df.iterrows():
                                state = str(li.get("STATE") or "")
                                usd = li.get("VERIFIED_USD") if state == "VERIFIED" else li.get("ESTIMATED_USD")
                                try:
                                    usd_s = f"${float(usd):,.0f}"
                                except (TypeError, ValueError):
                                    usd_s = "n/a"
                                st.markdown(f"- **{state}** — {li.get('DESCRIPTION')} ({usd_s})")
                            st.caption("VERIFIED/REJECTED comes from the monthly verifier's "
                                       "before/after actuals — the loop, closed end to end.")
                    except ValueError:
                        pass  # non-uuid event id shapes: chip simply doesn't render
                    fix_kind = st.radio("Fix", ["Tighten auto-suspend to 60s",
                                                "Statement timeout 1h",
                                                "Cap clusters at 1"],
                                        horizontal=True, key=f"clf_kind_{event_id[:8]}")
                    if fix_kind.startswith("Tighten"):
                        stmt_cl = remediation.auto_suspend_fix(wh_inline, 60)
                    elif fix_kind.startswith("Statement"):
                        stmt_cl = remediation.statement_timeout_fix(wh_inline, 3600)
                    else:
                        stmt_cl = remediation.cluster_range_fix(wh_inline, 1, 1)
                    st.code(stmt_cl, language="sql")
                    if is_operator:
                        from app.ui.components import blast_radius
                        blast_radius(wh_inline, _PAGE)
                        conf_cl = st.text_input("Type the warehouse name to confirm",
                                                key=f"clf_confirm_{event_id[:8]}")
                        if st.button("Execute + audit + book estimate", key=f"clf_exec_{event_id[:8]}",
                                     disabled=(conf_cl != wh_inline)):
                            ok, msg = execute_statement(stmt_cl, page=_PAGE)
                            execute_statement(
                                f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                                "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE) "
                                f"SELECT 'ALERT_CLOSED_LOOP', {sql_literal(wh_inline)}, "
                                f"{sql_literal(stmt_cl)}, {sql_literal('EXECUTED' if ok else 'FAILED')}, "
                                f"{sql_literal(('event ' + event_id[:8] + ': ' + msg)[:2000])}",
                                page=_PAGE)
                            if ok:
                                execute_statement(
                                    f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                                    "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES) "
                                    f"SELECT {sql_literal(fix_kind + ' on ' + wh_inline + ' (alert closed loop)')}, "
                                    f"'ESTIMATED', 0, {sql_literal(stmt_cl)}, "
                                    f"{sql_literal('From alert event ' + event_id[:8] + '; verifier measures actuals.')}",
                                    page=_PAGE)
                            notify(ok, msg)
                    else:
                        st.caption("Copy the SQL; executing needs OVERWATCH_OPERATOR.")
                    booked = run(mart_sql.ledger_for_event(event_id[:8]), page=_PAGE,
                                 key=f"clf_led_{event_id[:8]}", tier="live",
                                 source="SAVINGS_LEDGER")
                    if booked.usable():
                        st.markdown("**Savings booked from this alert**")
                        styled_table(booked.df[["DESCRIPTION", "STATE", "ESTIMATED_USD",
                                                "VERIFIED_USD", "CREATED_AT"]], height=140)
                        st.caption("ESTIMATED flips to VERIFIED (or REJECTED) when the monthly "
                                   "verifier compares actual before/after spend — the loop closes here.")
            rid_u = str(row["RULE_ID"]).upper()
            if rid_u.startswith(("COST_", "PERF_")):
                with st.expander("Explain with AI (grounded in the day's evidence)"):
                    import re as _re

                    m_day = _re.search(r"\d{4}-\d{2}-\d{2}", str(row["TITLE"]))
                    event_day = m_day.group(0) if m_day else str(row["RAISED_AT"])[:10]
                    st.caption(f"Evidence window: {event_day} vs its prior 7 days"
                               + (f" · warehouse filter {target['filters'].get('warehouse_contains')}"
                                  if target["filters"].get("warehouse_contains") else ""))
                    if st.button("Assemble evidence + explain", key=f"ai_expl_go_{event_id[:8]}"):
                        ev = run(insights_sql.anomaly_evidence(
                                     event_day, target["filters"].get("warehouse_contains", "")),
                                 page=_PAGE, key=f"ai_ev_{event_day}", tier="historical",
                                 source="ACCOUNT_USAGE.QUERY_HISTORY (evidence pack)")
                        if not ev.ok or ev.empty:
                            st.info("No query-family evidence found for that day/scope — "
                                    "the driver may be serverless or storage rather than queries.")
                        else:
                            settings = load_settings(_PAGE)
                            prompt = anomaly_explain_prompt(
                                str(row["TITLE"]), detail_text, ev.df, None,
                                f"{event_day} vs prior 7d")
                            ok_ai, answer = cortex_complete(
                                prompt, str(settings.get("CORTEX_MODEL") or "llama3.1-8b"),
                                page=_PAGE)
                            if ok_ai:
                                st.session_state[f"_ai_expl_{event_id}"] = answer
                            else:
                                st.error(answer)
                    hypothesis = st.session_state.get(f"_ai_expl_{event_id}", "")
                    if hypothesis:
                        st.markdown(hypothesis)
                        st.caption("Grounded on the evidence rows above only; verify before acting.")
                        if is_operator and st.button("Append hypothesis to the event",
                                                     key=f"ai_expl_save_{event_id[:8]}"):
                            appended = (
                                f"UPDATE {core_object('ALERT_EVENTS')} SET DETAIL = "
                                f"LEFT(COALESCE(DETAIL, '') || ' | AI hypothesis: ' || "
                                f"{sql_literal(hypothesis[:800])}, 2000) "
                                f"WHERE EVENT_ID = {sql_literal(event_id)};"
                            )
                            ok_u, msg_u = execute_statement(appended, page=_PAGE)
                            notify(ok_u, msg_u if not ok_u else "Hypothesis stored on the event.")
            c_inv, c_fix, c_act, c_note = st.columns([1.1, 1.1, 0.9, 1.9])
            with c_fix:
                if fix and st.button("Generate fix →", key="alert_fix", use_container_width=True,
                                     help="Lands on the remediation surface with this event's "
                                          "scope applied — generate, confirm, execute, audited."):
                    request_navigation(fix["page"], fix["section"], fix["filters"])
            with c_inv:
                if st.button("Investigate →", key="alert_investigate", use_container_width=True,
                             help=f"Jump to {target['page']} · {target['section'] or 'top'} "
                                  "with filters applied from this event"):
                    request_navigation(target["page"], target["section"], target["filters"])
            with c_act:
                action = st.radio("Action", ["ACK", "RESOLVE"], horizontal=True, key=f"alert_action_{event_id[:8]}")
            with c_note:
                note = st.text_input("Note (what was done / why)", key=f"alert_note_{event_id[:8]}", max_chars=500)
            kind = ""
            if action == "RESOLVE":
                kind = st.radio(
                    "How was it closed?", RESOLUTION_KINDS, horizontal=True, key=f"alert_kind_{event_id[:8]}",
                    help="ACTIONED = a real fix followed · NOISE = threshold cried wolf · "
                         "EXPECTED = known/maintenance. Feeds the per-rule precision score "
                         "on the Rules section (V021).")
            sql_script = _lifecycle_sql(event_id, action, note, kind)
            with st.expander("SQL that will run"):
                st.code(sql_script, language="sql")
            if is_operator:
                confirm = st.text_input(f"Type {action} to confirm execution", key=f"alert_confirm_{event_id[:8]}")
                if st.button("Execute with audit row", key="alert_exec", disabled=(confirm != action)):
                    ok_all, messages = True, []
                    for stmt in [s for s in sql_script.split(";") if s.strip()]:
                        ok, msg = execute_statement(stmt + ";", page=_PAGE)
                        if not ok and "RESOLUTION_KIND" in msg:
                            # Pre-V021 deployment: retry the legacy statement.
                            legacy = _lifecycle_sql(event_id, action, note)
                            ok, msg = execute_statement(
                                legacy.split(";")[0] + ";", page=_PAGE)
                            msg += (" (resolution kind not stored — an admin can apply the pending schema update on Admin → Migrations & freshness)")
                        ok_all = ok_all and ok
                        messages.append(msg)
                    notify(ok_all, " / ".join(messages))
            else:
                st.caption("Executing requires the OVERWATCH_OPERATOR role; the SQL is copyable for review.")

        if is_operator and len(edf):
            st.divider()
            st.markdown("**Bulk acknowledge / resolve**")
            options = {
                f"[{r['SEVERITY']}] {str(r['TITLE'])[:70]} ({str(r['EVENT_ID'])[:8]})": str(r["EVENT_ID"])
                for _, r in edf.iterrows()
            }
            chosen = st.multiselect("Events", list(options), key="alert_bulk_pick")
            b_action = st.radio("Bulk action", ["ACK", "RESOLVE"], horizontal=True,
                                key="alert_bulk_action")
            b_note = st.text_input("Bulk note (applies to every selected event)",
                                   key="alert_bulk_note", max_chars=500)
            confirm_b = st.text_input(
                f"Type BULK {b_action} to confirm ({len(chosen)} selected)",
                key="alert_bulk_confirm")
            if st.button(f"Execute bulk {b_action}", key="alert_bulk_exec",
                         disabled=(not chosen or confirm_b != f"BULK {b_action}")):
                done = 0
                for label in chosen:
                    script = _lifecycle_sql(options[label], b_action, b_note)
                    ok_one = True
                    for stmt in [s for s in script.split(";") if s.strip()]:
                        ok, _msg = execute_statement(stmt + ";", page=_PAGE)
                        ok_one = ok_one and ok
                    done += int(ok_one)
                notify(done == len(chosen),
                       f"{b_action} applied to {done}/{len(chosen)} event(s); audit rows written.")


@safe_page(_PAGE)
def render() -> None:
    filters()  # keep global scope initialized/consistent
    page_header("Alerts", "Open events, lifecycle with audit, and the rules that raise them.", icon_name="alerts")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES

    events = run(mart_sql.open_alert_events(300), page=_PAGE, key="alert_events",
                 tier="live", source="ALERT_EVENTS")
    if events.ok:
        sev = events.df["SEVERITY"].astype(str).str.upper() if not events.empty else None
        kpi_row([
            {"label": "Open critical", "value": f"{int((sev == 'CRITICAL').sum()) if sev is not None else 0}"},
            {"label": "Open high", "value": f"{int((sev == 'HIGH').sum()) if sev is not None else 0}"},
            {"label": "Open total", "value": f"{len(events.df) if not events.empty else 0}"},
        ])

    _delivery_status()
    section = lazy_sections(["Open events", "Rules", "History", "Native delivery"],
                            key="alerts_section")

    if section == "Open events":
        _open_events_section(events, is_operator)
    elif section == "Rules":
        rules = run(mart_sql.alert_rules(), page=_PAGE, key="alert_rules", tier="recent",
                    source="ALERT_CONFIG")
        if guard(rules, "No alert rules found.", setup_hint=_SETUP_HINT):
            styled_table(rules.df)
            st.caption(
                "Thresholds are data, not code: update ALERT_CONFIG and the next scan uses them. "
                "Statistical anomaly detection runs in-app (Cost > Attribution, Operations > Warehouses) "
                "and is deliberately separate from these deterministic rules."
            )
            st.markdown("**Rule precision (90d)** — is each rule worth its pages?")
            prec = run(mart_sql.rule_precision(90), page=_PAGE, key="rule_precision",
                       tier="recent", source="ALERT_EVENTS.RESOLUTION_KIND")
            if not prec.ok:
                st.info("Precision is not installed yet — an admin can apply the pending schema update on Admin → Migrations & freshness.")
            elif prec.empty:
                st.info("No resolved events in 90d — precision appears once alerts get closed "
                        "with a resolution kind.")
            else:
                pdf_ = prec.df.copy()
                styled_table(pdf_, column_config={
                    "PRECISION_PCT": st.column_config.NumberColumn("Precision %", format="%.1f%%"),
                })
                st.caption(
                    "Precision = ACTIONED / (ACTIONED + NOISE); EXPECTED is excluded. High NOISE "
                    "with low precision = raise the threshold; high UNTAGGED = the score isn't "
                    "trustworthy yet — close events with a kind. Tune via the generator below."
                )
                st.markdown("**Suggested thresholds (from your resolutions)**")
                mk = run(mart_sql.rule_metric_kinds(90), page=_PAGE, key="rule_metric_kinds",
                         tier="recent", source="ALERT_EVENTS metric values by resolution kind")
                if mk.usable():
                    thresholds = {str(r["RULE_ID"]): safe_float(r.get("THRESHOLD_NUM"))
                                  for _, r in rules.df.iterrows()} if not rules.empty else {}
                    sug = tuning.suggestions_by_rule(mk.df, thresholds)
                    if sug.empty:
                        st.caption("No rules have enough tagged resolutions yet.")
                    else:
                        styled_table(sug, height=240)
                        st.caption(
                            "Advice, not automation: suggestions keep ≥90% of ACTIONED alerts "
                            "while cutting NOISE, with the basis stated per rule. Apply through "
                            "the generator below — same review-then-run flow as always."
                        )
                else:
                    st.caption("Suggestions appear once resolved events carry metric values "
                               "and resolution kinds.")
            with st.expander("Generate a threshold change"):
                if not rules.empty:
                    rule_ids = rules.df["RULE_ID"].astype(str).tolist()
                    rule_id = st.selectbox("Rule", rule_ids, key="rule_pick")
                    new_threshold = st.number_input("New threshold", min_value=0.0, step=1.0, key="rule_thresh")
                    enabled = st.checkbox("Enabled", value=True, key="rule_enabled")
                    st.code(
                        f"UPDATE {core_object('ALERT_CONFIG')}\n"
                        f"SET THRESHOLD_NUM = {new_threshold}, ENABLED = {str(bool(enabled)).upper()}, "
                        "UPDATED_AT = CURRENT_TIMESTAMP()\n"
                        f"WHERE RULE_ID = {sql_literal(rule_id)};",
                        language="sql",
                    )
                    st.caption("Rule changes are generate-only: review, then run as OVERWATCH_OPERATOR.")

    elif section == "History":
        hist = run(mart_sql.alert_event_history(30), page=_PAGE, key="alert_history",
                   tier="recent", source="ALERT_EVENTS")
        if guard(hist, "No alert events in the last 30 days.", setup_hint=_SETUP_HINT):
            charts.events_by_day(hist.df)
            result_caption(hist)
        st.markdown("**Response performance (MTTA / MTTR)**")
        mttr = run(mart_sql.alert_mttr(90), page=_PAGE, key="alert_mttr",
                   tier="recent", source="ALERT_EVENTS lifecycle timestamps")
        if mttr.usable():
            df = mttr.df.copy()
            latest = df.dropna(subset=["MTTA_MIN"]).tail(4)
            kpi_row([
                {"label": "MTTA (4-week avg)",
                 "value": f"{latest['MTTA_MIN'].mean():,.0f} min" if not latest.empty else "No acks yet",
                 "help": "Raised -> acknowledged. Improve by working the queue, not the inbox."},
                {"label": "MTTR (4-week avg)",
                 "value": (f"{df.dropna(subset=['MTTR_MIN']).tail(4)['MTTR_MIN'].mean():,.0f} min"
                           if df["MTTR_MIN"].notna().any() else "No resolves yet"),
                 "help": "Raised -> resolved, including remediation time."},
                {"label": "Events (90d)", "value": f"{int(df['EVENTS'].sum()):,}"},
            ])
            styled_table(df, height=240)
        else:
            st.caption("MTTA/MTTR appears once events have been acknowledged/resolved via the lifecycle workflow.")

    else:
        st.markdown("**Routing (family → channel)**")
        panel_help(
            "Routing sends each family/severity through a named notification "
            "integration — COST to #finops, SECURITY to #security. The seeded ALL/HIGH "
            "route keeps the original single-webhook behavior until you add rows. One "
            "failing integration never blocks the others."
        )
        routes = run(mart_sql.alert_routes(), page=_PAGE, key="alert_routes", tier="live",
                     source="ALERT_ROUTES")
        if guard(routes, "No routes configured yet.",
                 setup_hint="Not installed yet — an admin can verify on Admin → Migrations & freshness."):
            st.dataframe(routes.df, hide_index=True, use_container_width=True)
            st.code(
                "-- add a route (operator): send all PIPELINE alerts of MEDIUM+ to #dataeng\n"
                "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)\n"
                "SELECT 'PIPELINE', 'MEDIUM', 'OVERWATCH_WEBHOOK_DATAENG';\n"
                "-- each integration is a Snowflake NOTIFICATION INTEGRATION pointing at one channel's webhook",
                language="sql",
            )
        st.markdown(
            "Server-side email delivery uses Snowflake ALERT objects so notifications fire even "
            "when nobody has the app open. Templates ship in the repo and stay suspended until "
            "the notification integration and recipients are approved."
        )
        for filename, blurb in (
            ("native_alert_templates.sql", "Email via Snowflake ALERT objects"),
            ("webhook_delivery.sql", "Slack / Teams webhook via SYSTEM$SEND_SNOWFLAKE_NOTIFICATION"),
        ):
            st.markdown(f"**{blurb}**")
            template_path = Path(__file__).resolve().parents[3] / "snowflake" / filename
            try:
                st.code(template_path.read_text(encoding="utf-8"), language="sql")
            except OSError:
                st.info(f"File not found in this deployment; see snowflake/{filename} in the repo.")
