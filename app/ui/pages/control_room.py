"""Control Room — DBA morning triage on one screen.

Ranked queue (alerts + task failures + spend anomalies), telemetry freshness,
the since-yesterday operations pulse, and spend movers. No button maze: the
queue is visible on entry.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import THRESHOLDS
from app.core.errors import safe_page
from app.core.query import run, run_batch
from app.core.state import filters
from app.data import cost_sql, mart27_sql, mart_sql, ops_sql, security_sql
from app.logic.actions import triage_queue
from app.logic.anomaly import anomaly_summary, complete_days_only, flag_anomalies
from app.logic.formulas import credits_to_usd, format_usd, pct_delta, safe_float
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    load_settings,
    localize_timestamps,
    page_header,
    panel_help,
    result_caption,
    run_mart_first,
    selectable_table,
    styled_table,
    with_user_names,
)

_PAGE = "Control Room"



def _incident_declare_sql(title: str, severity: str, company: str, proposal_key: str) -> str:
    """Generate-then-run declare: one incident + every open alert of the
    proposal's dedupe family as members (48h window). Two INSERTs sharing an
    APP-generated incident id (bug round 2 B3: the previous session-variable
    opener was outside the operator allow-list, so it was refused and the
    variable never existed — declare wrote zero rows. An inlined uuid keeps the
    id shared across both INSERTs, and both are allow-listed OVERWATCH DML)."""
    import uuid

    from app.config import core_object
    from app.core.sqlsafe import sql_literal
    fam = sql_literal(str(proposal_key).split("|", 1)[0])
    inc_id = sql_literal(str(uuid.uuid4()))
    return (
        f"INSERT INTO {core_object('INCIDENTS')} "
        "(INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND) "
        f"SELECT {inc_id}, {sql_literal(str(title)[:300])}, {sql_literal(str(severity).upper())}, "
        f"'OPEN', {sql_literal(str(company))}, CURRENT_TIMESTAMP(), 'UNKNOWN';\n"
        f"INSERT INTO {core_object('INCIDENT_MEMBERS')} "
        "(INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED) "
        f"SELECT {inc_id}, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE "
        f"FROM {core_object('ALERT_EVENTS')} e "
        "WHERE e.STATUS IN ('OPEN', 'ACK') "
        "AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP()) "
        # both companies share rule families — members must match the
        # proposal's company too (live round 8), account-level rides along
        f"AND (e.COMPANY = {sql_literal(str(company))} OR UPPER(e.COMPANY) = 'ALL') "
        f"AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = {fam} "
        f"AND NOT EXISTS (SELECT 1 FROM {core_object('INCIDENT_MEMBERS')} m "
        "WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID);"
    )


def _incident_close_sql(incident_id: str, kind: str, note: str) -> str:
    """Forward-only close: only OPEN/MITIGATED rows move; reopen is a NEW
    incident with REOPENED_FROM — history never rewrites."""
    from app.config import core_object
    from app.core.sqlsafe import sql_literal
    return (
        f"UPDATE {core_object('INCIDENTS')} "
        f"SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), "
        f"ROOT_CAUSE_KIND = {sql_literal(str(kind).upper())}, "
        f"ROOT_CAUSE_NOTE = {sql_literal(str(note)[:2000])}, "
        "UPDATED_AT = CURRENT_TIMESTAMP() "
        f"WHERE INCIDENT_ID = {sql_literal(str(incident_id))} "
        "AND STATUS IN ('OPEN', 'MITIGATED');"
    )


def _day_replay() -> None:
    """One day, every domain, one story — the flight recorder Snowsight
    can't assemble from its silos."""
    from datetime import timedelta

    from app.logic.formulas import account_today
    from app.logic.replay import replay_headlines

    st.subheader("Day replay — what changed?")
    pick = st.date_input("Day", value=account_today() - timedelta(days=1),
                         min_value=account_today() - timedelta(days=120),
                         max_value=account_today(), key="cr_replay_day")
    day_iso = pick.isoformat()
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    rp_company = filters()["company"]
    # Six independent reads -> two tier-grouped parallel batches (Codex #7);
    # any batch failure falls back to the original serial per-query path.
    _b_recent = run_batch([
        {"key": "mv", "sql": mart_sql.day_spend_movers(day_iso, rp_company),
         "source": "FACT_WAREHOUSE_DAILY vs 14d baseline"},
        {"key": "act", "sql": mart_sql.day_activity(day_iso, rp_company),
         "source": "FACT_QUERY_HOURLY (day vs baseline)"},
        {"key": "tf", "sql": mart_sql.day_task_failures(day_iso, rp_company),
         "source": "FACT_TASK_DAILY (failures that day)"},
        {"key": "al", "sql": mart_sql.day_alerts(day_iso, rp_company),
         "source": "ALERT_EVENTS (that day)"},
    ], page=_PAGE, tier="recent")
    _b_hist = run_batch([
        {"key": "ddl", "sql": security_sql.day_ddl(day_iso, rp_company),
         "source": "QUERY_HISTORY (DDL that day)", "max_rows": 300},
        {"key": "gr", "sql": security_sql.day_grants(day_iso, rp_company),
         "source": "GRANTS_TO_USERS (that day)", "max_rows": 200},
    ], page=_PAGE, tier="historical")
    if _b_recent is not None and _b_hist is not None:
        movers, activity = _b_recent["mv"], _b_recent["act"]
        tasks, alerts_d = _b_recent["tf"], _b_recent["al"]
        ddl, grants = _b_hist["ddl"], _b_hist["gr"]
    else:
        movers = run(mart_sql.day_spend_movers(day_iso, rp_company), page=_PAGE,
                     key=f"rp_mv_{rp_company}_{day_iso}",
                     tier="recent", source="FACT_WAREHOUSE_DAILY vs 14d baseline")
        activity = run(mart_sql.day_activity(day_iso, rp_company), page=_PAGE,
                       key=f"rp_act_{rp_company}_{day_iso}",
                       tier="recent", source="FACT_QUERY_HOURLY (day vs baseline)")
        ddl = run(security_sql.day_ddl(day_iso, rp_company), page=_PAGE,
                  key=f"rp_ddl_{rp_company}_{day_iso}",
                  tier="historical", source="QUERY_HISTORY (DDL that day)")
        grants = run(security_sql.day_grants(day_iso, rp_company), page=_PAGE,
                     key=f"rp_gr_{rp_company}_{day_iso}",
                     tier="historical", source="GRANTS_TO_USERS (that day)")
        tasks = run(mart_sql.day_task_failures(day_iso, rp_company), page=_PAGE,
                    key=f"rp_tf_{rp_company}_{day_iso}",
                    tier="recent", source="FACT_TASK_DAILY (failures that day)")
        alerts_d = run(mart_sql.day_alerts(day_iso, rp_company), page=_PAGE,
                       key=f"rp_al_{rp_company}_{day_iso}",
                       tier="recent", source="ALERT_EVENTS (that day)")
    crit_n = int((alerts_d.df["SEVERITY"].astype(str).str.upper() == "CRITICAL").sum()) \
        if alerts_d.usable() else 0
    heads = replay_headlines(
        movers.df if movers.usable() else None,
        activity.df if activity.usable() else None,
        len(ddl.df) if ddl.usable() else 0,
        len(grants.df) if grants.usable() else 0,
        int(tasks.df["FAILED"].sum()) if tasks.usable() else 0,
        crit_n, rate,
    )
    if not any(r.usable() for r in (movers, activity, ddl, grants, tasks, alerts_d)):
        st.info(f"No telemetry loaded for {day_iso} — facts cover ~120 days back.")
        return
    if heads:
        for h in heads:
            (st.error if h["severity"] == "bad" else
             st.warning if h["severity"] == "warn" else st.info)(h["text"])
    else:
        st.success(f"{day_iso}: a quiet day — no notable movement in any domain.")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Spend movers vs 14d baseline**")
        if guard(movers, "No warehouse spend recorded that day."):
            styled_table(movers.df, height=240)
        st.markdown("**Task failures**")
        if tasks.ok and tasks.empty:
            st.success("No task failures that day.")
        elif guard(tasks, ""):
            styled_table(tasks.df, height=200)
    with c2:
        st.markdown("**DDL that landed**")
        if ddl.ok and ddl.empty:
            st.success("No DDL that day.")
        elif guard(ddl, ""):
            styled_table(with_user_names(ddl.df, _PAGE), height=240)
        st.markdown("**Grant changes**")
        if grants.ok and grants.empty:
            st.success("No grant changes that day.")
        elif guard(grants, ""):
            styled_table(with_user_names(grants.df, _PAGE, user_col="GRANTEE_NAME"), height=200)
    st.markdown("**Alerts raised that day**")
    if alerts_d.ok and alerts_d.empty:
        st.success("No alerts raised that day.")
    elif guard(alerts_d, ""):
        styled_table(alerts_d.df, height=200)
    st.caption(f"Scoped to {rp_company} (alerts include account-level rows). Baselines are "
               "each entity's own trailing 14 days; account time throughout.")


def _freshness_board() -> None:
    res = run_mart_first(
        mart_sql.source_freshness_state(), mart_sql.source_freshness(),
        page=_PAGE, key="freshness",
        mart_source="SOURCE_FRESHNESS_STATE (10-min snapshot)",
        live_source="MART_SOURCE_FRESHNESS (aggregate view, pre-V040 fallback)",
        mart_tier="recent", live_tier="recent")   # state moves every 10 min (r14 #13)
    st.subheader("Telemetry freshness")
    if not res.ok:
        st.info("Freshness board is not installed yet; the live fallbacks on this page still work.")
        return
    if res.empty:
        st.info("Freshness view exists but has no rows — have the loader tasks run yet?")
        return
    df = res.df.copy()
    df["HOURS_SINCE_LOAD"] = df["HOURS_SINCE_LOAD"].map(safe_float)

    def _stale(row) -> bool:
        limit = (THRESHOLDS["stale_daily_fact_hours"]
                 if "DAILY" in str(row["SOURCE_NAME"]) or "METERING" in str(row["SOURCE_NAME"])
                 else THRESHOLDS["stale_fact_hours"])
        return row["HOURS_SINCE_LOAD"] > limit

    df["STALE"] = df.apply(_stale, axis=1)
    stale_count = int(df["STALE"].sum())
    if stale_count:
        st.warning(f"{stale_count} source(s) stale — numbers built on them are labeled accordingly.")
    styled_table(
        df[["SOURCE_NAME", "LAST_LOAD_TS", "ROW_COUNT", "HOURS_SINCE_LOAD", "STALE"]],
        column_config={"HOURS_SINCE_LOAD": st.column_config.NumberColumn("Hours since load", format="%.1f")},
    )


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    page_header("Control Room", "Morning triage: what broke, what's burning, what's stale.", icon_name="control",
                scope_note=f"{company} · last {days} days"
                           + (f" · {f['database']}" if f["database"] else ""))

    # ---- pulse (since yesterday 00:00) ---------------------------------------
    # Fact-first pulse (Codex #4): the hourly fact answers this without
    # a live QUERY_HISTORY scan; schema filter (no schema grain) or an empty
    # fact falls back to live, exactly like the Operations page. Triage #12:
    # all three feeding builders anchor on CURRENT_DATE, so days=1 covers
    # yesterday 00:00 -> now (24-48h) — labels say "since yday", not "24h".
    pulse, pulse_from_mart = None, False
    if not f["schema_contains"]:
        m_pulse = run(mart_sql.fact_query_window_summary(1, company, "", "", f["database"]),
                      page=_PAGE, key=f"pulse_fact_{company}", tier="recent",
                      source="FACT_QUERY_HOURLY (mart, loaded hourly)")
        if m_pulse.ok and not m_pulse.empty and safe_float(m_pulse.df.iloc[0].get("QUERY_COUNT")) > 0:
            pulse, pulse_from_mart = m_pulse, True
    else:
        s_pulse = run(mart27_sql.schema_window_summary(1, company, f["database"], f["schema_contains"]),
                      page=_PAGE, key=f"pulse_schema_fact_{company}", tier="recent",
                      source="FACT_QUERY_SCHEMA_HOURLY (mart — p95 is peak hourly)")
        if s_pulse.ok and not s_pulse.empty and safe_float(s_pulse.df.iloc[0].get("QUERY_COUNT")) > 0:
            pulse, pulse_from_mart = s_pulse, True
    if pulse is None:
        pulse = run(ops_sql.query_window_summary(1, company, database=f["database"], schema_contains=f["schema_contains"]),
                    page=_PAGE, key=f"pulse_{company}",
                    tier="live", source="ACCOUNT_USAGE.QUERY_HISTORY (since yesterday 00:00)")
    act = run(mart_sql.fact_daily_activity(14, company, f["database"]), page=_PAGE,
              key="cr_activity", tier="recent", source="FACT_QUERY_HOURLY (daily)")
    q_spark = act.df["QUERIES"].tolist() if act.ok and not act.empty else None
    f_spark = act.df["FAILS"].tolist() if act.ok and not act.empty else None
    if pulse.usable():
        row = pulse.df.iloc[0]
        qcount = safe_float(row.get("QUERY_COUNT"))
        failed = safe_float(row.get("FAILED_COUNT"))
        _fail_bad = bool(qcount and failed / qcount > 0.02)
        kpi_row([
            {"label": "Queries (since yday)", "value": f"{qcount:,.0f}", "spark": q_spark,
             "help": "Midnight-anchored: yesterday 00:00 to now (24-48h depending on "
                     "time of day) — the same span on the mart and live paths."},
            {"label": "Failed", "value": f"{failed:,.0f}",
             "delta": f"{(failed / qcount * 100) if qcount else 0:.1f}%",
             "delta_color": "inverse" if _fail_bad else "off",
             "severity": "bad" if _fail_bad else "ok", "spark": f_spark},
            {"label": "p95 runtime" + (" (peak hourly)" if pulse_from_mart else ""),
             "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s"},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.1f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(pulse)
    elif not pulse.ok:
        st.error(f"Pulse unavailable: {pulse.error}")
    else:
        st.info("No queries recorded since yesterday 00:00 for this scope.")


    # ---- Incidents (V032) ------------------------------------------------------
    st.subheader("Incidents")
    from app.config import OPERATOR_PROFILES, resolve_role_profile
    from app.core.query import execute_statement, run_batch
    from app.core.session import current_role
    from app.ui.components import log_ui_event, notify
    _is_op = resolve_role_profile(current_role()) in OPERATOR_PROFILES
    # T2.1: the three live-tier reads this screen makes (open incidents, proposals,
    # triage alerts) are each re-paid every 30s in steady state. Submit them as ONE
    # live run_batch so morning triage makes one round trip, not three. Proposals
    # render for operators only, so the batch carries that member only when _is_op —
    # non-operators stop paying for it. Each read keeps its own serial fallback if
    # the batch is unavailable or a member misses (prefetch-else-run).
    _live_specs = [
        {"key": "oi", "sql": mart_sql.open_incidents(50, company),
         "source": f"INCIDENTS (open + mitigated, {company} + account-level)"},
        {"key": "cra", "sql": mart_sql.open_alert_events(500, company),
         "source": "ALERT_EVENTS" if company == "ALL"
                   else f"ALERT_EVENTS ({company} + account-level)"},
    ]
    if _is_op:
        _live_specs.append({"key": "props", "sql": mart_sql.incident_proposals(20, company),
                            "source": f"INCIDENT_PROPOSALS ({company} + account-level — a human confirms)"})
    _live_pf = run_batch(_live_specs, page=_PAGE, tier="live") or {}
    inc_met = run(mart_sql.incident_metrics(90, company), page=_PAGE,
                  key=f"inc_metrics_{company}", tier="recent",
                  source=f"INCIDENTS lifecycle (90d, {company} + account-level)")
    if inc_met.usable():
        im = inc_met.df.iloc[0]
        # v4.50: the 90d lifecycle medians (MTTA/MTTR, reopen, compression,
        # change-correlated) moved to Alerts > History — retrospective process
        # health, not morning triage. Only the number that changes overnight
        # stays on this screen.
        kpi_row([
            {"label": "Open incidents", "value": f"{safe_float(im.get('OPEN_NOW')):,.0f}",
             "severity": "bad" if safe_float(im.get("OPEN_NOW")) else "ok",
             "help": "Lifecycle metrics (MTTA/MTTR, reopen, compression) live on "
                     "Alerts -> History."},
        ])
    oi = _live_pf.get("oi") or run(mart_sql.open_incidents(50, company), page=_PAGE,
             key=f"open_incidents_{company}", tier="live",
             source=f"INCIDENTS (open + mitigated, {company} + account-level)")
    if oi.ok and oi.empty:
        st.success("No open incidents.")
    elif guard(oi, "", setup_hint="Incident tables are not installed yet — an admin can apply the pending schema update on Admin → Migrations & freshness."):
        sel_i = selectable_table(
            with_user_names(oi.df, _PAGE, user_col="DECLARED_BY", display_col="Declared by"),
            key="cr_inc_sel", height=190)
        if sel_i is not None:
            _iid = str(oi.df.iloc[int(sel_i)]["INCIDENT_ID"])
            mem = run(mart_sql.incident_members_detail(_iid), page=_PAGE,
                      key=f"inc_mem_{_iid[:8]}", tier="live", source="INCIDENT_MEMBERS")
            if guard(mem, "No members linked yet — link from the timeline drill or proposals."):
                styled_table(with_user_names(mem.df, _PAGE, user_col="LINKED_BY", display_col="Linked by"))
            if _is_op:
                with st.expander("Close this incident (audited, forward-only)"):
                    _kind = st.selectbox("Root cause",
                                         ["DEPLOY", "CONFIG_CHANGE", "DATA", "CAPACITY", "EXTERNAL", "UNKNOWN"],
                                         key=f"inc_rc_{_iid[:8]}")
                    _note = st.text_input("Root-cause note", key=f"inc_note_{_iid[:8]}", max_chars=500)
                    _close = _incident_close_sql(_iid, _kind, _note)
                    st.code(_close, language="sql")
                    _conf = st.text_input("Type RESOLVE to confirm", key=f"inc_conf_{_iid[:8]}")
                    if st.button("Execute close", key=f"inc_close_{_iid[:8]}",
                                 disabled=(_conf != "RESOLVE")):
                        ok, msg = execute_statement(_close, page=_PAGE)
                        notify(ok, msg)
                        if ok:
                            log_ui_event("incident_close", page=_PAGE)
    # Read proposals only for operators (they alone can act on them) — prefetched in
    # the live batch above when _is_op, else read serially here.
    props = (_live_pf.get("props") or run(mart_sql.incident_proposals(20, company), page=_PAGE,
                key=f"inc_props_{company}", tier="live",
                source=f"INCIDENT_PROPOSALS ({company} + account-level — a human confirms)")) if _is_op else None
    if _is_op and props is not None and props.usable():
        with st.expander(f"Proposed incidents ({len(props.df)}) — nothing groups silently"):
            styled_table(props.df)
            _pick = st.selectbox("Proposal", props.df["PROPOSAL_KEY"].astype(str).tolist(),
                                 key="inc_prop_pick")
            _prow = props.df[props.df["PROPOSAL_KEY"].astype(str) == _pick].iloc[0]
            _dec = _incident_declare_sql(str(_prow["SUGGESTED_TITLE"]), str(_prow["SEVERITY"]),
                                         str(_prow["COMPANY"]), _pick)
            st.code(_dec, language="sql")
            _confd = st.text_input("Type DECLARE to confirm", key="inc_prop_conf")
            if st.button("Declare incident + link alerts", key="inc_prop_exec", type="primary",
                         disabled=(_confd != "DECLARE")):
                _ok_all = True
                for _stmt in [s for s in _dec.split(";") if s.strip()]:
                    _ok, _m = execute_statement(_stmt + ";", page=_PAGE)
                    _ok_all = _ok_all and _ok
                notify(_ok_all, "Incident declared with members linked." if _ok_all
                       else "Declare failed — see the error log.")
                if _ok_all:
                    log_ui_event("incident_declare", page=_PAGE)
    st.caption("DBA-gated, audited, forward-only (reopen = new incident with REOPENED_FROM). "
               "CRITICALs auto-declare hourly — one incident per dedupe family per 24h — "
               "unless INCIDENT_AUTO_DECLARE_CRITICAL is off in Settings.")

    # ---- Triage queue ----------------------------------------------------------
    st.subheader("Triage queue")
    alerts = _live_pf.get("cra") or run(mart_sql.open_alert_events(500, company), page=_PAGE,
                 key=f"cr_alerts_{company}", tier="live",
                 source="ALERT_EVENTS" if company == "ALL"
                 else f"ALERT_EVENTS ({company} + account-level)")
    tasks = run(mart_sql.fact_task_daily(2, company, f["database"]), page=_PAGE, key=f"cr_tasks_{company}",
                tier="recent", source="FACT_TASK_DAILY")
    if not tasks.usable():
        tasks = run(ops_sql.task_runs(2, company, f["database"], f["schema_contains"]),
                    page=_PAGE, key=f"cr_tasks_live_{company}",
                    tier="recent", source="ACCOUNT_USAGE.TASK_HISTORY (live fallback)")

    wh_daily = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE,
                   key=f"cr_wh_{company}", tier="recent", source="FACT_WAREHOUSE_DAILY")
    anomalies: list[dict] = []
    if wh_daily.usable():
        flagged = flag_anomalies(
            complete_days_only(wh_daily.df)  # B4: don't score today's partial row
            .assign(USD=lambda d: d["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))),
            "USD", group_col="WAREHOUSE_NAME",
        )
        anomalies = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")

    queue = triage_queue(
        alerts.df if alerts.usable() else None,
        tasks.df if tasks.usable() else None,
        anomalies,
    )
    if queue.empty:
        # R3-1: wh_daily gates the spend-anomaly scan — if that read failed the
        # queue is empty for the WRONG reason, so a failed FACT_WAREHOUSE_DAILY must
        # demote the all-clear (a runaway-warehouse day must never hide behind green).
        sources_ok = alerts.ok and tasks.ok and wh_daily.ok
        if sources_ok:
            st.success("Nothing to triage: no open alerts, task failures, or spend anomalies in scope.")
        else:
            st.info("Triage inputs incomplete: "
                    + ("alert tables not installed; " if not alerts.ok else "")
                    + ("task facts not installed; " if not tasks.ok else "")
                    + ("warehouse spend fact unavailable — spend anomalies not scanned." if not wh_daily.ok else ""))
    else:
        styled_table(queue)
        st.caption(f"{len(queue)} item(s), ranked by severity. Sources: alerts, task facts, spend anomalies."
                   + (" Task failures follow the database filter; alerts and "
                      "spend anomalies don't have database grain." if f["database"] else ""))

    # ---- Incident correlation timeline -----------------------------------------
    st.subheader("Incident correlation timeline")
    panel_help(
        "Alerts, task failures, and DDL changes on one time axis (48h or 7d). Click a row "
        "below the chart to see everything else that happened within ±30 minutes — the "
        "'what changed right before this broke?' view."
    )
    if f["database"]:
        st.caption("Company events — the database filter doesn't apply here.")
    tl_win = st.radio("Window", ["48h (fresh)", "7d (cached hourly)"], horizontal=True,
                      key="cr_tl_win", label_visibility="collapsed",
                      help="Mid-incident you want fresh; the 7-day retrospective is the "
                           "heavy three-source join, so it caches for an hour.")
    tl_days, tl_tier = (2, "recent") if tl_win.startswith("48h") else (7, "historical")
    _tl_company = f["company"] if isinstance(f, dict) and "company" in f else "ALL"
    tl = None
    if tl_win.startswith("48h"):
        _tl_m = run(mart27_sql.incident_timeline(48, _tl_company), page=_PAGE,
                    key=f"incident_tl_mart_{_tl_company}", tier="recent",
                    source="MART_INCIDENT_TIMELINE (mart, rebuilt hourly)")
        if _tl_m.usable():
            tl = _tl_m
    if tl is None:
        tl = run(mart_sql.incident_timeline(tl_days, _tl_company),
                 page=_PAGE, key=f"incident_timeline_{tl_days}", tier=tl_tier,
                 source="ALERT_EVENTS + TASK_HISTORY + QUERY_HISTORY (DDL"
                        + (", live fallback)" if tl_win.startswith("48h") else ")"))
    if tl.ok and tl.empty:
        st.success("Quiet week: no alerts, task failures, or DDL in the window.")
    elif guard(tl, ""):
        tdf = tl.df.copy()
        tdf, tz_note = localize_timestamps(tdf, ["AT"])
        if tz_note:
            st.caption(tz_note)
        charts.event_timeline(tdf)
        sel_tl = selectable_table(tdf, key="cr_timeline_sel", height=240)
        if sel_tl is not None:
            anchor = tdf.iloc[sel_tl]
            try:
                at = pd.to_datetime(anchor["AT"])
                lo, hi = at - pd.Timedelta(minutes=30), at + pd.Timedelta(minutes=30)
                nearby = tdf[(pd.to_datetime(tdf["AT"]) >= lo) & (pd.to_datetime(tdf["AT"]) <= hi)]
                st.markdown(f"**±30 minutes around** `{anchor['LABEL']}` — {len(nearby)} event(s)")
                styled_table(nearby)
            except (KeyError, ValueError, TypeError) as exc:
                st.caption(f"±30 min window unavailable for this row — {type(exc).__name__}: "
                           f"{str(exc)[:120]} (usually a non-timestamp AT value from a new source).")
        result_caption(tl)

    _spk = run(mart27_sql.lock_wait_spikes(company, f["database"]), page=_PAGE,
               key=f"cr_lockspike_{company}", tier="recent",
               source="MART_LOCK_WAIT_DAILY (spikes)")
    if _spk.ok and not _spk.empty:
        st.subheader("Lock-wait spikes (last day vs prior 6-day avg)")
        styled_table(_spk.df, height=180)
        st.caption("Objects with >=5 waits last day and >3x their own baseline — the Operations "
                   "Warehouses section has the full table and history.")

    st.subheader("Spend movers (window vs prior)")
    if f["database"]:
        st.caption("Warehouse grain — the database filter doesn't narrow this.")
    movers = run(mart_sql.fact_warehouse_window_vs_prior(days, company), page=_PAGE,
                 key=f"cr_movers_fact_{company}_{days}", tier="recent",
                 source="FACT_WAREHOUSE_DAILY (window vs prior, loaded hourly)")
    if not movers.usable():  # mart not deployed/loaded yet -> bounded live scan
        movers = run(cost_sql.warehouse_window_vs_prior(days, company), page=_PAGE,
                     key=f"cr_movers_{company}_{days}", tier="historical",
                     source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (live fallback)")
    if guard(movers, "No warehouse spend to compare in this window."):
        view = movers.df.copy()
        view["USD_CURRENT"] = view["CREDITS_CURRENT"].map(lambda c: credits_to_usd(c, rate))
        view["USD_PRIOR"] = view["CREDITS_PRIOR"].map(lambda c: credits_to_usd(c, rate))
        view["DELTA_USD"] = view["USD_CURRENT"] - view["USD_PRIOR"]
        view["DELTA_PCT"] = view.apply(lambda r: pct_delta(r["USD_CURRENT"], r["USD_PRIOR"]), axis=1)
        view = view.reindex(view["DELTA_USD"].abs().sort_values(ascending=False).index).head(10)
        st.dataframe(
            view[["WAREHOUSE_NAME", "COMPANY", "USD_CURRENT", "USD_PRIOR", "DELTA_USD", "DELTA_PCT"]],
            hide_index=True, use_container_width=True,
            column_config={
                "USD_CURRENT": st.column_config.NumberColumn("Current $", format="$%.0f"),
                "USD_PRIOR": st.column_config.NumberColumn("Prior $", format="$%.0f"),
                "DELTA_USD": st.column_config.NumberColumn("Δ $", format="$%.0f"),
                "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            },
        )
        total_delta = float(view["DELTA_USD"].sum())
        st.caption(f"Net movement across top movers: {format_usd(total_delta)}.")
        result_caption(movers)

    _freshness_board()
    st.divider()
    # r19 #2: six reads for a bottom-of-page feature ran on every rerun —
    # the flight recorder now loads only when asked (results stay cached).
    if st.toggle("Load day replay", key="cr_replay_on",
                 help="Six reads across spend, activity, DDL, grants, tasks, and "
                      "alerts for one chosen day. Loads when on; caches after."):
        _day_replay()
