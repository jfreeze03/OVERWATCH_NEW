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
from app.core.state import filters
from app.data import mart_sql
from app.ui import charts
from app.ui.components import guard, kpi_row, page_header, result_caption, styled_table

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

    tab_open, tab_rules, tab_history, tab_native = st.tabs(
        ["Open events", "Rules", "History", "Native delivery"]
    )

    with tab_open:
        if guard(events, "No open alert events — the scan ran and found nothing over threshold.",
                 setup_hint=_SETUP_HINT):
            styled_table(events.df[["RAISED_AT", "SEVERITY", "COMPANY", "TITLE", "STATUS", "ACK_BY"]])
            result_caption(events)
            st.markdown("**Acknowledge / resolve**")
            options = {
                f"[{r['SEVERITY']}] {str(r['TITLE'])[:70]} ({str(r['EVENT_ID'])[:8]})": str(r["EVENT_ID"])
                for _, r in events.df.iterrows()
            }
            chosen = st.selectbox("Event", list(options), key="alert_pick")
            action = st.radio("Action", ["ACK", "RESOLVE"], horizontal=True, key="alert_action")
            note = st.text_input("Note (what was done / why)", key="alert_note", max_chars=500)
            sql_script = _lifecycle_sql(options[chosen], action, note)
            st.code(sql_script, language="sql")
            if is_operator:
                confirm = st.text_input(f"Type {action} to confirm execution", key="alert_confirm")
                if st.button("Execute with audit row", key="alert_exec", disabled=(confirm != action)):
                    ok_all, messages = True, []
                    for stmt in [s for s in sql_script.split(";") if s.strip()]:
                        ok, msg = execute_statement(stmt + ";", page=_PAGE)
                        ok_all = ok_all and ok
                        messages.append(msg)
                    (st.success if ok_all else st.error)(" / ".join(messages))
            else:
                st.caption("Copy and run as OVERWATCH_OPERATOR — in-app execution needs the operator role.")

    with tab_rules:
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

    with tab_history:
        hist = run(mart_sql.alert_event_history(30), page=_PAGE, key="alert_history",
                   tier="recent", source="ALERT_EVENTS")
        if guard(hist, "No alert events in the last 30 days.", setup_hint=_SETUP_HINT):
            charts.events_by_day(hist.df)
            result_caption(hist)

    with tab_native:
        st.markdown(
            "Server-side email delivery uses Snowflake ALERT objects so notifications fire even "
            "when nobody has the app open. Templates ship in the repo and stay suspended until "
            "the notification integration and recipients are approved."
        )
        template_path = Path(__file__).resolve().parents[3] / "snowflake" / "native_alert_templates.sql"
        try:
            st.code(template_path.read_text(encoding="utf-8"), language="sql")
        except OSError:
            st.info("Template file not found in this deployment; see snowflake/native_alert_templates.sql in the repo.")
