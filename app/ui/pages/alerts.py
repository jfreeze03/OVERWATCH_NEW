"""Alerts — rules, open events, ack/resolve with audit, native templates.

Lifecycle writes are approval-shaped: the SQL is always shown; in-app
execution requires the operator profile and writes an ALERT_AUDIT row in the
same action. Rule changes are generate-only by design.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.errors import safe_page
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.core.state import filters, request_navigation
from app.data import mart_sql
from app.logic.navigate import investigation_target
from app.logic.playbooks import playbook_for
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    notify,
    page_header,
    panel_help,
    result_caption,
    selectable_table,
    styled_table,
)

_PAGE = "Alerts"
_SETUP_HINT = "Alert tables come from migration V004; the hourly scan task populates events."


def _lifecycle_sql(event_id: str, action: str, note: str) -> str:
    """ACK/RESOLVE update + audit insert, executed as one script."""
    action = "RESOLVE" if action == "RESOLVE" else "ACK"
    if action == "ACK":
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'ACK', ACK_BY = CURRENT_USER(), "
            f"ACK_AT = CURRENT_TIMESTAMP() WHERE EVENT_ID = {sql_literal(event_id)} AND STATUS = 'OPEN';"
        )
    else:
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'RESOLVED', "
            f"RESOLVED_AT = CURRENT_TIMESTAMP() WHERE EVENT_ID = {sql_literal(event_id)} "
            "AND STATUS IN ('OPEN', 'ACK');"
        )
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE) "
        f"VALUES ({sql_literal(event_id)}, {sql_literal(action)}, {sql_literal(note)});"
    )
    return update + "\n" + audit


@safe_page(_PAGE)
def render() -> None:
    filters()  # keep global scope initialized/consistent
    page_header("Alerts", "Open events, lifecycle with audit, and the rules that raise them.")
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

    section = lazy_sections(["Open events", "Rules", "History", "Native delivery"],
                            key="alerts_section")

    if section == "Open events":
        if guard(events, "No open alert events — the scan ran and found nothing over threshold.",
                 setup_hint=_SETUP_HINT):
            edf = events.df.reset_index(drop=True)
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
                    st.markdown(detail_text)
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
                hist = run(mart_sql.alert_event_history(90), page=_PAGE, key="hist_for_drawer",
                           tier="recent", source="ALERT_EVENTS (90d)")
                if hist.usable():
                    same = hist.df[hist.df["RULE_ID"].astype(str) == str(row["RULE_ID"])].head(10)
                    if len(same) > 1:
                        with st.expander(f"This rule recently ({len(same)} events)"):
                            styled_table(same, height=220)
                target = investigation_target(str(row["RULE_ID"]),
                                              f"{row['TITLE']} {detail_text}")
                c_inv, c_act, c_note = st.columns([1.2, 1.0, 2.0])
                with c_inv:
                    if st.button("Investigate →", key="alert_investigate", use_container_width=True,
                                 help=f"Jump to {target['page']} · {target['section'] or 'top'} "
                                      "with filters applied from this event"):
                        request_navigation(target["page"], target["section"], target["filters"])
                with c_act:
                    action = st.radio("Action", ["ACK", "RESOLVE"], horizontal=True, key="alert_action")
                with c_note:
                    note = st.text_input("Note (what was done / why)", key="alert_note", max_chars=500)
                sql_script = _lifecycle_sql(event_id, action, note)
                with st.expander("SQL that will run"):
                    st.code(sql_script, language="sql")
                if is_operator:
                    confirm = st.text_input(f"Type {action} to confirm execution", key="alert_confirm")
                    if st.button("Execute with audit row", key="alert_exec", disabled=(confirm != action)):
                        ok_all, messages = True, []
                        for stmt in [s for s in sql_script.split(";") if s.strip()]:
                            ok, msg = execute_statement(stmt + ";", page=_PAGE)
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
            "ALERT_ROUTES (V012) sends each family/severity through a named notification "
            "integration — COST to #finops, SECURITY to #security. The seeded ALL/HIGH "
            "route keeps the original single-webhook behavior until you add rows. One "
            "failing integration never blocks the others."
        )
        routes = run(mart_sql.alert_routes(), page=_PAGE, key="alert_routes", tier="live",
                     source="ALERT_ROUTES")
        if guard(routes, "No routes — run V012 to seed the default.",
                 setup_hint="V012 creates ALERT_ROUTES with a default OVERWATCH_WEBHOOK route."):
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
