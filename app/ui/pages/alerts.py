"""Alerts — rules, open events, ack/resolve with audit, native templates.

Lifecycle writes are approval-shaped: the SQL is always shown; in-app
execution requires the operator profile and writes an ALERT_AUDIT row in the
same action. Rule changes are generate-only by design.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from app.config import core_object
from app.core.errors import safe_page
from app.core.identity import idempotency_key, identity_sql, viewer_name
from app.core.query import execute_action, execute_statement, run, run_batch
from app.core.session import is_operator as _is_operator
from app.core.sqlsafe import sql_literal
from app.core.state import filters, navigation_context, request_navigation
from app.data import alert_evidence_sql, mart_sql, recheck_sql
from app.logic import remediation, tuning
from app.logic.ai_prompts import alert_evidence_prompt
from app.logic.alert_evidence import plan_for_alert
from app.logic.formulas import account_now, humanize_age, humanize_duration, md_dollars, safe_float
from app.logic.navigate import fix_target, inline_fix_warehouse, investigation_target
from app.logic.playbooks import playbook_for
from app.logic.verdict import Signal, page_verdict
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    add_to_case_button,
    confirm_gate,
    download_text_button,
    empty_state,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    page_verdict_line,
    panel_help,
    result_caption,
    section_filter_contract,
    selectable_table,
    severity_sort,
    since_last_visit_opener,
    stamp_write,
    styled_table,
    with_user_names,
    write_gate_open,
)

_PAGE = "Alerts"
def _optional_number(value: object, suffix: str = "", decimals: int = 0) -> str:
    """Format a measured ratio without turning NULL evidence into a healthy zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(number):
        return "n/a"
    return f"{number:,.{decimals}f}{suffix}"


_SETUP_HINT = "Alerting is not installed yet — an admin can verify on Admin → Migrations & freshness."


RESOLUTION_KINDS = ("ACTIONED", "NOISE", "EXPECTED")

# V086 per-event snooze: label -> hours. The server computes the wake time from the
# duration (DATEADD in SP_ALERT_SNOOZE), so the app never reasons about the clock.
SNOOZE_PRESETS = {
    "1 hour": 1.0,
    "4 hours": 4.0,
    "1 day": 24.0,
    "3 days": 72.0,
    "1 week": 168.0,
}


def _lifecycle_sql(event_id: str, action: str, note: str, kind: str = "") -> str:
    """Joined display form of _lifecycle_stmts (kept for the SQL preview + tests)."""
    return "\n".join(_lifecycle_stmts(event_id, action, note, kind))


def _lifecycle_stmts(event_id: str, action: str, note: str, kind: str = "") -> list[str]:
    """ACK/RESOLVE update + audit insert as a STRUCTURED statement list.

    codex#9: the legacy fallback must consume this list directly — NEVER re-split a joined
    SQL string on ';', because a semicolon inside the note literal would fracture the audit
    INSERT into malformed fragments.

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
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'ACK', ACK_BY = {identity_sql()}, "
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
    if viewer_name():
        # v4.51 (Codex P1-B): ACTED_BY takes the viewer explicitly — omitting
        # it let the CURRENT_USER() default stamp the app owner under
        # owner's-rights SiS. The note keeps the suffix for prose context.
        audit_note = f"{audit_note} — by {viewer_name()}"
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE, ACTED_BY) "
        f"VALUES ({sql_literal(event_id)}, {sql_literal(action)}, {sql_literal(audit_note)}, {identity_sql()});"
    )
    return [update, audit]


def _snooze_stmts(event_id: str, hours: float, reason: str = "") -> list[str]:
    """Per-event snooze as UPDATE + audit — the SQL preview and the legacy fallback
    for SP_ALERT_SNOOZE (V086). Moves an OPEN/ACK event to STATUS='SNOOZED' with a
    server-computed wake time; the hourly scanner returns it to OPEN when it expires."""
    hours = float(hours)
    reason = str(reason or "")
    audit_note = f"snooze {hours:g}h" + (f" — {reason}" if reason else "")
    if viewer_name():
        audit_note = f"{audit_note} — by {viewer_name()}"
    update = (
        f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'SNOOZED', "
        f"SNOOZED_UNTIL = DATEADD('minute', {round(hours * 60)}, CURRENT_TIMESTAMP()), "
        f"SNOOZE_BY = {identity_sql()}, SNOOZE_REASON = {sql_literal(reason[:1000])} "
        f"WHERE EVENT_ID = {sql_literal(event_id)} AND STATUS IN ('OPEN', 'ACK');"
    )
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE, ACTED_BY) "
        f"VALUES ({sql_literal(event_id)}, 'SNOOZE', {sql_literal(audit_note)}, {identity_sql()});"
    )
    return [update, audit]


def _unsnooze_stmts(event_ids: list[str]) -> list[str]:
    """Early un-snooze (bulk) — the fallback + SQL preview for SP_ALERT_SNOOZE(..., 0, ...).
    Restores each SNOOZED event to its TRUE prior status now (ACK if it was acked, else
    OPEN) — the exact transition the hourly wake step performs — plus an audit row."""
    ids = ", ".join(sql_literal(str(e)) for e in event_ids)
    audit_note = "un-snooze (early)"
    if viewer_name():
        audit_note = f"{audit_note} — by {viewer_name()}"
    # Audit BEFORE the update: the audit SELECTs the STATUS='SNOOZED' rows, so it must
    # run while they still match (the proc audits-before-update for the same reason). The
    # atomic proc is preferred; this legacy pair is only the fallback + SQL preview.
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE, ACTED_BY) "
        f"SELECT EVENT_ID, 'UNSNOOZE', {sql_literal(audit_note)}, {identity_sql()} "
        f"FROM {core_object('ALERT_EVENTS')} WHERE EVENT_ID IN ({ids}) AND STATUS = 'SNOOZED';"
    )
    update = (
        f"UPDATE {core_object('ALERT_EVENTS')} "
        f"SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN'), "
        f"SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL "
        f"WHERE EVENT_ID IN ({ids}) AND STATUS = 'SNOOZED';"
    )
    return [audit, update]


def _bulk_lifecycle_sql(event_ids: list[str], action: str, note: str, kind: str = "") -> tuple[str, str]:
    """r27 #12 (set-based half): ONE UPDATE + ONE audit INSERT for the whole
    selection instead of 2N statements in a loop. True single-transaction
    atomicity needs a stored proc — r28's action layer."""
    action = "RESOLVE" if action == "RESOLVE" else "ACK"
    kind = str(kind or "").upper()
    kind = kind if kind in RESOLUTION_KINDS else ""
    ids = ", ".join(sql_literal(str(e)) for e in event_ids)
    if action == "ACK":
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'ACK', ACK_BY = {identity_sql()}, "
            f"ACK_AT = CURRENT_TIMESTAMP() WHERE EVENT_ID IN ({ids}) AND STATUS = 'OPEN';"
        )
        # R3-5: the UPDATE only transitions OPEN->ACK, but the feed also offers
        # already-ACK events; matching bare STATUS='ACK' would write a false audit row
        # for those no-op events. Scope the audit to events THIS actor just stamped
        # (RESOLVE below needs no such guard — nothing is pre-RESOLVED in the feed).
        state_filter = (f"STATUS = 'ACK' AND ACK_BY = {identity_sql()} "
                        f"AND ACK_AT >= DATEADD('minute', -2, CURRENT_TIMESTAMP())")
    else:
        set_kind = f", RESOLUTION_KIND = {sql_literal(kind)}" if kind else ""
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'RESOLVED', "
            f"RESOLVED_AT = CURRENT_TIMESTAMP(){set_kind} "
            f"WHERE EVENT_ID IN ({ids}) AND STATUS IN ('OPEN', 'ACK');"
        )
        state_filter = "STATUS = 'RESOLVED'"
    audit_note = f"[{kind}] {note}" if kind else note
    if viewer_name():
        audit_note = f"{audit_note} — by {viewer_name()}"
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE, ACTED_BY) "
        f"SELECT EVENT_ID, {sql_literal(action)}, {sql_literal(audit_note)}, {identity_sql()} "
        f"FROM {core_object('ALERT_EVENTS')} WHERE EVENT_ID IN ({ids}) AND {state_filter};"
    )
    return update, audit


def _clear_open_queue_stmts(company: str, action: str, note: str, kind: str = "") -> list[str]:
    """'Clear the open queue' — ack or resolve EVERY open/ack event in the current company scope,
    matched by STATUS + company rather than enumerated EVENT_IDs, so one action clears the whole queue
    past the feed's row cap (e.g. resetting after pre-production validation). Snoozed events are left
    alone. Audit-first (mirrors _unsnooze_stmts): the ALERT_AUDIT insert SELECTs the to-be-transitioned
    rows, then the state UPDATE runs. Same STATUS + company predicate as open_alert_severity_counts so
    it clears exactly the population the KPI tiles show. Operator- and confirm-gated at the call site."""
    action = "RESOLVE" if action == "RESOLVE" else "ACK"
    kind = str(kind or "").upper()
    kind = kind if kind in RESOLUTION_KINDS else ""
    comp = str(company or "ALL").strip()
    scope = "" if comp.upper() == "ALL" else (
        f" AND (COMPANY = {sql_literal(comp)} OR UPPER(COMPANY) = 'ALL')")
    transition = "STATUS = 'OPEN'" if action == "ACK" else "STATUS IN ('OPEN', 'ACK')"
    audit_note = f"[{kind}] {note}" if kind else note
    if viewer_name():
        audit_note = f"{audit_note} — by {viewer_name()}"
    audit = (
        f"INSERT INTO {core_object('ALERT_AUDIT')} (EVENT_ID, ACTION, NOTE, ACTED_BY) "
        f"SELECT EVENT_ID, {sql_literal(action)}, {sql_literal(audit_note)}, {identity_sql()} "
        f"FROM {core_object('ALERT_EVENTS')} WHERE {transition}{scope};"
    )
    if action == "ACK":
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'ACK', ACK_BY = {identity_sql()}, "
            f"ACK_AT = CURRENT_TIMESTAMP() WHERE STATUS = 'OPEN'{scope};"
        )
    else:
        set_kind = f", RESOLUTION_KIND = {sql_literal(kind)}" if kind else ""
        update = (
            f"UPDATE {core_object('ALERT_EVENTS')} SET STATUS = 'RESOLVED', "
            f"RESOLVED_AT = CURRENT_TIMESTAMP(){set_kind} "
            f"WHERE STATUS IN ('OPEN', 'ACK'){scope};"
        )
    return [audit, update]


def _last_delivery_card() -> None:
    """'No alerts today — quiet, or broken?' answered in one card, PER ROUTE.

    The owner hit exactly this on 2026-07-31: a full day of Teams silence with no way
    to tell a healthy quiet stretch from a dead pipe (the app HAD the timestamp, buried
    as a suffix on a banner). Silence alone is not a fault signal — the sender is a
    per-key 24h digest, so a chronic condition raises once and quiet days are legitimately
    empty. What decides it is whether anything is WAITING, so the card reports both.

    The builder now returns one row PER ENABLED ROUTE (Codex #29): a healthy route no
    longer masks a dead sibling and one dead route no longer reddens the whole card. We
    aggregate the WORST route state for the headline and name the offender:
      - FAILING_NOW (#42): latest failure newer than latest success — a later success
        clears the red, so a recovered endpoint does not stay red for 24h.
      - STUCK (#41): eligible backlog but never sent (or silent >=90m) -> BROKEN now,
        instead of the old "queued for next run" when nothing had EVER been delivered.
    """
    res = run(mart_sql.last_delivery_health(), page=_PAGE, key="last_delivery_health",
              tier="live", source="ALERT_DELIVERIES + ALERT_EVENTS/ROUTES + APP_ERROR_LOG")
    if not res.usable():
        return
    df = res.df
    routes_n = int(safe_float(df["ENABLED_ROUTES"].iloc[0])) if "ENABLED_ROUTES" in df else 0
    # Per-route rows only (drop the synthetic zero-route row, whose ROUTE_ID is NULL).
    rdf = df[df["ROUTE_ID"].notna()].copy() if "ROUTE_ID" in df else df.iloc[0:0]

    def _flag(series):
        # Snowflake booleans arrive as bool/np.bool_/NaN; normalize to a bool mask.
        return series.fillna(False).astype(bool)

    # Headline value: newest confirmed send across ANY route (min minutes-since).
    mins_series = rdf["MINUTES_SINCE"].dropna() if not rdf.empty else rdf.get("MINUTES_SINCE")
    mins = safe_float(mins_series.min(), -1) if (mins_series is not None and not mins_series.empty) else -1
    never = rdf.empty or rdf["LAST_SENT_AT"].dropna().empty
    # DISTINCT_ELIGIBLE_NOW is a count of DISTINCT events eligible to send now (same on
    # every row via CROSS JOIN); read it once. Summing per-route ELIGIBLE_NOW would
    # double-count an event that matches several enabled routes.
    waiting = int(safe_float(rdf["DISTINCT_ELIGIBLE_NOW"].iloc[0])) if not rdf.empty else 0
    fails = int(safe_float(rdf["ROUTE_FAILS_24H"].sum())) if not rdf.empty else 0
    failing = rdf[_flag(rdf["FAILING_NOW"])] if not rdf.empty else rdf
    stuck = rdf[_flag(rdf["STUCK"])] if not rdf.empty else rdf

    if never:
        value, sev = "never", "bad"
    else:
        value = f"{humanize_duration(mins, 'min')} ago"
        sev = "ok" if mins < 60 * 48 else "warn"

    def _rlabel(r) -> str:
        integ = str(r.get("INTEGRATION_NAME") or "").strip()
        rid = str(r.get("ROUTE_ID") or "")[:8]
        return f"{integ} ({rid})" if integ else rid

    # The verdict line — this is the part that separates quiet from broken, worst first.
    last_fail_sample = ""
    if routes_n == 0:
        verdict, sev = "No enabled route — nothing can be delivered.", "bad"
    elif not failing.empty:
        r0 = failing.sort_values("CONSEC_FAILS", ascending=False).iloc[0]
        more = f" (+{len(failing) - 1} more route(s) failing)" if len(failing) > 1 else ""
        verdict = (f"Route {_rlabel(r0)} is failing: {int(safe_float(r0.get('CONSEC_FAILS')))} "
                   "send failure(s) since its last success — the endpoint is refusing. "
                   "Check the integration, not the alert rules." + more)
        sev = "bad"
        last_fail_sample = str(r0.get("LAST_FAILURE") or "").strip()
    elif not stuck.empty:
        # #14/#15: rank by TOTAL undelivered backlog (in-window eligible + expired/stranded),
        # not just the in-window count — a route stuck purely on events that aged past the
        # sender's 24h window has ELIGIBLE_NOW = 0 yet is the one that is broken.
        def _backlog(r) -> int:
            return int(safe_float(r.get("ELIGIBLE_NOW"))) + int(safe_float(r.get("EXPIRED_UNDELIVERED")))
        r0 = max((r for _, r in stuck.iterrows()), key=_backlog)
        more = f" (+{len(stuck) - 1} more stuck)" if len(stuck) > 1 else ""
        verdict = (f"Route {_rlabel(r0)} has {_backlog(r0)} undelivered event(s), the oldest "
                   "past the sender's cycle, but nothing has gone out — delivery looks stuck." + more)
        sev = "bad"
    elif waiting > 0:
        verdict = f"{waiting} event(s) queued for the next run."
    else:
        verdict = ("Quiet: nothing is waiting. Alerts are a per-key digest, so a chronic "
                   "condition raises once — no news here really is no news.")

    # Headline last-send delta: the newest route's timestamp, not a global masked one.
    last_at = None
    if not never:
        _sent = rdf["LAST_SENT_AT"].dropna()
        last_at = _sent.max() if not _sent.empty else None

    kpi_row([
        {"label": "Last alert delivery", "value": value, "severity": sev,
         "delta": (str(last_at)[:16] if last_at is not None else "no successful send on record"),
         "delta_color": "off",
         "help": "Newest row in ALERT_DELIVERIES across all routes — a send Snowflake "
                 "confirmed, not a raise. Silence alone is not a fault signal: the sender "
                 "is a 24h-windowed digest and dedupes per key, so a steady account is "
                 "legitimately quiet. The verdict below is the WORST route's state, not a "
                 "global average — one healthy route can't hide a dead sibling."},
        {"label": "Eligible to send now", "value": f"{waiting}",
         "severity": "warn" if not stuck.empty else "",
         "help": "Open events inside the sender's 24h window that match an enabled route "
                 "and are not yet in that route's delivery ledger — mirrors the sender's "
                 "own predicate, summed across routes."},
        {"label": "Send failures (24h)", "value": f"{fails}",
         "severity": "bad" if not failing.empty else "ok",
         "help": "route_send_failed rows: the integration raised. Red only when a route's "
                 "LATEST failure is newer than its latest success (a recovered endpoint "
                 "clears); a dead or unauthorized webhook shows up here, never as silence."},
    ])
    st.caption(md_dollars(verdict))
    if last_fail_sample:
        with st.expander("Last send failure"):
            st.code(last_fail_sample[:400])


def _delivery_status() -> None:
    """Answers 'who gets paged at 2am?' in green or red, right on the page."""
    # FIX (2026-07-31): this probed the hardcoded 'OVERWATCH_WEBHOOK' — the Slack
    # placeholder from webhook_delivery.sql. On a Teams-only account that integration
    # never exists, so the banner claimed "No webhook integration — alerts stay in-app
    # only" while Teams was delivering fine. Resolve the integrations the ENABLED ROUTES
    # actually name, and check for ANY of them.
    integ = run("SHOW NOTIFICATION INTEGRATIONS", page=_PAGE,
                key="delivery_integ", tier="metadata", source="SHOW INTEGRATIONS", max_rows=0)
    task = run("SHOW TASKS LIKE 'TASK_ALERT_NOTIFY' IN SCHEMA DBA_MAINT_DB.OVERWATCH",
               page=_PAGE, key="delivery_task", tier="metadata", source="SHOW TASKS", max_rows=0)
    last = run(f"SELECT MAX(NOTIFIED_AT) AS LAST_SEND FROM {core_object('ALERT_EVENTS')}",
               page=_PAGE, key="delivery_last", tier="live", source="ALERT_EVENTS")
    # Which integrations do the ENABLED routes actually name? That is the set that has
    # to exist — not a hardcoded one. A route pointing at a missing integration is its
    # own (louder) problem, surfaced by the send-failure count on the card above.
    wanted = run(f"SELECT DISTINCT UPPER(INTEGRATION_NAME) AS N FROM {core_object('ALERT_ROUTES')} "
                 "WHERE ENABLED", page=_PAGE, key="delivery_routes_integ", tier="recent",
                 source="ALERT_ROUTES")
    want = ({str(v) for v in wanted.df["N"].dropna()} if wanted.usable() else set())
    have: set[str] = set()
    if integ.ok and not integ.empty:
        _idf = integ.df.copy()
        _idf.columns = [str(c).lower() for c in _idf.columns]
        if "name" in _idf.columns:
            have = {str(v).upper() for v in _idf["name"].dropna()}
    # #32: a FAILED probe is not evidence of absence. If SHOW NOTIFICATION INTEGRATIONS or
    # the enabled-routes read itself errored, we cannot claim "no integration" — say the
    # status is unverifiable and stop, rather than rendering a false "missing" red.
    if not integ.ok or not wanted.ok:
        which = ", ".join(w for w, ok in (("SHOW NOTIFICATION INTEGRATIONS", integ.ok),
                                          ("ALERT_ROUTES", wanted.ok)) if not ok)
        st.warning(f"Delivery status unable to verify — the {which} read failed. This is NOT "
                   "proof alerts are undelivered; retry, or check the warehouse/grants. Treat "
                   "delivery as UNKNOWN until it resolves.")
        return
    # #32: LIVE needs an actual delivery PATH — >=1 ENABLED route whose integration exists.
    # An integration that no enabled route points at delivers nothing, so it is not LIVE.
    if not want:
        st.warning("No enabled alert route — even with an integration present, nothing routes "
                   "out. Enable a route in ALERT_ROUTES (family/severity/company) to begin "
                   "delivering; until then alerts stay in-app only.")
        return
    has_integ = bool(want & have)   # an enabled route AND its integration is present
    missing = sorted(want - have)
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
        st.success("Delivery LIVE — an enabled route's integration is up, notify task chained "
                   "after the scan"
                   + (f" · last send {last_send}" if last_send else " · no sends yet"))
        if missing:
            # A dead route does not stop its siblings (the sender isolates per route),
            # but it fails on every run and buries real errors in the log.
            st.caption(f"Note: {len(missing)} enabled route(s) point at an integration that "
                       f"does not exist ({', '.join(missing)}) — every run logs a failure "
                       "for it, burying real errors. Disable those routes in ALERT_ROUTES "
                       "or create the integration.")
    elif has_integ:
        st.warning("Integration exists but the notify task is suspended — an admin can "
                   "resume TASK_ALERT_NOTIFY (one statement, see the runbook's delivery "
                   "section). Until then, 2am alerts wait for someone to look.")
    else:
        st.error(f"Enabled route(s) point at an integration that does not exist "
                 f"({', '.join(missing)}) — alerts stay in-app only. One-time setup: "
                 "snowflake/webhook_delivery.sql (SNOW_ACCOUNTADMINS pastes the channel URL), "
                 "or repoint the route in ALERT_ROUTES.")


def _stale_rebind(sel: int, event_id: str, bound: object) -> bool:
    """F51: True when a sticky positional selection now points at a DIFFERENT event
    than the one the drawer was bound to — the feed shrank or reordered underneath it
    (a resolve here, the V091 auto-resolver, a filter change). st.dataframe re-emits
    its raw positional index verbatim on every rerun, so after a shift the same index
    silently lands on whatever event slid into the slot. A CHANGED index is a genuine
    new click; the SAME index with a changed event id is the frame moving under the
    selection, and the drawer must not open the wrong event. Pure; never raises."""
    if not isinstance(bound, (tuple, list)) or len(bound) != 2:
        return False
    try:
        bound_idx = int(bound[0])
    except (TypeError, ValueError):
        return False
    return bound_idx == int(sel) and str(bound[1]) != str(event_id)


@st.fragment
def _open_events_section(events, is_operator: bool, company: str = "ALL") -> None:
    """Fragment: drawer/bulk interactions rerun this section only, not the page."""
    # F51: the write receipt / feed-shift notice render FIRST — before the guard and
    # the rollup early-return — so resolving the LAST open event still shows its
    # receipt (above the empty state) and nothing strands in session state to
    # resurface stale against a later, unrelated feed.
    _receipt = st.session_state.pop("_ow_alert_receipt", "")
    if _receipt:
        st.success(_receipt, icon="✅")
    _stale_note = st.session_state.pop("_ow_alert_stale_note", "")
    if _stale_note:
        st.caption(_stale_note)
    # F50: apply a queued decide-bar prefill BEFORE any widget mounts (setting an
    # instantiated widget's key raises) — the re-check's one-click resolve stashes
    # RESOLVE + ACTIONED + the measured evidence here and reruns.
    _prefill = st.session_state.pop("_ow_alert_prefill", None)
    if isinstance(_prefill, dict):
        for _pk, _pv in _prefill.items():
            st.session_state[_pk] = _pv
    # F51: every selection/write widget in this fragment mounts under a nonce-suffixed
    # key so a write can RESET it. Popping session keys is NOT a reset: the frontend
    # keeps values by element id and re-sends them on the next interaction (sticky
    # dataframe highlights, typed confirm text) — a fresh key is the one reliable reset.
    _sel_nonce = int(st.session_state.get("_ow_alert_sel_nonce", 0))
    # Operator-only "clear the whole open queue" — resolve/ack EVERY open event in scope in one action,
    # past the feed's row cap. Deliberately collapsed + typed-confirm + audited; defaults to an UNTAGGED
    # resolve so a start-fresh clear (e.g. after pre-production validation) does not skew per-rule
    # precision. Renders before the feed guard so it works even when the queue exceeds the feed cap.
    if is_operator:
        _clr = run(mart_sql.open_alert_severity_counts(company), page=_PAGE,
                   key=f"alert_clear_counts_{company}", tier="live",
                   source="ALERT_EVENTS (open counts, scope)", probe=True)
        _open_total = int(safe_float(_clr.df.iloc[0].get("TOTAL"))) if _clr.usable() else 0
        if _open_total > 0:
            _scope_lbl = ("all companies" if str(company).strip().upper() == "ALL"
                          else f"{company} + account-level")
            with st.expander(f"🧹 Clear the open queue — {_open_total:,} open in scope ({_scope_lbl})"):
                st.caption("Bulk-transition EVERY open/acknowledged event in this scope in one action — "
                           "past the feed's row cap. Snoozed events are left alone. Every change is "
                           "written to ALERT_AUDIT.")
                _cr = _clr.df.iloc[0]
                _mix = " · ".join(
                    f"{int(safe_float(_cr.get(_k))):,} {_lbl}"
                    for _k, _lbl in (("CRIT", "CRITICAL"), ("HIGH", "HIGH"),
                                     ("MED", "MEDIUM"), ("LOW", "LOW"))
                    if int(safe_float(_cr.get(_k))) > 0)
                if _mix:
                    st.caption("In scope: " + _mix)
                _c_choice = st.radio(
                    "Action", ["Resolve (clear the queue)", "Acknowledge (mark seen, keeps the count)"],
                    key=f"alert_clear_action_{company}",
                    help="Resolve zeroes the open counts; Acknowledge only marks them seen — an ACK "
                         "event still counts as open in the tiles.")
                _c_resolve = _c_choice.startswith("Resolve")
                _c_kind = ""
                if _c_resolve:
                    _c_pick = st.radio(
                        "Resolution tag", ["Untagged — excluded from precision", *RESOLUTION_KINDS],
                        key=f"alert_clear_kind_{company}",
                        help="Untagged is right for a setup / validation clear: it drops out of the "
                             "per-rule precision score, so test alerts don't skew it.")
                    _c_kind = _c_pick if _c_pick in RESOLUTION_KINDS else ""
                _c_note = st.text_input(
                    "Note (written to every audit row)", value="Bulk cleared — pre-production validation",
                    key=f"alert_clear_note_{company}", max_chars=500)
                _c_verb = "RESOLVE" if _c_resolve else "ACK"
                if (confirm_gate(
                        f"CLEAR {_c_verb}", f"Clear the open queue ({_c_verb})",
                        key=f"alert_clear_exec_{company}",
                        prompt=f"Type CLEAR {_c_verb} to confirm ({_open_total:,} event(s) in scope)",
                        type="primary")
                        and write_gate_open(f"alert_clear_exec_{company}")):
                    _c_ok = True
                    for _c_stmt in _clear_open_queue_stmts(company, _c_verb, _c_note, _c_kind):
                        _c_o, _c_m = execute_statement(_c_stmt, page=_PAGE)
                        _c_ok = _c_ok and _c_o
                        if not _c_o:
                            break
                    stamp_write(f"alert_clear_exec_{company}", _c_ok)
                    notify(_c_ok, (f"Cleared the open queue — {_open_total:,} event(s) {_c_verb}."
                                   if _c_ok else "Clear failed — see the error log."))
                    if _c_ok:
                        from app.ui.components import log_ui_event
                        log_ui_event("alert_resolve" if _c_resolve else "alert_ack", page=_PAGE)
                        st.session_state["_ow_alert_receipt"] = (
                            f"Open queue cleared — {_open_total:,} event(s) {_c_verb}")
                        st.rerun()
    if guard(events, "No open alert events — the scan ran and found nothing over threshold.",
             setup_hint=_SETUP_HINT, kind="clean"):
        edf = severity_sort(events.df)  # worst first, newest within — triage order
        requested_event = str(navigation_context().get("event_id") or "").strip()
        # C44: a RESOLVE/SNOOZE queues the NEXT open event by IDENTITY; it rides
        # the deep-link machinery below (armed per arrival, disarmed by the next
        # write's nonce bump), so triage keeps momentum with zero positional risk.
        _next_up = str(st.session_state.get("_ow_alert_next_up") or "")
        if not requested_event and _next_up:
            requested_event = _next_up
            # review fix: the nonce folds the ARRIVAL EPOCH into the signature —
            # re-queuing the same id after a write must read as a NEW arrival
            # (the applied-gate only re-arms on signature CHANGE).
            event_signature = f"alert-next:{_next_up}:{_sel_nonce}"
        else:
            if _next_up:
                # review fix: a real deep-link supersedes the queue — the miss
                # is final, never a surprise drawer later.
                st.session_state.pop("_ow_alert_next_up", None)
            event_signature = f"alert:{requested_event}"
        if (requested_event
                and st.session_state.get("_ow_alert_context_applied") != event_signature):
            st.session_state["alert_rollup"] = False
            st.session_state["_ow_alert_context_applied"] = event_signature
            # F51: a NEW deep-link arrival re-arms the identity fallback below.
            st.session_state.pop("_ow_alert_deeplink_armed", None)
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
            # F51: the rollup selection is positional too — same nonce key + identity
            # binding as the drawer, so a storm collapsing under the selection can't
            # silently relabel the expanded rule.
            sel_g = selectable_table(
                rolled[["SEVERITY", "RULE_ID", "EVENTS", "NEWEST", "SAMPLE"]],
                key=f"alert_rollup_sel_{_sel_nonce}", height=280)
            if sel_g is not None and 0 <= int(sel_g) < len(rolled):
                rid_pick = str(rolled.iloc[int(sel_g)]["RULE_ID"])
                if _stale_rebind(int(sel_g), rid_pick,
                                 st.session_state.get("_ow_alert_rollup_bind")):
                    st.session_state["_ow_alert_sel_nonce"] = _sel_nonce + 1
                    st.session_state.pop("_ow_alert_rollup_bind", None)
                    st.session_state["_ow_alert_stale_note"] = (
                        "The feed changed under the previous selection — click a row "
                        "to reopen it.")
                    st.rerun()
                st.session_state["_ow_alert_rollup_bind"] = (int(sel_g), rid_pick)
                st.markdown(f"**Events for `{rid_pick}`**")
                styled_table(edf[edf["RULE_ID"].astype(str) == rid_pick]
                             [["RAISED_AT", "SEVERITY", "COMPANY", "TITLE", "STATUS"]], height=240)
            else:
                st.session_state.pop("_ow_alert_rollup_bind", None)
            st.caption("Dedupe semantics are untouched — this is a display rollup. "
                       "Toggle off to open a drawer, bulk-ack, or investigate.")
            # F51: the selection widgets unmount on this path — their binds must
            # not outlive the selections they mirror.
            st.session_state.pop("_ow_alert_drawer_bind", None)
            st.session_state.pop("_ow_alert_bulk_bind", None)
            return
        # rec27: an "Age" companion ("3h ago") reads at a glance; RAISED_AT stays the
        # real, sortable, tz-converted column. rec30: declare the severity-then-newest order.
        # C42: feed LEFT, the selected drawer RIGHT (a desktop console layout;
        # the columns restack on narrow viewports via the shared st-key-ow_md_*
        # CSS). Pure render-location split — every F51/C44/C48 guard is session
        # state and layout-agnostic; sel/_bulk_rows are fragment locals in scope
        # across both columns, and the bulk panel + snoozed tray stay full-width.
        with st.container(key="ow_md_alerts"):
            col_feed, col_drawer = st.columns(
                (1.15, 1.0), gap="large", vertical_alignment="top")
            with col_feed:
                _now = account_now()
                _feed = with_user_names(edf, _PAGE, user_col="ACK_BY", display_col="Ack by")
                _feed = _feed.assign(AGE=_feed["RAISED_AT"].map(lambda t: humanize_age(t, _now)))
                # C43: operator-gated BULK MODE — off (the default) keeps the one-click
                # single-row drawer; on, the table takes checkbox multi-selection that arms
                # the bulk panel below (replacing the old duplicate multiselect lookup).
                # The mode suffix on the key mounts a fresh table on every flip so selection
                # state can never leak across modes; non-operators never see the toggle, so
                # bulk is never promised to a viewer who cannot execute it.
                _bulk_mode = bool(is_operator and st.toggle(
                    "Bulk select", key=f"alert_bulk_mode_{_sel_nonce}",
                    help="Check several rows and acknowledge/resolve them together; "
                         "switch off for single-click drawer triage."))
                _raw_sel = selectable_table(
                    _feed[["RAISED_AT", "AGE", "SEVERITY", "COMPANY", "TITLE", "STATUS", "Ack by", "ACK_BY"]],
                    key=f"alert_events_sel_{_sel_nonce}_{'m' if _bulk_mode else 's'}",
                    sort_label="severity, then newest", multi=_bulk_mode)
                if _bulk_mode:
                    _bulk_rows = [int(r) for r in (_raw_sel or [])]
                    sel = None
                else:
                    _bulk_rows = []
                    sel = int(_raw_sel) if _raw_sel is not None else None
                # C43+F51: the bulk SET binds by identity too — a feed shift under checked
                # rows (same positions, different events) disarms the panel instead of
                # silently re-targeting the write the operator reviewed and typed to confirm.
                if _bulk_rows:
                    _ids_here = tuple(str(edf.iloc[i]["EVENT_ID"]) for i in _bulk_rows)
                    _bbind = st.session_state.get("_ow_alert_bulk_bind")
                    if (isinstance(_bbind, tuple) and len(_bbind) == 2
                            and tuple(_bbind[0]) == tuple(_bulk_rows)
                            and tuple(_bbind[1]) != _ids_here):
                        st.session_state["_ow_alert_sel_nonce"] = _sel_nonce + 1
                        st.session_state.pop("_ow_alert_bulk_bind", None)
                        st.session_state["_ow_alert_stale_note"] = (
                            "The feed changed under the previous selection — re-select rows "
                            "to arm a bulk action.")
                        st.rerun()
                    st.session_state["_ow_alert_bulk_bind"] = (tuple(_bulk_rows), _ids_here)
                else:
                    st.session_state.pop("_ow_alert_bulk_bind", None)
                _from_deeplink = False
                if sel is None and not _bulk_rows and requested_event:
                    # F51: the identity fallback is armed per ARRIVAL and disarmed by the next
                    # write's nonce bump — un-gated it re-fired on every paint, reopening the
                    # acted drawer after each write. While armed it re-applies on every
                    # fragment rerun (the table emits no selection while the user works in
                    # the drawer), which is exactly what keeps the deep-linked drawer open.
                    _armed = st.session_state.get("_ow_alert_deeplink_armed")
                    _is_next = event_signature.startswith("alert-next:")
                    if _is_next and _bulk_mode:
                        # C44 review fix: bulk mode supersedes the queue — a funneled
                        # drawer must never render beneath the checkbox table.
                        st.session_state.pop("_ow_alert_next_up", None)
                        st.session_state.pop("_ow_alert_deeplink_armed", None)
                    elif _armed is None or _armed == (event_signature, _sel_nonce):
                        matches = [
                            index for index, value in enumerate(edf["EVENT_ID"].astype(str))
                            if value == requested_event
                        ]
                        sel = matches[0] if matches else None
                        if sel is not None:
                            _from_deeplink = True
                            if _armed is None:
                                st.session_state["_ow_alert_deeplink_armed"] = (
                                    event_signature, _sel_nonce)
                        elif _is_next:
                            # C44 review fix: the queued event left the feed (colleague
                            # write / V091 auto-resolver) or is filtered out — the miss
                            # is FINAL, never a zombie drawer when the id returns.
                            st.session_state.pop("_ow_alert_next_up", None)
                            st.session_state.pop("_ow_alert_deeplink_armed", None)
                # F51: bind the drawer by EVENT IDENTITY, not position. When the live feed
                # shrinks or reorders under a sticky selection (a resolve here, the V091
                # auto-resolver, a filter change), the same positional index silently lands
                # on a DIFFERENT event — drop the rebind instead of opening the wrong drawer.
                if sel is not None and 0 <= int(sel) < len(edf):
                    _ev_here = str(edf.iloc[int(sel)]["EVENT_ID"])
                    # An identity-derived deep-link selection can never be a stale POSITIONAL
                    # rebind — only the widget's sticky index gets the guard.
                    if not _from_deeplink and _stale_rebind(
                            int(sel), _ev_here, st.session_state.get("_ow_alert_drawer_bind")):
                        st.session_state["_ow_alert_sel_nonce"] = _sel_nonce + 1
                        st.session_state.pop("_ow_alert_drawer_bind", None)
                        st.session_state["_ow_alert_stale_note"] = (
                            "The feed changed under the previous selection — click a row "
                            "to reopen its drawer.")
                        # Rerun NOW so the fresh, unselected table mounts in this same
                        # interaction — without it the wrong-row highlight survives the paint
                        # and the retired key swallows the user's next click.
                        st.rerun()
                    st.session_state["_ow_alert_drawer_bind"] = (int(sel), _ev_here)
                else:
                    # F51: no live selection -> no bind may outlive it (an orphaned bind
                    # would falsely veto a later genuine click that lands on the same index).
                    st.session_state.pop("_ow_alert_drawer_bind", None)
                result_caption(events)
                if sel is None or not (0 <= int(sel) < len(edf)):
                    # C42: in the master-detail layout the DRAWER pane (right) owns
                    # the "select an event to open its drawer" prompt, so the feed
                    # keeps only the bulk-relevant lines and the single-mode tip.
                    if _bulk_rows:
                        st.caption(f"{len(_bulk_rows)} row(s) checked — the bulk action panel is below.")
                    elif _bulk_mode:
                        st.caption("Bulk select is on — check rows to arm a bulk acknowledge/resolve.")
                    elif is_operator:
                        st.caption("Flip Bulk select for multi-row actions.")
            with col_drawer:
                if sel is not None and 0 <= int(sel) < len(edf):
                    row = edf.iloc[sel]
                    event_id = str(row["EVENT_ID"])
                    # C42: no leading divider — the column edge separates the feed
                    # from the drawer now (the old rule was a stacked-flow separator).
                    st.markdown(f"**[{row['SEVERITY']}] {row['TITLE']}**")
                    st.caption(f"{row['RAISED_AT']} · {row['COMPANY']} · rule {row['RULE_ID']} · "
                               f"event {event_id[:8]} · status {row['STATUS']}")
                    detail_text = str(row.get("DETAIL") or "").strip()
                    if detail_text:
                        # Plain text on purpose: DETAIL originates in Snowflake data;
                        # rendering it as markdown let object names inject formatting.
                        st.text(detail_text)
                    add_to_case_button(
                        # Scope the preview to the SELECTED alert, not the whole feed, so the
                        # captured evidence matches the item's title/summary.
                        "Alerts", replace(events, df=edf.iloc[[sel]]),
                        title=f"[{row['SEVERITY']}] {row['TITLE']}",
                        summary=(detail_text[:200] if detail_text
                                 else f"{row['SEVERITY']} alert on rule {row['RULE_ID']}"),
                        next_action=f"Ack/resolve or investigate rule {row['RULE_ID']} (status {row['STATUS']}).",
                        key=f"ow_case_add_alert_{event_id[:8]}")
                    # F57: the drawer's four supporting reads (rule config, deliveries,
                    # rule history, prior resolutions) submit as ONE async batch instead
                    # of four serial round-trips — each row click used to stall in four
                    # visible steps. The spinner names the wait on a cold open; cache
                    # hits render instantly.
                    with st.spinner("Assembling event context…"):
                        _dr = run_batch([
                            {"key": "rules", "sql": mart_sql.alert_rules(),
                             "source": "ALERT_CONFIG"},
                            {"key": "deliv", "sql": mart_sql.deliveries_for_event(event_id),
                             "source": "ALERT_DELIVERIES + ALERT_ROUTES"},
                            {"key": "hist", "sql": mart_sql.events_for_rule(str(row["RULE_ID"]), 90),
                             "source": "ALERT_EVENTS (90d, this rule)"},
                            {"key": "res", "sql": mart_sql.resolutions_for_rule(str(row["RULE_ID"])),
                             "source": "ALERT_EVENTS (resolved, this rule)"},
                        ], page=_PAGE, tier="recent") or {}
                    rules_res = _dr.get("rules") or run(
                        mart_sql.alert_rules(), page=_PAGE, key="rules_for_drawer",
                        tier="recent", source="ALERT_CONFIG")
                    if rules_res.usable():
                        rmatch = rules_res.df[rules_res.df["RULE_ID"].astype(str) == str(row["RULE_ID"])]
                        if not rmatch.empty:
                            rrow = rmatch.iloc[0]
                            st.caption(f"Rule: {rrow.get('NAME', '')} · family {rrow.get('FAMILY', '')} · "
                                       f"threshold {rrow.get('THRESHOLD_NUM', '')} · enabled {rrow.get('ENABLED', '')}")
                    # rec38: did THIS event reach anyone? ALERT_DELIVERIES keys per event+route,
                    # so the drawer can answer directly instead of only History's aggregate SLO.
                    _deliv = _dr.get("deliv") or run(
                        mart_sql.deliveries_for_event(event_id), page=_PAGE,
                        key=f"alert_deliv_{event_id}", tier="recent",
                        source="ALERT_DELIVERIES + ALERT_ROUTES")
                    if _deliv.ok and not _deliv.empty:
                        st.caption("Delivered — " + " · ".join(
                            f"{r['INTEGRATION_NAME']} at {r['SENT_AT']}" for _, r in _deliv.df.iterrows()))
                    elif _deliv.ok:
                        # Neutral wording: an empty result is normal for a severity/family no
                        # enabled route matches, not necessarily a delivery miss.
                        st.caption("No delivery recorded (a route may not match this event's "
                                   "severity or family).")
                    # F49: the DECIDE bar leads the drawer — the operator's verb (ack/resolve/
                    # snooze + investigate/fix) was the LAST thing rendered, below six
                    # evidence panels, so every triage required scrolling the whole drawer.
                    # The evidence panels follow, demoted into a supporting group.
                    target = investigation_target(str(row["RULE_ID"]),
                                                  f"{row['TITLE']} {detail_text}")
                    fix = fix_target(str(row["RULE_ID"]), f"{row['TITLE']} {detail_text}")
                    wh_inline = inline_fix_warehouse(str(row["RULE_ID"]), f"{row['TITLE']} {detail_text}")
                    # rec18: two rows — nav buttons + action radio share one row; the note
                    # gets a full-width row of its own instead of a cramped ~30% column.
                    c_inv, c_fix, c_act = st.columns([1.1, 1.1, 0.9])
                    with c_inv:
                        if st.button("Investigate →", key="alert_investigate", width="stretch",
                                     help=f"Jump to {target['page']} · {target['section'] or 'top'} "
                                          "with filters applied from this event"):
                            # rec24: only claim "filters applied" when the jump actually reshapes
                            # them, so the arrival note on the destination is never misleading.
                            _inv_ctx = None
                            if target["filters"]:
                                _inv_ctx = {"filter_note": (f"Filters applied from alert "
                                                            f"[{row['RULE_ID']}]: {str(row['TITLE'])[:80]}")}
                            request_navigation(target["page"], target["section"], target["filters"], _inv_ctx)
                    with c_fix:
                        if fix and st.button("Generate fix →", key="alert_fix", width="stretch",
                                             help="Lands on the remediation surface with this event's "
                                                  "scope applied — generate, confirm, execute, audited."):
                            request_navigation(fix["page"], fix["section"], fix["filters"])
                    with c_act:
                        action = st.radio("Action", ["ACK", "RESOLVE", "SNOOZE"], horizontal=True,
                                          key=f"alert_action_{event_id[:8]}_{_sel_nonce}")
                    note = st.text_input("Note (what was done / why)", key=f"alert_note_{event_id[:8]}_{_sel_nonce}", max_chars=500)
                    kind = ""
                    snooze_hours = 0.0
                    if action == "RESOLVE":
                        kind = st.radio(
                            "How was it closed?", RESOLUTION_KINDS, horizontal=True, key=f"alert_kind_{event_id[:8]}_{_sel_nonce}",
                            help="ACTIONED = a real fix followed · NOISE = threshold cried wolf · "
                                 "EXPECTED = known/maintenance. Feeds the per-rule precision score "
                                 "on the Rules section.")
                    elif action == "SNOOZE":
                        snooze_hours = SNOOZE_PRESETS[st.selectbox(
                            "Snooze for", list(SNOOZE_PRESETS), key=f"alert_snooze_{event_id[:8]}_{_sel_nonce}",
                            help="The event leaves the triage feed now and returns to it automatically "
                                 "when the timer expires — no ack, no resolve. The underlying rule keeps "
                                 "firing for other entities.")]
                        try:
                            _wake = account_now() + timedelta(hours=float(snooze_hours))
                            # review fix: past a day out "%a %H:%M" misleads — a 1-week
                            # snooze lands on the SAME weekday, reading as later today.
                            _wfmt = "%a %H:%M" if float(snooze_hours) <= 24 else "%a %b %d, %H:%M"
                            st.caption(f"→ wakes ~{_wake.strftime(_wfmt)} account time, "
                                       "returning to this feed automatically.")
                        except (TypeError, ValueError):  # F52: the timer caption is cosmetic
                            pass
                    if action == "SNOOZE":
                        stmts = _snooze_stmts(event_id, snooze_hours, note)
                    else:
                        stmts = _lifecycle_stmts(event_id, action, note, kind)
                    with st.container(border=True):
                        with st.expander("SQL that will run"):
                            st.code("\n".join(stmts), language="sql")
                        if is_operator:
                            # rec14 write-friction policy: ACK is a reversible OPEN->ACK move on
                            # OVERWATCH's own audit trail, so it is ONE CLICK (the SQL preview
                            # above stays visible). RESOLVE classifies the event — it feeds the
                            # per-rule precision score — so it is the consequential write and
                            # keeps the type-to-confirm gate. rec42: key PER EVENT so neither the
                            # confirm field nor the button state carries across events.
                            # ACK (reversible OPEN->ACK) and SNOOZE (auto-reverses on wake) are
                            # one click; RESOLVE classifies the event, so it keeps the confirm gate.
                            if action in ("ACK", "SNOOZE"):
                                _fire = st.button("Execute with audit row", type="primary",
                                                  width="stretch",
                                                  key=f"alert_exec_ack_{event_id[:8]}_{_sel_nonce}")
                            else:
                                _fire = confirm_gate(action, "Execute with audit row",
                                                     key=f"alert_exec_{event_id[:8]}_{_sel_nonce}",
                                                     prompt=f"Type {action} to confirm execution")
                            if _fire and write_gate_open(f"alert_exec_{event_id[:8]}_{action}"):
                                # V051/V086: one atomic proc (update + audit in a transaction);
                                # the legacy path is the split script, unchanged.
                                if action == "SNOOZE":
                                    call = (f"CALL {core_object('SP_ALERT_SNOOZE')}("
                                            f"{sql_literal(event_id)}, {snooze_hours}, {sql_literal(note)}, "
                                            f"{identity_sql()}, "
                                            f"{sql_literal(idempotency_key('ALERT_SNOOZE', event_id))})")
                                else:
                                    call = (f"CALL {core_object('SP_ALERT_LIFECYCLE')}("
                                            f"{sql_literal(event_id)}, {sql_literal(action)}, {sql_literal(note)}, "
                                            f"{sql_literal(kind)}, {identity_sql()}, "
                                            f"{sql_literal(idempotency_key('ALERT_' + action, event_id))})")
                                # codex#9: pass the structured statement list straight through — a ';'
                                # inside the note no longer fractures the legacy fallback.
                                ok, msg = execute_action(call, stmts, page=_PAGE)
                                stamp_write(f"alert_exec_{event_id[:8]}_{action}", ok)  # C48
                                notify(ok, msg)
                                if ok:
                                    from app.ui.components import log_ui_event
                                    _ev = {"RESOLVE": "alert_resolve", "SNOOZE": "alert_snooze"}.get(action, "alert_ack")
                                    log_ui_event(_ev, page=_PAGE)
                                    # F51 post-write hygiene. RESOLVE/SNOOZE remove the event
                                    # from the feed, so the selection resets via the nonce bump
                                    # (which remounts every nonce-keyed widget fresh — session-
                                    # key pops don't survive the frontend's state resend). ACK
                                    # keeps the event at its index, so the drawer STAYS OPEN to
                                    # continue triage (ack -> investigate -> resolve); the
                                    # identity guard protects it if the feed shifts anyway. The
                                    # full rerun is notify()'s own contract: the re-read feed is
                                    # the durable receipt.
                                    _nxt_label = ""
                                    if action in ("RESOLVE", "SNOOZE"):
                                        st.session_state["_ow_alert_sel_nonce"] = _sel_nonce + 1
                                        st.session_state.pop("_ow_alert_drawer_bind", None)
                                        st.session_state.pop("_ow_alert_bulk_bind", None)
                                        # F50 review fix: the verdict dies with the triage
                                        # interaction — a snooze that wakes must not
                                        # resurface a stale CLEAR with a live one-click.
                                        st.session_state.pop(f"_ow_recheck_{event_id[:8]}", None)
                                        # C44 review fix (HIGH): consume the spent drill
                                        # identity — navigation_context() is read WITHOUT
                                        # consume and nothing else ever clears it, so a
                                        # lingering event_id would shadow the queued
                                        # next-up on every later run (dead advance, lying
                                        # receipt). ACK keeps it: the drawer stays open.
                                        _nav = st.session_state.get("_ow_nav_context")
                                        if isinstance(_nav, dict) and _nav.get("event_id"):
                                            st.session_state["_ow_nav_context"] = {
                                                k: v for k, v in _nav.items() if k != "event_id"}
                                        # C44: queue the next open event (identity, not
                                        # position) so the queue advances instead of
                                        # landing back on an unselected feed.
                                        if int(sel) + 1 < len(edf):
                                            _nrow = edf.iloc[int(sel) + 1]
                                            st.session_state["_ow_alert_next_up"] = str(_nrow["EVENT_ID"])
                                            _nxt_label = (f" — next: [{_nrow['SEVERITY']}] "
                                                          f"{str(_nrow['TITLE'])[:60]}")
                                        else:
                                            st.session_state.pop("_ow_alert_next_up", None)
                                    st.session_state["_ow_alert_receipt"] = (
                                        f"{action} recorded — [{row['SEVERITY']}] "
                                        f"{str(row['TITLE'])[:80]} (event {event_id[:8]})" + _nxt_label)
                                    st.rerun()
                        else:
                            st.caption("Executing requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS; the SQL is copyable for review.")
                    st.markdown("**Supporting evidence**")
                    with st.expander("Playbook — what to do first", expanded=False):
                        st.markdown(playbook_for(str(row["RULE_ID"])))
                    _rid = str(row["RULE_ID"]).upper()
                    _wh_guess = inline_fix_warehouse(_rid, f"{row['TITLE']} {detail_text}")
                    _rc_sql = recheck_sql.recheck_sql(_rid, _wh_guess, str(row.get("COMPANY", "")))
                    _rc_key = f"_ow_recheck_{event_id[:8]}"
                    if _rc_sql and st.button(
                            "Re-check condition now", key=f"recheck_{event_id[:8]}",
                            help="Runs this rule's condition against TODAY's data for "
                                 f"{_wh_guess or 'the account'} — is this still true?"):
                        rc = run(_rc_sql, page=_PAGE, key=f"recheck_{event_id[:8]}", tier="live",
                                 source="live re-check (today)")
                        if rc.usable():
                            # review fix: NULL (idle warehouse, zero-row window) must not
                            # coerce to a fabricated "clear 0.00" that the one-click then
                            # writes into the audit note — route it to the error branch.
                            current_v = safe_float(rc.df.iloc[0].get("CURRENT_VALUE"),
                                                   default=float("nan"))
                            if math.isnan(current_v):
                                st.session_state[_rc_key] = {
                                    "error": "no data today — condition not evaluable "
                                             "for this target (idle/suspended?).",
                                    "at": account_now().strftime("%H:%M"),
                                }
                            else:
                                thr = None
                                if rules_res.usable():
                                    _rm = rules_res.df[rules_res.df["RULE_ID"].astype(str) == str(row["RULE_ID"])]
                                    if not _rm.empty:
                                        thr = safe_float(_rm.iloc[0].get("THRESHOLD_NUM"))
                                # F50: PERSIST the verdict per event — it used to vanish on the
                                # very next rerun, the moment the operator touched the decide bar.
                                # at_dt (full datetime) is the freshness gate; "at" is display.
                                st.session_state[_rc_key] = {
                                    "value": current_v, "thr": thr,
                                    "label": recheck_sql.recheck_label(_rid),
                                    "at": account_now().strftime("%H:%M"),
                                    "at_dt": account_now(),
                                }
                        else:
                            st.session_state[_rc_key] = {"error": rc.error or "no data today.",
                                                         "at": account_now().strftime("%H:%M")}
                    _rc_state = st.session_state.get(_rc_key)
                    if isinstance(_rc_state, dict):
                        if "error" in _rc_state:
                            # review fix: stamped — "right now" aged poorly on a verdict
                            # that persists across reruns.
                            st.info(f"Re-check unavailable (as of {_rc_state.get('at') or '—'!s}): "
                                    + str(_rc_state["error"]))
                        else:
                            _rcv = safe_float(_rc_state.get("value"), default=float("nan"))
                            _rct = _rc_state.get("thr")
                            _rcl = str(_rc_state.get("label") or "")
                            _rca = str(_rc_state.get("at") or "")
                            # review fix: the one-click resolve is evidence for an AUDIT
                            # note — gate it on freshness (30 min) so a persisted CLEAR
                            # can't resurface days later as if measured just now.
                            try:
                                _rc_fresh = (account_now() - _rc_state["at_dt"]) <= timedelta(minutes=30)
                            except (KeyError, TypeError):
                                _rc_fresh = False
                            if math.isnan(_rcv):
                                st.info("Re-check result unreadable — run it again.")
                            elif _rct is not None and safe_float(_rct) > 0:
                                if _rcv >= safe_float(_rct):
                                    st.warning(f"Still over: {_rcl} = {_rcv:,.2f} vs "
                                               f"threshold {safe_float(_rct):,.2f} "
                                               f"(re-checked {_rca}).")
                                elif not _rc_fresh:
                                    st.info(f"Was clear when re-checked {_rca}: {_rcl} = "
                                            f"{_rcv:,.2f} vs threshold {safe_float(_rct):,.2f} "
                                            "— re-check again before resolving.")
                                else:
                                    st.success(f"Condition clear: {_rcl} = {_rcv:,.2f} vs "
                                               f"threshold {safe_float(_rct):,.2f} "
                                               f"(re-checked {_rca}).")
                                    # F50: close the loop the copy promises — one click
                                    # prefills RESOLVE + ACTIONED + the measured evidence
                                    # (applied at the fragment top, before widgets mount).
                                    if is_operator and st.button(
                                            "Resolve as ACTIONED with this evidence",
                                            key=f"recheck_resolve_{event_id[:8]}_{_sel_nonce}"):
                                        st.session_state["_ow_alert_prefill"] = {
                                            f"alert_action_{event_id[:8]}_{_sel_nonce}": "RESOLVE",
                                            f"alert_kind_{event_id[:8]}_{_sel_nonce}": "ACTIONED",
                                            f"alert_note_{event_id[:8]}_{_sel_nonce}": (
                                                f"Re-check clear at {_rca}: {_rcl} {_rcv:,.2f} "
                                                f"vs threshold {safe_float(_rct):,.2f}")[:500],
                                        }
                                        st.rerun()
                            else:
                                st.info(f"{_rcl}: {_rcv:,.2f} (rule threshold unavailable).")
                    hist = _dr.get("hist") or run(
                        mart_sql.events_for_rule(str(row["RULE_ID"]), 90), page=_PAGE,
                        key=f"hist_rule_{event_id[:8]}", tier="recent",
                        source="ALERT_EVENTS (90d, this rule)")
                    if hist.usable() and len(hist.df) > 1:
                        with st.expander(f"This rule recently ({len(hist.df)} events)"):
                            styled_table(hist.df, height=220)
                    # rec26: how was this resolved last time? The kind + note from the account's
                    # own history is a playbook this exact alert has earned. styled_table (not
                    # markdown) so a note can't inject formatting.
                    _res = _dr.get("res") or run(
                        mart_sql.resolutions_for_rule(str(row["RULE_ID"])), page=_PAGE,
                        key=f"resolved_rule_{event_id[:8]}", tier="recent",
                        source="ALERT_EVENTS (resolved, this rule)")
                    if _res.usable():
                        with st.expander(f"How this was resolved before ({len(_res.df)})"):
                            styled_table(_res.df[["RESOLVED_AT", "RESOLUTION_KIND", "RESOLUTION_NOTE"]],
                                         height=180, slug="rule-resolutions")
                            st.caption("The last few times this rule was closed — kind and note from "
                                       "your own history, the playbook this alert has earned.")
                    if wh_inline:
                        with st.expander(f"Respond — closed loop on {wh_inline}", expanded=False):
                            st.caption("Playbook above says what; this generates the how. Execute is "
                                       "operator-gated, audited to REMEDIATION_LOG, and books an "
                                       "ESTIMATED ledger item — verify it on Cost & Contract > "
                                       "Optimization & Savings. The change scan settles its own "
                                       "measured row for warehouse-setting changes (V038).")
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
                                        # $-escape: DESCRIPTION is data — a '$' in it pairs with usd_s
                                        st.markdown(md_dollars(f"- **{state}** — {li.get('DESCRIPTION')} ({usd_s})"))
                                    st.caption("VERIFIED comes from a manual proof-backed verify on the "
                                               "Savings ledger; the change scan (V038) additionally books "
                                               "and settles its own measured row for warehouse-setting "
                                               "changes.")
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
                                # R3-6: key the reverse-hint off the SAME fix_kind branching that
                                # built stmt_cl (the old binary ternary mislabeled an auto-suspend
                                # fix as a cluster-range change — its token matched neither arm).
                                _rev_kind = ("AUTO_SUSPEND" if fix_kind.startswith("Tighten")
                                             else "STATEMENT_TIMEOUT" if fix_kind.startswith("Statement")
                                             else "CLUSTER_RANGE")
                                st.caption(remediation.reverse_hint(_rev_kind, wh_inline))
                                if (confirm_gate(wh_inline, "Execute + audit + book estimate",
                                                 key=f"clf_exec_{event_id[:8]}",
                                                 prompt="Type the warehouse name to confirm",
                                                 object_name=True)
                                        and write_gate_open(f"clf_exec_{event_id[:8]}")):
                                    ok, msg = execute_statement(stmt_cl, page=_PAGE)
                                    execute_statement(
                                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE, EXECUTED_BY) "
                                        f"SELECT 'ALERT_CLOSED_LOOP', {sql_literal(wh_inline)}, "
                                        f"{sql_literal(stmt_cl)}, {sql_literal('EXECUTED' if ok else 'FAILED')}, "
                                        f"{sql_literal(('event ' + event_id[:8] + ': ' + msg)[:2000])}, {identity_sql()}",
                                        page=_PAGE)
                                    if ok:
                                        from app.ui.components import log_ui_event
                                        log_ui_event("remediation_exec", page=_PAGE)
                                    if ok:
                                        execute_statement(
                                            f"INSERT INTO {core_object('SAVINGS_LEDGER')} "
                                            "(DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES) "
                                            f"SELECT {sql_literal(fix_kind + ' on ' + wh_inline + ' (alert closed loop)')}, "
                                            f"'ESTIMATED', 0, {sql_literal(stmt_cl)}, "
                                            f"{sql_literal('From alert event ' + event_id[:8] + '; verifier measures actuals.')}",
                                            page=_PAGE)
                                    stamp_write(f"clf_exec_{event_id[:8]}", ok)  # C48
                                    notify(ok, msg)
                            else:
                                st.caption("Copy the SQL; executing needs SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")
                            booked = run(mart_sql.ledger_for_event(event_id[:8]), page=_PAGE,
                                         key=f"clf_led_{event_id[:8]}", tier="live",
                                         source="SAVINGS_LEDGER")
                            if booked.usable():
                                st.markdown("**Savings booked from this alert**")
                                styled_table(booked.df[["DESCRIPTION", "STATE", "ESTIMATED_USD",
                                                        "VERIFIED_USD", "CREATED_AT"]], height=140)
                                st.caption("ESTIMATED flips to VERIFIED when verified on the Savings "
                                           "ledger (proof + measured amount). Warehouse-setting changes "
                                           "also get a separate change-scan row that settles itself (V038).")
                    # Per-family evidence: each alert gets the evidence pack that matches
                    # the metric it fired on, not one query-latency pack for everything
                    # (which fed cost/serverless/Cortex alerts unrelated latency rows).
                    plan = plan_for_alert(str(row["RULE_ID"]), str(row["TITLE"]),
                                          detail_text, str(row["RAISED_AT"]))
                    if plan is not None:
                        _scope = plan.scope_note
                        st.caption(
                            f"Explain with AI — evidence: {plan.label} · {plan.window_label}"
                            + (f" · {_scope}" if _scope else ""))
                        # The evidence pack is a historical ACCOUNT_USAGE/mart query, so it
                        # stays button-gated; once assembled, the shared ai_evaluation_panel
                        # owns the AI run, cost disclosure, grounding popover, download, and
                        # the operator-only save-to-event hook.
                        _expl_prompt_key = f"_ai_expl_prompt_{event_id}"
                        if st.button("Assemble the evidence", key=f"ai_expl_go_{event_id[:8]}",
                                     help="Runs the evidence pack that matches THIS alert's metric, "
                                          "then unlocks a grounded AI evaluation over exactly those rows."):
                            ev = run(alert_evidence_sql.build(plan),
                                     page=_PAGE, key=f"ai_ev_{plan.kind}_{event_id[:8]}", tier="historical",
                                     source="ACCOUNT_USAGE / marts (per-alert evidence)")
                            if not ev.ok or ev.empty:
                                st.session_state.pop(_expl_prompt_key, None)
                                empty_state("no_data_yet",
                                            "No evidence rows for this alert's scope — the driver may be "
                                            "outside the window, or the family/service label didn't match.")
                            else:
                                st.session_state[_expl_prompt_key] = alert_evidence_prompt(
                                    plan.kind, str(row["TITLE"]), detail_text, ev.df, plan.window_label)
                        _expl_prompt = st.session_state.get(_expl_prompt_key)
                        if _expl_prompt:
                            def _append_hypothesis(answer: str) -> tuple[bool, str]:
                                # C48 re-verify fix: content-scoped key — regenerate-and-
                                # save-again inside this fragment is a NEW hypothesis, not
                                # a duplicate; only a byte-identical re-save is swallowed.
                                _ai_key = f"ai_save_{event_id[:8]}:{hash(answer) & 0xFFFFFF}"
                                if not write_gate_open(_ai_key):  # C48
                                    return True, ""
                                appended = (
                                    f"UPDATE {core_object('ALERT_EVENTS')} SET DETAIL = "
                                    f"LEFT(COALESCE(DETAIL, '') || ' | AI hypothesis: ' || "
                                    f"{sql_literal(answer[:800])}, 2000) "
                                    f"WHERE EVENT_ID = {sql_literal(event_id)};"
                                )
                                ok_u, msg_u = execute_statement(appended, page=_PAGE)
                                stamp_write(_ai_key, ok_u)  # C48
                                return ok_u, (msg_u if not ok_u else "Hypothesis stored on the event.")
                            ai_evaluation_panel(
                                key=f"alert_expl_{event_id[:8]}",
                                prompt=_expl_prompt,
                                settings=load_settings(_PAGE),
                                page=_PAGE,
                                subject=str(row["TITLE"]),
                                on_save=_append_hypothesis if is_operator else None,
                                save_label="Append hypothesis to the event",
                            )

                elif not _bulk_mode:
                    # C42: single-mode empty drawer owns the "select an event" prompt;
                    # in bulk mode there IS no drawer, so the right pane stays quiet
                    # (the feed's bulk caption + the full-width bulk panel guide it).
                    st.caption("Select an event on the left to open its drawer "
                               "(detail, rule, history, playbook, ack/resolve).")
        if is_operator and _bulk_rows:
            st.divider()
            _bdf = edf.iloc[_bulk_rows]
            st.markdown(f"**Bulk acknowledge / resolve — {len(_bdf)} selected**")
            # F53 (lite): confirm knowing exactly what the write hits — the severity
            # mix and the rows themselves, not just a count in the confirm prompt.
            _sevc = _bdf["SEVERITY"].astype(str).str.upper().value_counts()
            st.caption("Selected: " + " · ".join(f"{int(n)} {sev}" for sev, n in _sevc.items()))
            styled_table(_bdf[["RAISED_AT", "SEVERITY", "COMPANY", "TITLE", "STATUS"]],
                         height=160)
            b_action = st.radio("Bulk action", ["ACK", "RESOLVE"], horizontal=True,
                                key=f"alert_bulk_action_{_sel_nonce}")
            b_kind = ""
            if b_action == "RESOLVE":
                b_kind = st.radio("How were these closed?", RESOLUTION_KINDS, horizontal=True,
                                  key=f"alert_bulk_kind_{_sel_nonce}",
                                  help="Required — untagged closes drop out of the per-rule "
                                       "precision score.")
            b_note = st.text_input("Bulk note (applies to every selected event)",
                                   key=f"alert_bulk_note_{_sel_nonce}", max_chars=500)
            _bulk_ids = _bdf["EVENT_ID"].astype(str).tolist()
            if (confirm_gate(f"BULK {b_action}", f"Execute bulk {b_action}",
                             key=f"alert_bulk_exec_{_sel_nonce}",
                             prompt=f"Type BULK {b_action} to confirm ({len(_bulk_ids)} selected)",
                             enabled=bool(_bulk_ids), type="primary")
                    and write_gate_open(f"alert_bulk_exec_{_sel_nonce}")):
                # V051: one atomic proc for the whole selection; the
                # pre-V051 legacy path is the set-based 2-statement pair.
                upd, aud = _bulk_lifecycle_sql(_bulk_ids, b_action, b_note, b_kind)
                call = (f"CALL {core_object('SP_ALERT_LIFECYCLE')}("
                        f"{sql_literal(','.join(str(e) for e in _bulk_ids))}, {sql_literal(b_action)}, "
                        f"{sql_literal(b_note)}, {sql_literal(b_kind)}, {identity_sql()}, "
                        f"{sql_literal(idempotency_key('ALERT_BULK_' + b_action, ''.join(_bulk_ids)))})")
                ok_u, msg_u = execute_action(call, [upd, aud], page=_PAGE)
                stamp_write(f"alert_bulk_exec_{_sel_nonce}", ok_u)  # C48
                notify(ok_u, f"Bulk {b_action}: {len(_bulk_ids)} event(s) — {msg_u}")
                if ok_u:
                    from app.ui.components import log_ui_event
                    log_ui_event("alert_resolve" if b_action == "RESOLVE" else "alert_ack",
                                 page=_PAGE)
                    # F51: same post-write hygiene as the drawer — the nonce bump
                    # remounts the bulk widgets (picks, note, typed confirm) fresh, so
                    # an executed selection can never linger visually and re-arm the
                    # gate against the shifted feed; the full rerun re-reads the live
                    # feed as the durable receipt.
                    st.session_state["_ow_alert_sel_nonce"] = _sel_nonce + 1
                    st.session_state.pop("_ow_alert_drawer_bind", None)
                    st.session_state.pop("_ow_alert_bulk_bind", None)
                    st.session_state["_ow_alert_receipt"] = (
                        f"Bulk {b_action} recorded — {len(_bulk_ids)} event(s)")
                    st.rerun()

    # V086: snoozed events are hidden from the OPEN/ACK feed until their wake time.
    # Surface them so a snooze is never a black hole, with an early un-snooze. This
    # renders OUTSIDE the `if guard(events, ...)` block on purpose: an empty open
    # feed must not present a green "found nothing over threshold" all-clear while a
    # snoozed CRITICAL is still pending (it would be invisible until its auto-wake).
    _snz = run(mart_sql.snoozed_alert_events(100, company), page=_PAGE,
               key=f"alert_snoozed_{company}", tier="live",
               source="ALERT_EVENTS (STATUS=SNOOZED)", probe=True)
    if _snz.usable() and not _snz.empty:
        with st.expander(f"💤 Snoozed ({len(_snz.df)}) — hidden from triage until their wake time"):
            # F52: a relative countdown, soonest wake first — a snooze reads as
            # a running timer, not a black-hole timestamp.
            _sdf = _snz.df.copy()
            _sn_now = account_now()

            def _wakes_in(value: object) -> str:
                try:
                    _secs = (value - _sn_now).total_seconds()
                except (TypeError, AttributeError):
                    return "—"
                if _secs != _secs:  # NaT
                    return "—"
                _secs = max(0.0, _secs)
                if _secs >= 86400:
                    # review fix: humanize_duration caps at hours by design
                    # (query durations) — "167h 59m" is not a countdown.
                    _d, _rem = divmod(round(_secs), 86400)
                    _h = int(_rem // 3600)
                    return f"{_d}d {_h}h" if _h else f"{_d}d"
                return humanize_duration(_secs)

            _sdf["WAKES_IN"] = _sdf["SNOOZED_UNTIL"].map(_wakes_in)
            _sdf = _sdf.sort_values("SNOOZED_UNTIL")
            styled_table(_sdf[["WAKES_IN", "SEVERITY", "TITLE", "SNOOZED_UNTIL",
                               "SNOOZE_BY", "SNOOZE_REASON"]])
            if is_operator:
                _snz_opts = {
                    f"[{r['SEVERITY']}] {str(r['TITLE'])[:60]} (wakes {r['SNOOZED_UNTIL']})": str(r["EVENT_ID"])
                    for _, r in _snz.df.iterrows()
                }
                _snz_pick = st.multiselect("Wake now (un-snooze early)", list(_snz_opts),
                                           key="alert_unsnooze_pick")
                _uids = [_snz_opts[c] for c in _snz_pick]
                # C48 re-verify fix: scope the latch by the SELECTION — inside
                # this fragment the run seq never advances, so a bare key would
                # swallow "wake these… and that one too" for the whole backstop.
                _unsz_key = "alert_unsnooze:" + "".join(str(e) for e in _uids)[:64]
                if (st.button("Wake selected now", key="alert_unsnooze_exec",
                              disabled=not _uids, type="primary")
                        and write_gate_open(_unsz_key)):
                    # hours=0 tells SP_ALERT_SNOOZE to un-snooze now (restore prior status).
                    call = (f"CALL {core_object('SP_ALERT_SNOOZE')}("
                            f"{sql_literal(','.join(str(e) for e in _uids))}, 0, '', {identity_sql()}, "
                            f"{sql_literal(idempotency_key('ALERT_UNSNOOZE', ''.join(_uids)))})")
                    ok_s, msg_s = execute_action(call, _unsnooze_stmts(_uids), page=_PAGE)
                    stamp_write(_unsz_key, ok_s)  # C48
                    notify(ok_s, f"Un-snooze: {len(_uids)} event(s) — {msg_s}")
                    if ok_s:
                        from app.ui.components import log_ui_event
                        log_ui_event("alert_unsnooze", page=_PAGE)
            else:
                st.caption("Un-snoozing requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Alerts", "Open events, lifecycle with audit, and the rules that raise them.", icon_name="alerts")
    # C18: shared since-last-visit opener (renders nothing mid-session or for
    # anonymous viewers; the helper skips the "Open events" jump on this page).
    since_last_visit_opener(_PAGE, f["company"])
    section_filter_contract(
        f,
        applies=("company",),
        note="Headline counts are the current open queue; the global Window does not limit them.",
    )
    # #3: operator gating from the VIEWER identity + allowlist, not CURRENT_ROLE().
    is_operator = _is_operator()

    company = f["company"]
    # The small uncapped count owns the section badge. The 500-row event payload
    # loads only inside Open events, after the lazy section is selected.
    counts = run(mart_sql.open_alert_severity_counts(company), page=_PAGE,
                 key=f"alert_counts_{company}", tier="live",
                 source="ALERT_EVENTS (COUNT_IF by severity, uncapped)")
    if counts.usable():
        _c0 = counts.df.iloc[0]
        crit_n = int(safe_float(_c0.get("CRIT")))
        high_n = int(safe_float(_c0.get("HIGH")))
        total_n = int(safe_float(_c0.get("TOTAL")))
    else:
        crit_n = high_n = total_n = 0
    # C17: the page answers "should I worry?" BEFORE the section bar, from the
    # uncapped counts it already reads. Rendered only when the counts resolved —
    # a failed read must never manufacture a green all-clear.
    if counts.usable():
        page_verdict_line(page_verdict([
            Signal("bad", f"{crit_n} open critical(s)") if crit_n else None,
            Signal("warn", f"{high_n} open high(s)") if high_n else None,
        ], healthy=(f"{total_n} open event(s), none critical or high"
                    if total_n else "the alert queue is empty")))
    section = lazy_sections(["Open events", "Rules", "History", "Native delivery"],
                            key="alerts_section",
                            counts={"Open events": total_n} if counts.usable() else None)
    _contracts = {
        "Open events": {
            "applies": ("company",),
            "note": "Current open and acknowledged events, independent of the global Window.",
        },
        "Rules": {
            "applies": (),
            "note": "Account-wide configuration and fixed 90-day precision evidence.",
        },
        "History": {
            "applies": (),
            "partial": ("company",),
            "note": "Fixed 30/90-day lifecycle horizons; Company applies only to incident metrics.",
        },
        "Native delivery": {
            "applies": (),
            "note": "Account-wide routes, backlog, and delivery configuration.",
        },
    }
    section_filter_contract(f, **_contracts[section])

    if section == "Open events":
        events = run(mart_sql.open_alert_events(500, company), page=_PAGE,
                     key=f"alert_events_{company}", tier="live",
                     source="ALERT_EVENTS" if company == "ALL"
                     else f"ALERT_EVENTS ({company} + account-level)")
        # The uncapped aggregate owns the tiles; feed-derived counts are only a
        # labeled fallback when that tiny aggregate fails.
        _counts_known = counts.usable()
        if not _counts_known and events.ok:
            if events.empty:
                crit_n = high_n = total_n = 0
            else:
                _sev = events.df["SEVERITY"].astype(str).str.upper()
                crit_n = int((_sev == "CRITICAL").sum())
                high_n = int((_sev == "HIGH").sum())
                total_n = len(events.df)
            _counts_known = True
        if _counts_known:
            kpi_row([
                {"label": "Open critical", "value": f"{crit_n}",
                 "severity": "bad" if crit_n else "ok",
                 "delta_color": "inverse" if crit_n else "off"},
                {"label": "Open high", "value": f"{high_n}",
                 "severity": "warn" if high_n else "ok"},
                {"label": "Open total", "value": f"{total_n}",
                 "help": ("True open+ack count across all severities. The feed table below "
                          "shows the 500 most severe/newest; tiles count every open event."
                          if counts.usable() and total_n > 500
                          else "Open + acknowledged events across all severities."),
                 "severity": "info"},
            ])
        _open_events_section(events, is_operator, company)
    elif section == "Rules":
        rules = run(mart_sql.alert_rules(), page=_PAGE, key="alert_rules", tier="recent",
                    source="ALERT_CONFIG")
        if guard(rules, "No alert rules found.", setup_hint=_SETUP_HINT):
            styled_table(rules.df)
            st.caption(
                "Thresholds are data, not code: update ALERT_CONFIG and the next scan uses them. "
                "Statistical anomaly detection runs in-app (Cost & Contract > Spend & Attribution, Operations > Warehouses) "
                "and is deliberately separate from these deterministic rules."
            )
            st.markdown("**Rule precision (90d)** — is each rule worth its pages?")
            prec = run(mart_sql.rule_precision(90), page=_PAGE, key="rule_precision",
                       tier="recent", source="ALERT_EVENTS.RESOLUTION_KIND")
            if not prec.ok:
                empty_state("needs_setup", "Precision is not installed yet — an admin can apply the pending schema update on Admin → Migrations & freshness.")
            elif prec.empty:
                empty_state("no_data_yet",
                            "No resolved events in 90d — precision appears once alerts get closed "
                            "with a resolution kind.")
            else:
                pdf_ = prec.df.copy()
                _prec_sel = selectable_table(pdf_, key="rule_prec_sel", column_config={
                    "PRECISION_PCT": st.column_config.NumberColumn("Precision %", format="%.1f%%"),
                })
                # Click a rule -> its recent resolved-event playbook inline. Store the
                # RULE_ID (not the positional index) so sticky re-emit just re-renders the
                # same cached read instead of re-firing anything.
                if _prec_sel is not None and 0 <= _prec_sel < len(pdf_):
                    st.session_state["rule_prec_sel_last"] = str(pdf_.iloc[int(_prec_sel)]["RULE_ID"])
                _prec_rid = st.session_state.get("rule_prec_sel_last")
                if _prec_rid:
                    _prec_res = run(mart_sql.resolutions_for_rule(_prec_rid), page=_PAGE,
                                    key=f"rule_prec_res:{_prec_rid}", tier="recent",
                                    source="ALERT_EVENTS (resolved, this rule)")
                    if _prec_res.usable():
                        st.markdown(f"**Recent resolutions for {_prec_rid}**")
                        styled_table(_prec_res.df[["RESOLVED_AT", "RESOLUTION_KIND", "RESOLUTION_NOTE"]],
                                     height=180, slug="rule-prec-resolutions")
                    else:
                        st.caption(f"No resolved events yet for {_prec_rid}.")
                st.caption(
                    "Precision = ACTIONED / (ACTIONED + NOISE); EXPECTED is excluded. High NOISE "
                    "with low precision = move the threshold away from noise in the rule's firing "
                    "direction; high UNTAGGED = the score isn't "
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
                    st.caption("Rule changes are generate-only: review, then run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")
                    st.caption("WINDOW_HOURS is informational: each rule family's scan "
                               "window is fixed in SP_ALERT_SCAN (see the runbook's rule "
                               "catalogue) — editing the column does not change the scan.")

    elif section == "History":
        # Perf: the section's five UNCONDITIONAL 'recent' reads prefetch in one parallel batch
        # instead of five serial round-trips; each keeps its original run() fallback and render
        # point, so which scans fire and the render order are unchanged (rt/bl stay conditional).
        _hb = run_batch([
            {"key": "hist", "sql": mart_sql.alert_event_history(30), "source": "ALERT_EVENTS"},
            {"key": "mttr", "sql": mart_sql.alert_mttr(90), "source": "ALERT_EVENTS lifecycle timestamps"},
            {"key": "inc", "sql": mart_sql.incident_metrics(90, f["company"]),
             "source": f"INCIDENTS lifecycle (90d, {f['company']} + account-level)"},
            {"key": "slo", "sql": mart_sql.delivery_slo_summary(30),
             "source": "ALERT_DELIVERIES + ALERT_EVENTS + APP_ERROR_LOG"},
            {"key": "fat", "sql": mart_sql.alert_fatigue(30),
             "source": "ALERT_EVENTS (resolution kinds + dedupe repeats)"},
        ], page=_PAGE, tier="recent")
        hist = _hb.get("hist") or run(mart_sql.alert_event_history(30), page=_PAGE, key="alert_history",
                   tier="recent", source="ALERT_EVENTS")
        if guard(hist, "No alert events in the last 30 days.", setup_hint=_SETUP_HINT):
            charts.events_by_day(hist.df)
            result_caption(hist)
        st.markdown("**Response performance (MTTA / MTTR)**")
        mttr = _hb.get("mttr") or run(mart_sql.alert_mttr(90), page=_PAGE, key="alert_mttr",
                   tier="recent", source="ALERT_EVENTS lifecycle timestamps")
        if mttr.usable():
            df = mttr.df.copy()

            def _weighted_minutes(val_col: str, weight_col: str) -> float | None:
                # alert_mttr returns per-WEEK averages; a plain .mean() of those is a
                # mean-of-means that weights a 1-event week the same as a 100-event one.
                # Pool the last 4 active weeks by their event count for a true MTTA/MTTR.
                sub = df.dropna(subset=[val_col]).tail(4)
                w = pd.to_numeric(sub.get(weight_col), errors="coerce").fillna(0.0)
                v = pd.to_numeric(sub[val_col], errors="coerce").fillna(0.0)
                total = float(w.sum())
                return float((v * w).sum() / total) if total > 0 else None

            _mtta = _weighted_minutes("MTTA_MIN", "ACKED")
            _mttr = _weighted_minutes("MTTR_MIN", "RESOLVED")
            kpi_row([
                {"label": "MTTA (last 4 active weeks)",
                 "value": humanize_duration(_mtta, "min") if _mtta is not None else "No acks yet",
                 "help": "Raised -> acknowledged, event-weighted. Improve by working the queue, not the inbox."},
                {"label": "MTTR (last 4 active weeks)",
                 "value": humanize_duration(_mttr, "min") if _mttr is not None else "No resolves yet",
                 "help": "Raised -> resolved (event-weighted), including remediation time."},
                {"label": "Events (90d)", "value": f"{int(df['EVENTS'].sum()):,}"},
            ])
            styled_table(df, height=240)
        else:
            st.caption("MTTA/MTTR appears once events have been acknowledged/resolved via the lifecycle workflow.")

        st.markdown("**Incident lifecycle (90d, incident grain)**")
        # Moved from Control Room (v4.50): retrospective process-health
        # medians don't change morning to morning — they read with alert
        # history, beside the alert-grain MTTA/MTTR above.
        inc_met = _hb.get("inc") or run(mart_sql.incident_metrics(90, f["company"]), page=_PAGE,
                      key=f"inc_metrics_{f['company']}", tier="recent",
                      source=f"INCIDENTS lifecycle (90d, {f['company']} + account-level)")
        if inc_met.usable():
            im = inc_met.df.iloc[0]
            kpi_row([
                {"label": "MTTA / MTTR (90d)",
                 "value": (f"{humanize_duration(im.get('MTTA_MIN'), 'min')} / "
                           f"{humanize_duration(im.get('MTTR_MIN'), 'min')}"),
                 "help": "Detected -> acknowledged / resolved at INCIDENT grain — "
                         "the alert-grain pair above counts individual events."},
                {"label": "Reopen rate", "value": _optional_number(im.get("REOPEN_PCT"), "%"),
                 "help": "Of incidents resolved in the last 90 days, the share later reopened "
                         "(a new incident links back via REOPENED_FROM)."},
                {"label": "Alerts / incident", "value": _optional_number(im.get("COMPRESSION"), decimals=1),
                 "help": "How many alerts each incident absorbs — higher means storms "
                         "compress into one object instead of many pages."},
                # "Change-correlated" removed v4.351: it counted INCIDENT_MEMBERS of kind
                # WH_CHANGE/DEPLOY, but no writer ever persists those kinds, so it was a
                # permanent misleading 0%. Change correlation lives in the Control Room RCA.
            ])
        else:
            st.caption("Incident lifecycle metrics appear once incidents are declared (Control Room).")

        st.markdown("**Delivery health (SLO)** — did alerts leave the building, and how fast?")
        slo = _hb.get("slo") or run(mart_sql.delivery_slo_summary(30), page=_PAGE, key="delivery_slo",
                  tier="recent", source="ALERT_DELIVERIES + ALERT_EVENTS + APP_ERROR_LOG")
        if slo.usable():
            row0 = slo.df.iloc[0]
            _und = int(safe_float(row0.get("UNDELIVERED_CRITICALS_30M")))
            _exp = int(safe_float(row0.get("EXPIRED_UNDELIVERED")))
            kpi_row([
                {"label": "Events delivered (30d)",
                 "value": f"{safe_float(row0.get('EVENTS_DELIVERED')):,.0f} / {safe_float(row0.get('EVENTS_RAISED')):,.0f}",
                 "help": "Raised events with at least one delivery row. Routes filter by "
                         "severity, so 100% is not the target."},
                {"label": "Latency p50 / p95",
                 "value": (f"{humanize_duration(row0.get('MEDIAN_MIN'), 'min')} / "
                           f"{humanize_duration(row0.get('P95_MIN'), 'min')}"),
                 "help": "RAISED_AT -> first SENT_AT; the notify task rides the hourly chain."},
                {"label": "Undelivered criticals (30m+)", "value": f"{_und}",
                 "severity": "bad" if _und else "ok",
                 "delta_color": "inverse" if _und else "off"},
                {"label": "Route failures (30d)",
                 "value": f"{safe_float(row0.get('ROUTE_FAILURES')):,.0f}",
                 "help": "route_send_failed rows in APP_ERROR_LOG — RUNBOOK section 19 has the Teams debugging path."},
                # rec19: the webhook itself flags an OPEN event that aged past the
                # 24h delivery window with no send — surfaced here, not just in logs.
                {"label": "Expired-undelivered (30d)", "value": f"{_exp}",
                 "severity": "bad" if _exp else "ok",
                 "delta_color": "inverse" if _exp else "off",
                 "help": "undelivered_expired rows SP_NOTIFY_WEBHOOK raises when an eligible "
                         "event crosses 24h unsent — a persistent integration outage, not lag."},
            ])
            _db = run_batch([
                {"key": "rt", "sql": mart_sql.delivery_by_route(30), "source": "ALERT_DELIVERIES by route"},
                {"key": "bl", "sql": mart_sql.route_backlog(),
                 "source": "ALERT_EVENTS x ALERT_ROUTES (send-eligibility)"},
            ], page=_PAGE, tier="recent")
            rt = _db.get("rt") or run(mart_sql.delivery_by_route(30), page=_PAGE, key="delivery_routes",
                     tier="recent", source="ALERT_DELIVERIES by route")
            if rt.usable():
                styled_table(rt.df, height=170)
            # rec19: per-route BACKLOG — what SP_NOTIFY_WEBHOOK will drain next and
            # the age of the oldest pending event (the starvation signal rec8 fixes).
            # Same send-eligibility predicate as the drainer, so the two agree.
            st.markdown("**Route backlog** — open eligible events not yet delivered, oldest first.")
            bl = _db.get("bl") or run(mart_sql.route_backlog(), page=_PAGE, key="route_backlog",
                     tier="recent", source="ALERT_EVENTS x ALERT_ROUTES (send-eligibility)")
            if bl.usable() and not bl.df.empty:
                styled_table(bl.df, height=170, column_config={
                    "OLDEST_MIN": st.column_config.Column("Oldest"),
                })
                st.caption("A rising OLDEST while the notify task runs means a route is starved — "
                           "check its integration. The oldest-first drain (V064) clears the tail first.")
            else:
                st.caption("No route has an undelivered backlog right now.")
        else:
            st.caption("Delivery SLOs appear once the per-route ledger has rows.")

        st.markdown("**Alert fatigue** — which rules burn attention without earning it?")
        fat = _hb.get("fat") or run(mart_sql.alert_fatigue(30), page=_PAGE, key="alert_fatigue",
                  tier="recent", source="ALERT_EVENTS (resolution kinds + dedupe repeats)")
        if fat.usable():
            _fat_sel = selectable_table(fat.df, key="alert_fatigue_sel", height=240, column_config={
                "PER_WEEK": st.column_config.NumberColumn("Events/week", format="%.1f"),
            })
            st.caption("High NOISE or UNTAGGED at high volume = tune or retire the rule — "
                       "the precision panel under Rules has suggested thresholds.")
            # Click a rule -> its recent events inline. Store RULE_ID (not the row index)
            # so the sticky re-emit only re-renders the same cached read.
            if _fat_sel is not None and 0 <= _fat_sel < len(fat.df):
                st.session_state["alert_fatigue_sel_last"] = str(fat.df.iloc[int(_fat_sel)]["RULE_ID"])
            _fat_rid = st.session_state.get("alert_fatigue_sel_last")
            if _fat_rid:
                _fat_ev = run(mart_sql.events_for_rule(_fat_rid, 90), page=_PAGE,
                              key=f"alert_fatigue_ev:{_fat_rid}", tier="recent",
                              source="ALERT_EVENTS (90d, this rule)")
                if _fat_ev.usable():
                    st.markdown(f"**Recent events for {_fat_rid}**")
                    styled_table(_fat_ev.df, height=220)
                else:
                    st.caption(f"No events in 90d for {_fat_rid}.")
        else:
            empty_state("no_data_yet", "Fatigue metrics appear once events exist in the window.")

    else:
        _delivery_status()
        _last_delivery_card()
        st.markdown("**Routing (family → channel)**")
        panel_help(
            "Routing sends each family/severity through a named notification "
            "integration — COST to #finops, SECURITY to #security. The seeded ALL/HIGH "
            "route keeps the original single-webhook behavior until you add rows. One "
            "failing integration never blocks the others."
        )
        routes = run(mart_sql.alert_routes(), page=_PAGE, key="alert_routes", tier="recent",  # r24 #8: config table; post-save freshness rides the action salt
                     source="ALERT_ROUTES")
        if guard(routes, "No routes configured yet.",
                 setup_hint=_SETUP_HINT):
            styled_table(with_user_names(routes.df, _PAGE, user_col="CREATED_BY", display_col="Created by"))
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
        st.warning(
            "Trust boundary — treat everything rendered in this panel as PUBLIC: it is shown to "
            "every viewer, so never paste a secret into these templates (keep credentials in "
            "Snowflake secrets/notification integrations)."
        )
        # rec19: each template is hundreds of lines — keep the blurb visible but tuck the
        # wall of SQL behind a collapsed expander with a real download button.
        for filename, blurb in (
            ("native_alert_templates.sql", "Email via Snowflake ALERT objects"),
            ("webhook_delivery.sql", "Slack / Teams webhook via SYSTEM$SEND_SNOWFLAKE_NOTIFICATION"),
        ):
            st.markdown(f"**{blurb}**")
            template_path = Path(__file__).resolve().parents[3] / "snowflake" / filename
            try:
                _sql_text = template_path.read_text(encoding="utf-8")
            except OSError:
                st.info(f"File not found in this deployment; see snowflake/{filename} in the repo.")
                continue
            with st.expander(f"View / download {filename}", expanded=False):
                st.code(_sql_text, language="sql")
                download_text_button(f"Download {filename}", _sql_text, filename)
