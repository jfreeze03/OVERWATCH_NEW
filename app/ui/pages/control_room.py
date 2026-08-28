"""Control Room — DBA morning triage on one screen.

Ranked queue (alerts + task failures + spend anomalies), telemetry freshness,
the since-yesterday operations pulse, and spend movers. No button maze: the
queue is visible on entry.
"""

from __future__ import annotations

import contextlib

import pandas as pd
import streamlit as st

from app.config import THRESHOLDS
from app.core.errors import safe_page
from app.core.query import run, run_batch, run_batch_mixed
from app.core.state import filters, navigation_context, request_navigation
from app.data import cost_sql, mart27_sql, mart_sql, ops_sql, security_sql
from app.logic.actions import ANOMALY_HIGH_EXCESS_USD, ANOMALY_HIGH_Z, triage_queue
from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    DEFAULT_THRESHOLD,
    anomaly_summary,
    complete_days_only,
    flag_anomalies,
    suppress_expected_spikes,
)
from app.logic.formulas import (
    account_now,
    credits_to_usd,
    format_usd,
    humanize_age,
    humanize_duration,
    humanize_gb,
    md_dollars,
    pct_delta,
    safe_float,
)
from app.logic.verdict import Signal, page_verdict
from app.ui import charts
from app.ui.components import (
    confirm_gate,
    exception_summary,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    localize_timestamps,
    nested_sections,
    page_header,
    page_verdict_line,
    panel_help,
    read_model_caption,
    result_caption,
    run_mart_first,
    section_filter_contract,
    section_header,
    selectable_nav_table,
    selectable_table,
    stamp_write,
    styled_table,
    with_user_names,
    write_gate_open,
)
from app.ui.workbench import render_action_center, render_entity_360, render_watchlist

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
    proposal_parts = str(proposal_key).split("|", 3)
    fam = sql_literal(proposal_parts[0])
    entity_filter = ""
    if len(proposal_parts) == 4 and proposal_parts[2].upper() != "ACCOUNT":
        entity_filter = (
            "AND UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = "
            f"UPPER({sql_literal(proposal_parts[3])}) "
        )
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
        f"{entity_filter}"
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


def _auto_investigation(inc_row, company: str, rate: float) -> None:
    """Incidents that investigate themselves (read-only): for the selected incident,
    assemble the change / task-failure / spend-anomaly / grant signals around its onset
    and RANK candidate root causes by timing, magnitude, and entity match (app/logic/rca.py).
    It explains — it never writes an INCIDENT_MEMBERS row, never executes a remediation."""
    from app.data import change_impact_sql, insights_sql
    from app.logic.insights import build_failure_timeline
    from app.logic.rca import (
        candidates_from_anomalies,
        candidates_from_changes,
        candidates_from_grants,
        candidates_from_tasks,
        rank_root_causes,
        rca_summary,
    )

    onset_dt = pd.to_datetime(inc_row.get("STARTED_AT") or inc_row.get("DETECTED_AT"), errors="coerce")
    if pd.isna(onset_dt):
        return
    section_header("Auto-investigation — ranked root cause", "info", "incident")
    st.caption("Read-only synthesis: the changes, task failures, spend anomalies and grant "
               "changes around this incident's onset, ranked as candidate causes by timing "
               "(a trigger precedes onset), magnitude, and entity match. It explains — it never "
               "remediates.")
    # Window-covering lookback: a couple of days of lead-in before onset, through now.
    _days = max(3, min((pd.Timestamp(account_now()) - onset_dt).days + 3, 30))
    _b = run_batch([
        {"key": "ai_obj", "sql": change_impact_sql.change_registry(_days, company),
         "source": "OBJECT_CHANGE_REGISTRY"},
        {"key": "ai_wh", "sql": change_impact_sql.warehouse_change_registry(_days, company),
         "source": "WAREHOUSE_CHANGE_REGISTRY"},
        {"key": "ai_task", "sql": insights_sql.task_failure_details(_days, company),
         "source": "TASK_HISTORY failures"},
        {"key": "ai_grant", "sql": security_sql.recent_grant_changes(_days, company),
         "source": "GRANTS_TO_USERS changes"},
        {"key": "ai_whd", "sql": mart_sql.fact_warehouse_daily(max(_days, 14), company),
         "source": "FACT_WAREHOUSE_DAILY (spend anomaly)"},
    ], page=_PAGE, tier="recent")
    if _b is None:
        st.caption("Investigation feeds could not be read.")
        return

    cands: list = []
    _obj, _wh = _b.get("ai_obj"), _b.get("ai_wh")
    _task, _grant, _whd = _b.get("ai_task"), _b.get("ai_grant"), _b.get("ai_whd")
    if _obj is not None and _obj.usable():
        cands += candidates_from_changes(_obj.df)
    if _wh is not None and _wh.usable():
        cands += candidates_from_changes(_wh.df)
    if _task is not None and _task.usable():
        cands += candidates_from_tasks(build_failure_timeline(_task.df))
    if _grant is not None and _grant.usable():
        cands += candidates_from_grants(_grant.df)
    if _whd is not None and _whd.usable():
        _priced = complete_days_only(_whd.df.copy())
        _priced["USD"] = pd.to_numeric(_priced.get("CREDITS_TOTAL"), errors="coerce").fillna(0.0) * rate
        _flagged = flag_anomalies(_priced, "USD", group_col="WAREHOUSE_NAME",
                                  min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
        cands += candidates_from_anomalies(anomaly_summary(_flagged, "WAREHOUSE_NAME", "USD"))

    hyps = rank_root_causes(cands, onset_dt, top=5)
    summ = rca_summary(hyps)
    _banner = st.info if not summ["has_lead"] else (st.warning if summ["top_band"] == "MEDIUM" else st.error)
    _banner(md_dollars(summ["headline"]))
    if hyps:
        _rows = pd.DataFrame([{
            "Confidence": h["band"], "Hypothesis": h["title"], "When": h["lead_text"],
            "Magnitude": h["magnitude_text"], "By": h["changed_by"], "Why (scoring)": h["why"],
        } for h in hyps])
        styled_table(_rows, height=min(60 + 34 * len(hyps), 260))
        st.caption("Ranked by 0.45·timing + 0.35·magnitude + 0.20·entity-match. A change outside "
                   "the ~48h pre-onset window can't be a confident cause however large. Confirm the "
                   "lead, then close the incident with its root cause above.")
        # Grounded-AI narrative (button-gated, credit-warned): a plain-English read
        # of the ranked evidence above for the responder. The prompt embeds exactly
        # these hypotheses — the model never sees data the DBA can't — and it is told
        # to explain, not remediate, so the feature stays read-only end to end.
        from app.logic.ai_prompts import incident_narrative_prompt
        from app.ui.ai_panel import ai_evaluation_panel
        _iid = str(inc_row.get("INCIDENT_ID") or "inc")
        ai_evaluation_panel(
            key=f"rca_{_iid[:12]}",
            prompt=incident_narrative_prompt(inc_row, hyps, summ),
            settings=load_settings(_PAGE),
            page=_PAGE,
            subject=f"root cause · {str(inc_row.get('TITLE') or 'incident')[:60]}",
        )


def _day_replay() -> None:
    """One day, every domain, one story — the flight recorder Snowsight
    can't assemble from its silos."""
    from datetime import timedelta

    from app.logic.formulas import account_today
    from app.logic.replay import replay_headlines

    section_header("Day replay — what changed?")
    pick = st.date_input("Day", value=account_today() - timedelta(days=1),
                         min_value=account_today() - timedelta(days=120),
                         max_value=account_today(), key="cr_replay_day")
    day_iso = pick.isoformat()
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    rp_company = filters()["company"]
    # PERF #58: six independent reads across TWO tiers (recent + historical) now gather in ONE
    # mixed-tier parallel batch instead of two round trips; any batch failure falls back to the
    # original serial per-query path below (also the path the stress harness forces).
    _b = run_batch_mixed([
        {"key": "mv", "tier": "recent", "sql": mart_sql.day_spend_movers(day_iso, rp_company),
         "source": "FACT_WAREHOUSE_DAILY vs 14d baseline"},
        {"key": "act", "tier": "recent", "sql": mart_sql.day_activity(day_iso, rp_company),
         "source": "FACT_QUERY_HOURLY (day vs baseline)"},
        {"key": "tf", "tier": "recent", "sql": mart_sql.day_task_failures(day_iso, rp_company),
         "source": "FACT_TASK_DAILY (failures that day)"},
        {"key": "al", "tier": "recent", "sql": mart_sql.day_alerts(day_iso, rp_company),
         "source": "ALERT_EVENTS (that day)"},
        {"key": "ddl", "tier": "historical", "sql": security_sql.day_ddl(day_iso, rp_company),
         "source": "QUERY_HISTORY (DDL that day)", "max_rows": 300},
        {"key": "gr", "tier": "historical", "sql": security_sql.day_grants(day_iso, rp_company),
         "source": "GRANTS_TO_USERS (that day)", "max_rows": 200},
    ], page=_PAGE) or {}
    if all(_b.get(k) is not None for k in ("mv", "act", "tf", "al", "ddl", "gr")):
        movers, activity = _b["mv"], _b["act"]
        tasks, alerts_d = _b["tf"], _b["al"]
        ddl, grants = _b["ddl"], _b["gr"]
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
    section_header("Telemetry freshness")
    if not res.ok:
        st.info("Freshness board is not installed yet; the live fallbacks on this page still work.")
        return
    if res.empty:
        st.info("Freshness view exists but has no rows — have the loader tasks run yet?")
        return
    df = res.df.copy()
    # C3: a source that has NEVER loaded comes back with a NULL LAST_LOAD_TS →
    # NULL HOURS_SINCE_LOAD → safe_float 0, which would render as "0.0h, fresh".
    # A source with no data is not fresh — it is the most broken state there is.
    df["NOT_LOADED"] = df["LAST_LOAD_TS"].isna()
    df["HOURS_SINCE_LOAD"] = df["HOURS_SINCE_LOAD"].map(safe_float)

    def _status(row) -> str:
        if row["NOT_LOADED"]:
            return "NOT LOADED"
        limit = (THRESHOLDS["stale_daily_fact_hours"]
                 if "DAILY" in str(row["SOURCE_NAME"]) or "METERING" in str(row["SOURCE_NAME"])
                 else THRESHOLDS["stale_fact_hours"])
        return "STALE" if row["HOURS_SINCE_LOAD"] > limit else "OK"

    df["STATUS"] = df.apply(_status, axis=1)
    not_loaded = int(df["NOT_LOADED"].sum())
    stale_count = int((df["STATUS"] == "STALE").sum())
    if not_loaded:
        st.error(f"{not_loaded} source(s) have NEVER loaded — their panels are blank or wrong "
                 "until the loader task runs. Check Admin → Migrations & freshness.")
    if stale_count:
        st.warning(f"{stale_count} source(s) stale — numbers built on them are labeled accordingly.")
    if not (not_loaded or stale_count):
        st.success("All telemetry sources fresh.")
    # Admin ▸ Migrations & freshness owns the full per-source freshness table; this
    # is just the at-a-glance count, so link there instead of repeating the table.
    if st.button("Full freshness table → Admin", key="cr_freshness_admin"):
        request_navigation("Admin", "Migrations & freshness")


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    page_header("Control Room", "Morning triage: what broke, what's burning, what's stale.", icon_name="control",
                scope_note=f"{company} · {f['window_label']}"
                           + (f" · {f['database']}" if f["database"] else ""))

    # N2: undelivered-critical banner up top — the DBA's first triage question is
    # "did last night's page actually reach anyone?". Rides the shell-shared
    # health_strip cache entry (same SQL + key as the sidebar), zero extra queries.
    # Perf: 'recent' (300s) to match the other health_strip sites — see main.py.
    _strip = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="recent",
                 source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    _sv: dict[str, str] = {}
    _open_crit = 0
    if _strip.ok and not _strip.empty:
        _sv = {str(r["METRIC"]): str(r["VALUE"]) for _, r in _strip.df.iterrows()}
        _und = int(safe_float(_sv.get("UNDELIVERED_CRITICAL", "0")))
        _und_age = safe_float(_sv.get("UNDELIVERED_OLDEST_MIN", "0"))
        _und_age_txt = f", oldest {humanize_duration(_und_age, 'min')}" if _und_age > 0 else ""
        if _und and st.button(f"⚠ {_und} critical alert(s) reached nobody (account-wide; 30+ min, "
                              f"no delivery{_und_age_txt}) — check delivery →", key="cr_undelivered",
                              type="primary", width="stretch"):
            request_navigation("Alerts", "Native delivery")
    # The shell strip is intentionally account-wide. The page badge must match the
    # company-scoped incident/alert queue it opens (company + account-level events).
    _crit_counts = run(
        mart_sql.open_alert_severity_counts(company),
        page=_PAGE,
        key=f"cr_alert_counts_{company}",
        tier="live",
        source=f"ALERT_EVENTS counts ({company} + account-level)",
    )
    _crit_known = _crit_counts.usable()
    if _crit_known:
        _open_crit = int(safe_float(_crit_counts.df.iloc[0].get("CRIT")))

    # rec11: badge the Incidents pill with open criticals — already in the health strip
    # the shell fetched (zero extra queries), same precedent as Alerts "Open events (N)".
    # rec8: Decision Studio moved out to its own Analyze page — Control Room is now the
    # pure triage console (Entity 360 stays; it is the drill target for cross-jumps).
    # CoCo do-first #1: a page-level "should I worry?" opener from the health strip
    # the shell already fetched (zero extra queries) + the scoped critical count.
    _vsig = []
    if _crit_known and _open_crit:
        _vsig.append(Signal("bad", f"{_open_crit} open critical alert(s)"))
    _und_n = int(safe_float(_sv.get("UNDELIVERED_CRITICAL", "0")))
    if _und_n:
        _vsig.append(Signal("bad", f"{_und_n} critical(s) reached nobody"))
    _stale_n = int(safe_float(_sv.get("STALE_SOURCES", "0")))
    if _stale_n:
        _vsig.append(Signal("warn", f"{_stale_n} stale telemetry source(s)"))
    if not (_strip.ok and not _strip.empty):
        _vsig.append(Signal("warn", "health telemetry unavailable"))
    page_verdict_line(page_verdict(
        _vsig, healthy="no open criticals, delivery clear, telemetry fresh"))

    section = lazy_sections(["Action Center", "Pulse", "Incidents & triage",
                             "Timeline & movers", "Freshness & replay", "Entity 360"],
                            key="cr_section",
                            counts={"Incidents & triage": _open_crit} if _crit_known else None)
    _contracts = {
        "Action Center": {
            "applies": (),
            "partial": ("company",),
            "note": "Persistent work is account-wide; Company includes matching and account-level actions.",
        },
        "Pulse": {
            "applies": ("company",),
            "partial": ("database", "schema_contains"),
            "note": "Database and Schema narrow query health only; the horizon is fixed since yesterday.",
        },
        "Incidents & triage": {
            "applies": ("company",),
            "partial": ("database", "schema_contains"),
            "note": "Database and Schema narrow task evidence, not incident/alert records.",
        },
        "Timeline & movers": {
            "applies": ("company",),
            "partial": ("days", "database"),
            "note": "Window shapes spend movers; timeline uses its local 48h/7d control.",
        },
        "Freshness & replay": {
            "applies": (),
            "partial": ("company", "database"),
            "note": "Freshness is account-wide; replay uses its own date and scoped evidence where available.",
        },
        "Entity 360": {
            "applies": (),
            "partial": ("company", "days"),
            "note": "Identity and ownership are exact; evidence panels declare their own metric scope.",
        },
    }
    section_filter_contract(f, **_contracts[section])

    if section == "Action Center":
        render_action_center(company)

    elif section == "Pulse":
        # ---- pulse (since yesterday 00:00) ---------------------------------------
        # Fact-first pulse (Codex #4): the hourly fact answers this without
        # a live QUERY_HISTORY scan; schema filter (no schema grain) or an empty
        # fact falls back to live, exactly like the Operations page. Triage #12:
        # all three feeding builders anchor on CURRENT_DATE, so days=1 covers
        # yesterday 00:00 -> now (24-48h) — labels say "since yday", not "24h".
        pulse, pulse_from_mart = None, False
        if not f["schema_contains"]:
            m_pulse = run(mart_sql.fact_query_window_summary(1, company, "", "", f["database"]),
                          page=_PAGE, key=f"pulse_fact_{company}", tier="hourly",
                          source="FACT_QUERY_HOURLY (mart, loaded hourly)")
            if m_pulse.ok and not m_pulse.empty and safe_float(m_pulse.df.iloc[0].get("QUERY_COUNT")) > 0:
                pulse, pulse_from_mart = m_pulse, True
        else:
            s_pulse = run(mart27_sql.schema_window_summary(1, company, f["database"], f["schema_contains"]),
                          page=_PAGE, key=f"pulse_schema_fact_{company}", tier="hourly",
                          source="FACT_QUERY_SCHEMA_HOURLY (mart — p95 is peak hourly)")
            if s_pulse.ok and not s_pulse.empty and safe_float(s_pulse.df.iloc[0].get("QUERY_COUNT")) > 0:
                pulse, pulse_from_mart = s_pulse, True
        if pulse is None:
            pulse = run(ops_sql.query_window_summary(1, company, database=f["database"], schema_contains=f["schema_contains"]),
                        page=_PAGE, key=f"pulse_{company}",
                        tier="live", source="ACCOUNT_USAGE.QUERY_HISTORY (since yesterday 00:00)")
        act = run(mart_sql.fact_daily_activity(14, company, f["database"]), page=_PAGE,
                  key="cr_activity", tier="hourly", source="FACT_QUERY_HOURLY (daily)")
        _activity_cols = {"DAY", "QUERIES", "FAILS"}
        _activity_ready = act.usable() and _activity_cols.issubset(act.df.columns)
        q_spark = act.df["QUERIES"].tolist() if _activity_ready else None
        f_spark = act.df["FAILS"].tolist() if _activity_ready else None
        # CR3: day-over-day vs-prior delta on the Queries tile, from the already-
        # loaded activity frame (no new query). Complete days only, so it is
        # full-day-over-full-day (the tile value is the partial since-yday pulse);
        # hidden when the prior day is zero (pct_delta -> None).
        _q_delta = None
        if _activity_ready:
            from app.logic.formulas import account_today
            _ca = act.df[pd.to_datetime(act.df["DAY"], errors="coerce").dt.date < account_today()]
            if len(_ca) >= 2:
                _qd = pct_delta(safe_float(_ca["QUERIES"].iloc[-1]),
                                safe_float(_ca["QUERIES"].iloc[-2]))
                if _qd is not None:
                    _q_delta = f"{_qd:+,.0f}% vs prior day"
        if pulse.usable():
            row = pulse.df.iloc[0]
            qcount = safe_float(row.get("QUERY_COUNT"))
            failed = safe_float(row.get("FAILED_COUNT"))
            fail_pct = (failed / qcount * 100) if qcount else None
            _fail_bad = bool(qcount and failed / qcount > 0.02)
            queue_sec = safe_float(row.get("QUEUED_SEC"))
            remote_spill_gb = safe_float(row.get("SPILL_REMOTE_GB"))
            read_model_caption("control_pulse")
            exceptions = []
            if qcount <= 0:
                exceptions.append({
                    "label": "Query evidence",
                    "value": "Unavailable",
                    "detail": "No query denominator exists for this scope, so health cannot be cleared.",
                    "severity": "warn",
                })
            if failed:
                exceptions.append({
                    "label": "Failed queries",
                    "value": f"{failed:,.0f}",
                    "detail": (f"{fail_pct:.1f}% of this scope." if fail_pct is not None
                               else "No query denominator is available."),
                    "severity": "bad" if _fail_bad else "warn",
                })
            if queue_sec >= 60:
                exceptions.append({
                    "label": "Queue pressure",
                    "value": humanize_duration(queue_sec, "s"),
                    "detail": "Aggregate queued time is at least one minute since yesterday.",
                    "severity": "warn",
                })
            if remote_spill_gb > 0:
                exceptions.append({
                    "label": "Remote spill",
                    "value": humanize_gb(remote_spill_gb),
                    "detail": "Queries spilled beyond local storage in this scope.",
                    "severity": "warn",
                })
            exception_summary(
                exceptions,
                "No failed queries, material queue pressure, or remote spill since yesterday.",
            )
            kpi_row([
                {"label": "Queries (since yday)", "value": f"{qcount:,.0f}", "spark": q_spark,
                 "delta": _q_delta, "delta_color": "off",   # CR3: day-over-day, neutral direction
                 "help": "Midnight-anchored: yesterday 00:00 to now (24-48h depending on "
                         "time of day) — the same span on the mart and live paths. Delta = "
                         "last complete day vs the one before."},
                {"label": "Failed", "value": f"{failed:,.0f}",
                 "delta": f"{fail_pct:.1f}%" if fail_pct is not None else "No query denominator",
                 "delta_color": "inverse" if _fail_bad else "off",
                 "severity": "bad" if _fail_bad else ("ok" if fail_pct is not None else ""),
                 "spark": f_spark},
                {"label": "p95 runtime" + (" (peak hourly)" if pulse_from_mart else ""),
                 "value": humanize_duration(row.get("P95_ELAPSED_SEC"), "s")},
                {"label": "Queued", "value": humanize_duration(queue_sec, "s")},
                {"label": "Remote spill", "value": humanize_gb(remote_spill_gb)},
            ])
            result_caption(pulse)
        elif not pulse.ok:
            st.error(f"Pulse unavailable: {pulse.error}")
        else:
            st.info("No queries recorded since yesterday 00:00 for this scope.")
        # The pulse is the distinct "since yesterday" glance; Operations ▸ Queries
        # owns the full query metrics (and their definitions), so link there rather
        # than repeat them here.
        if st.button("Full query metrics → Operations", key="cr_pulse_ops"):
            request_navigation("Operations", "Queries")

        section_header("14-day query activity", "info", "operations")
        if _activity_ready:
            c_queries, c_fails = st.columns(2)
            with c_queries:
                charts.daily_metric_line(act.df, "DAY", "QUERIES", "Queries")
            with c_fails:
                charts.daily_metric_line(act.df, "DAY", "FAILS", "Failed queries")
            result_caption(act)
        elif not act.ok:
            st.warning(f"Activity trend unavailable: {act.error}")
        elif act.usable():
            st.warning("Activity trend returned an unexpected data shape.")
        else:
            st.info("No query activity recorded in the last 14 days for this scope.")

    elif section == "Incidents & triage":
        # ---- Incidents (V032) ------------------------------------------------------
        section_header("Incidents")
        from app.core.query import execute_statement, run_batch
        from app.core.session import is_operator
        from app.ui.components import log_ui_event, notify
        # correctness #3: entitle operator UI from the VIEWER identity, not
        # CURRENT_ROLE() — under owner's-rights SiS the latter is the app owner's
        # role for every viewer, so it never differentiates people.
        _is_op = is_operator()
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
        # rec47: a slow cold first paint runs these live mart reads behind Streamlit's
        # bare skeleton, which reads as a hang. Wrap the ONE heaviest prefetch batch in a
        # collapsed st.status so the wait reads as progress; degrade to a no-op context on
        # Streamlit builds without st.status (same hasattr degrade pattern as the nav bar).
        _load_status = (st.status(f"Loading Control Room — {len(_live_specs)} mart reads…",
                                  expanded=False)
                        if hasattr(st, "status") else contextlib.nullcontext())
        with _load_status:
            _live_pf = run_batch(_live_specs, page=_PAGE, tier="live") or {}
        inc_met = run(mart_sql.incident_metrics(90, company), page=_PAGE,
                      key=f"inc_metrics_{company}", tier="recent",
                      source=f"INCIDENTS lifecycle (90d, {company} + account-level)")
        # rec10: lead the section with the house exception-first summary — the DBA's
        # first triage question is "what needs me", answered from numbers already in
        # hand (health strip + incident metrics), zero extra queries.
        _open_now = int(safe_float(inc_met.df.iloc[0].get("OPEN_NOW"))) if inc_met.usable() else 0
        _exc = []
        if _open_crit:
            _exc.append({"label": "Open criticals", "value": f"{_open_crit:,}",
                         "detail": "Open CRITICAL alert events — in the triage queue below.",
                         "severity": "bad"})
        if _open_now:
            _exc.append({"label": "Open incidents", "value": f"{_open_now:,}",
                         "detail": "Declared incidents still open.", "severity": "bad"})
        _stale = int(safe_float(_sv.get("STALE_SOURCES", "0")))
        if _stale:
            _exc.append({"label": "Stale sources", "value": f"{_stale:,}",
                         "detail": "Feeds past their freshness SLA — see Freshness & replay.",
                         "severity": "warn"})
        exception_summary(_exc, "No open criticals, open incidents, or stale sources.")
        # v4.50: the 90d lifecycle medians (MTTA/MTTR, reopen, compression,
        # change-correlated) moved to Alerts > History — retrospective process
        # health, not morning triage. Open incidents now surfaces once, via the
        # exception summary above (OPEN_NOW), so the standalone KPI is dropped.
        # CR5: a lifecycle Gantt — detected->resolved spans (open runs to now), so
        # the shape of the last 14 days of incidents reads at a glance.
        # Pass account-time now (minute-rounded so the SQL-keyed cache doesn't churn
        # every render) so an OPEN incident's live duration measures against account
        # time, not the server's UTC CURRENT_TIMESTAMP() (unset account TZ = a false
        # multi-hour bar).
        _ig = run(mart_sql.incident_gantt(14, company,
                                          account_now().replace(second=0, microsecond=0).isoformat()),
                  page=_PAGE, key=f"incident_gantt_{company}", tier="recent",
                  source="INCIDENTS (14d lifecycle spans)")
        if _ig.usable():
            st.caption("Recent incidents (14d) — bar = detected → resolved; an open incident's bar "
                       "reaches now. Account time.")
            charts.incident_gantt(_ig.df)
        oi = _live_pf.get("oi") or run(mart_sql.open_incidents(50, company), page=_PAGE,
                 key=f"open_incidents_{company}", tier="live",
                 source=f"INCIDENTS (open + mitigated, {company} + account-level)")
        if oi.ok and oi.empty:
            st.success("No open incidents.")
        elif guard(oi, "", setup_hint="Incident tables are not installed yet — an admin can apply the pending schema update on Admin → Migrations & freshness."):
            sel_i = selectable_table(
                with_user_names(oi.df, _PAGE, user_col="DECLARED_BY", display_col="Declared by"),
                key="cr_inc_sel", height=190)
            requested_incident = str(
                navigation_context().get("incident_id") or ""
            ).strip()
            if sel_i is None and requested_incident:
                matches = [
                    index for index, value in enumerate(oi.df["INCIDENT_ID"].astype(str))
                    if value == requested_incident
                ]
                sel_i = matches[0] if matches else None
            if sel_i is not None:
                _iid = str(oi.df.iloc[int(sel_i)]["INCIDENT_ID"])
                mem = run(mart_sql.incident_members_detail(_iid), page=_PAGE,
                          key=f"inc_mem_{_iid[:8]}", tier="live", source="INCIDENT_MEMBERS")
                if guard(mem, "No members linked yet — link from the timeline drill or proposals."):
                    def _open_member(_mi: int) -> None:
                        # A linked ALERT member's REF_ID is the ALERT_EVENTS EVENT_ID —
                        # carry it so the click lands on that event's drawer (the Alerts
                        # drawer self-selects on an event_id context). Non-ALERT kinds
                        # have no obvious drill, so a click on them stays inert. The
                        # sticky-selection guard lives in selectable_nav_table (per key).
                        _mrow = mem.df.iloc[int(_mi)]
                        if str(_mrow.get("MEMBER_KIND") or "").strip() == "ALERT":
                            _mref = str(_mrow.get("REF_ID") or "").strip()
                            if _mref:
                                request_navigation("Alerts", "Open events",
                                                   context={"event_id": _mref})
                    selectable_nav_table(
                        with_user_names(mem.df, _PAGE, user_col="LINKED_BY", display_col="Linked by"),
                        key=f"cr_inc_mem_sel_{_iid[:8]}", on_select=_open_member)
                # Incidents that investigate themselves: the auto-assembled ranked root cause.
                _auto_investigation(oi.df.iloc[int(sel_i)], company, rate)
                if _is_op:
                    with st.expander("Close this incident (audited, forward-only)"):
                        _kind = st.selectbox("Root cause",
                                             ["DEPLOY", "CONFIG_CHANGE", "DATA", "CAPACITY", "EXTERNAL", "UNKNOWN"],
                                             key=f"inc_rc_{_iid[:8]}")
                        _note = st.text_input("Root-cause note", key=f"inc_note_{_iid[:8]}", max_chars=500)
                        _close = _incident_close_sql(_iid, _kind, _note)
                        st.code(_close, language="sql")
                        if confirm_gate("RESOLVE", "Execute close", key=f"inc_close_{_iid[:8]}",
                                        prompt="Type RESOLVE to confirm"
                                        ) and write_gate_open(f"inc_close_{_iid[:8]}"):
                            ok, msg = execute_statement(_close, page=_PAGE)
                            stamp_write(f"inc_close_{_iid[:8]}", ok)  # C48
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
                _proposal_columns = [
                    "PROPOSAL_KEY", "SUGGESTED_TITLE", "SEVERITY", "COMPANY",
                    "ENTITY_KIND", "ENTITY_NAME", "CONFIDENCE", "ALERTS",
                    "MATCHED_WH_CHANGES", "MATCHED_OBJECT_CHANGES",
                    "MATCHED_TASK_FAILURES", "FIRST_TS", "LAST_TS", "EVIDENCE",
                ]
                styled_table(props.df[[c for c in _proposal_columns if c in props.df.columns]])
                _pick = st.selectbox("Proposal", props.df["PROPOSAL_KEY"].astype(str).tolist(),
                                     key="inc_prop_pick")
                _prow = props.df[props.df["PROPOSAL_KEY"].astype(str) == _pick].iloc[0]
                _entity_kind = str(_prow.get("ENTITY_KIND", "family") or "family")
                _entity_name = str(_prow.get("ENTITY_NAME", "account") or "account")
                _confidence = str(_prow.get("CONFIDENCE", "legacy") or "legacy")
                _evidence = str(_prow.get("EVIDENCE", "Alert-family correlation only.")
                                or "Alert-family correlation only.")
                st.caption(
                    f"Scope: {_entity_kind} {_entity_name} | confidence {_confidence}. "
                    f"Evidence: {_evidence} Human confirmation is still required."
                )
                _dec = _incident_declare_sql(str(_prow["SUGGESTED_TITLE"]), str(_prow["SEVERITY"]),
                                             str(_prow["COMPANY"]), _pick)
                st.code(_dec, language="sql")
                if confirm_gate("DECLARE", "Declare incident + link alerts", key="inc_prop_exec",
                                prompt="Type DECLARE to confirm", type="primary"
                                ) and write_gate_open("inc_prop_exec"):
                    _ok_all = True
                    for _stmt in [s for s in _dec.split(";") if s.strip()]:
                        _ok, _m = execute_statement(_stmt + ";", page=_PAGE)
                        _ok_all = _ok_all and _ok
                        if not _ok:
                            # Stop at the first failure — running INCIDENT_MEMBERS
                            # after a failed INCIDENTS insert half-applies the declare.
                            break
                    stamp_write("inc_prop_exec", _ok_all)  # C48
                    notify(_ok_all, "Incident declared with members linked." if _ok_all
                           else "Declare failed — see the error log.")
                    if _ok_all:
                        log_ui_event("incident_declare", page=_PAGE)
        st.caption("DBA-gated, audited, forward-only (reopen = new incident with REOPENED_FROM). "
                   "CRITICALs auto-declare hourly — one incident per dedupe family per 24h — "
                   "unless INCIDENT_AUTO_DECLARE_CRITICAL is off in Settings.")

        # ---- Triage queue ----------------------------------------------------------
        section_header("Triage queue")
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
        _base_median_usd = 0.0      # median of the per-warehouse baselines, for the caption
        _thin_warehouses = 0        # warehouses with too few days to score (see below)
        if wh_daily.usable():
            _wh_complete = (complete_days_only(wh_daily.df)  # B4: don't score today's partial row
                            .assign(USD=lambda d: d["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))))
            flagged = flag_anomalies(_wh_complete, "USD", group_col="WAREHOUSE_NAME",
                                     min_value=ANOMALY_MIN_USD,
                                     min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
            # Known-spike calendar: same suppression as the Cost sweep, so the same
            # day never reads "expected" there but anomalous in this triage queue.
            flagged = suppress_expected_spikes(
                flagged, str(settings.get("EXPECTED_SPIKE_CALENDAR") or ""))
            anomalies = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")
            # D6: anomaly_summary carries the z but not the BASELINE, and a z alone is
            # $-blind — z=8 on a $12/day sandbox outranked z=4 on a $9k/day production
            # warehouse. Attach each hit's excess over its own robust (median) baseline
            # — the exact baseline robust_zscores used — so triage_queue can escalate
            # and rank on money. Signed: a collapse comes through negative, and
            # triage_queue takes the magnitude.
            _med = _wh_complete.groupby("WAREHOUSE_NAME")["USD"].median()
            for _a in anomalies:
                _a["excess_usd"] = safe_float(_a.get("value")) - safe_float(_med.get(_a.get("label")))
            _base_median_usd = safe_float(_med.median()) if len(_med) else 0.0
            # E5: robust_zscores returns all-zero for a series with <5 points, so a
            # warehouse created this week CANNOT be flagged however wild its spend.
            # That exclusion has to be visible, or the all-clear over-promises.
            _day_counts = _wh_complete.groupby("WAREHOUSE_NAME")["DAY"].nunique()
            _thin_warehouses = int((_day_counts < 5).sum())
            # r6-bug5: only surface anomalies from the MOST RECENT complete day. flag_anomalies
            # is MAD-based, so a one-off spike keeps |z| high for the whole trailing-30d frame;
            # without this the SAME 30-day-old spike re-fired as an undismissable, dateless HIGH
            # every morning, indistinguishable from an overnight break. anomaly_summary now
            # carries each hit's day, so the triage feed can age stale spikes out.
            if anomalies and "DAY" in _wh_complete.columns and not _wh_complete.empty:
                _latest = str(_wh_complete["DAY"].max())
                anomalies = [a for a in anomalies if str(a.get("day") or "") == _latest]

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
            # N3: the DBA's one morning list is now actionable — select a row to jump
            # to the page that owns it (alerts/ops/cost), instead of a read-only wall.
            _disp = [c for c in ("SEVERITY", "KIND", "DATABASE", "TITLE", "DETAIL", "SOURCE", "RAISED_AT")
                     if c in queue.columns]
            # rec27: an "Age" companion ("3h ago") next to RAISED_AT reads at a glance;
            # the real timestamp stays for sort/tz. assign preserves row order so the
            # positional selection below still maps back to `queue`.
            _qdisp = queue
            if "RAISED_AT" in _disp:
                _now = account_now()
                _qdisp = queue.assign(AGE=queue["RAISED_AT"].map(lambda t: humanize_age(t, _now)))
                _disp = [*_disp, "AGE"]
            # rec29: navigate only on a CHANGED selection (the sticky-selection guard
            # lives in selectable_nav_table now — was firing request_navigation every
            # rerun on the sticky row).
            def _open_triage(_i: int) -> None:
                # rec20/21/22: carry the row's IDENTITY (event) / land on the owning
                # SECTION (task) / SCOPE to the offending entity (spend), instead of
                # dropping everything and landing account-wide on a page default.
                _qr = queue.iloc[int(_i)]
                _kind = str(_qr.get("KIND"))
                _ctx: dict = {}
                _flt: dict = {}
                if _kind == "Alert":
                    # The Alerts drawer self-selects on an event_id context, so carrying
                    # it lands the click on THIS event's drawer, not the Open-events wall.
                    _dest = ("Alerts", "Open events")
                    _eid = str(_qr.get("EVENT_ID") or "").strip()
                    if _eid:
                        _ctx["event_id"] = _eid
                elif _kind == "Task failure":
                    _dest = ("Operations", "Tasks")  # the section that owns tasks
                elif _kind in ("Spend anomaly", "Spend collapse"):
                    # Operations -> Queries is the section that actually CONSUMES
                    # warehouse_contains (_queries_tab takes it; its contract lists it).
                    # Warehouses ignores it — its contract even renders "Active but
                    # ignored: Warehouse" — so route to Queries: the "what ran to spike
                    # this warehouse" view, genuinely scoped, not a silent no-op.
                    _dest = ("Operations", "Queries")
                    _wh = str(_qr.get("WAREHOUSE") or "").strip()
                    if _wh:
                        _flt["warehouse_contains"] = _wh
                else:
                    _dest = ("Alerts", "")
                request_navigation(_dest[0], _dest[1], _flt or None, _ctx or None)
            selectable_nav_table(_qdisp[_disp], key="cr_triage_sel", on_select=_open_triage,
                                 height=260, size_note=False)  # the caption below states the count
            st.caption(f"{len(queue)} item(s), ranked by severity then by dollars at risk "
                       "— select one to open its page. Sources: alerts, task facts, spend "
                       "anomalies. Task rows are one per task (failures summed across the "
                       "last 3 days incl. today), not one per day."
                       + (" Task failures follow the database filter; alerts and "
                          "spend anomalies don't have database grain." if f["database"] else ""))
        # C2: the app scores FACT_WAREHOUSE_DAILY itself, so the server twin's
        # COST_ANOMALY_SWEEP events are dropped from THIS feed (they stay on Alerts) —
        # otherwise every spend break arrived twice, once from each scorer, at two
        # different severities. E5: name the baseline, the scoring minimum and the money
        # floor OUTSIDE the empty/non-empty branch, because "nothing to triage" is
        # exactly where the reader most needs to know what was never in scope.
        if wh_daily.usable():
            st.caption(md_dollars(
                "Spend anomalies: robust median/MAD z-score per warehouse over the last 30 "
                f"complete days (baseline median {format_usd(_base_median_usd)}/day). Flagged at "
                f"|z| >= {DEFAULT_THRESHOLD}; HIGH at |z| >= {ANOMALY_HIGH_Z:.0f} or "
                f"{format_usd(ANOMALY_HIGH_EXCESS_USD)}/day over baseline — fixed in-app defaults. "
                "The server sweep SP_ANOMALY_SWEEP escalates on the configurable "
                "ALERT_CONFIG.THRESHOLD_NUM, so where that threshold has been tuned the two can "
                "differ; its COST_ANOMALY_SWEEP events (excluded here, shown on Alerts) stay "
                "authoritative. A warehouse needs 5+ complete days of history "
                "to be scored at all"
                + (f" — {_thin_warehouses} currently do not have them and are unscored."
                   if _thin_warehouses else ".")))

    elif section == "Timeline & movers":
        # ---- Incident correlation timeline -----------------------------------------
        section_header("Incident correlation timeline")
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
            if tz_note and not st.session_state.get("_ow_tz_note_shown"):  # rec34: once per page
                st.caption(tz_note)
                st.session_state["_ow_tz_note_shown"] = True
            # CR9: overlay hourly spend on the same time axis so a cost spike and
            # the events around it read together. Same window as the timeline;
            # HOUR_TS is localized the same way as AT so the two cannot drift.
            tl_hours = 48 if tl_win.startswith("48h") else tl_days * 24
            cred = run(cost_sql.hourly_credits(tl_hours, _tl_company), page=_PAGE,
                       key=f"cr_tl_credits_{_tl_company}_{tl_hours}", tier="recent",
                       source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (hourly credits)")
            credits_df = None
            if cred.usable():
                cdf = cred.df.copy()
                cdf["USD"] = cdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
                cdf, _ = localize_timestamps(cdf, ["HOUR_TS"])
                credits_df = cdf
            charts.operational_replay(tdf, credits=credits_df)
            sel_tl = selectable_table(tdf, key="cr_timeline_sel", height=240)
            if sel_tl is not None and 0 <= sel_tl < len(tdf):
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
            _ls_sel = selectable_table(_spk.df, key="cr_lockspike_sel", height=180)
            st.caption("Objects with >=5 waits last day and >3x their own baseline — the Operations "
                       "Warehouses section has the full table and history. Select an object for its "
                       "recent lock-wait events.")
            if _ls_sel is not None and 0 <= _ls_sel < len(_spk.df):
                _o = _spk.df.iloc[_ls_sel]
                _odb, _osc, _oob = str(_o["DATABASE_NAME"]), str(_o["SCHEMA_NAME"]), str(_o["OBJECT_NAME"])
                _det = run(ops_sql.lock_wait_object_detail(_odb, _osc, _oob, days=2), page=_PAGE,
                           key=f"cr_lockdetail_{_odb}_{_osc}_{_oob}", tier="recent",
                           source="LOCK_WAIT_HISTORY (per-object detail, live)")
                st.markdown(f"**Lock-wait events** — `{_odb}.{_osc}.{_oob}` (last 2 days)")
                if guard(_det, "No lock-wait events for this object in the last 2 days "
                               "(aggregate lags the raw view)."):
                    styled_table(_det.df, height=240)
                    result_caption(_det)

        section_header("Spend movers (window vs prior)")
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
            # Cost ▸ Spend & Attribution owns the full movers table; here we show
            # only the top-3 by absolute movement as a glance + a deep-link.
            top = view.reindex(view["DELTA_USD"].abs().sort_values(ascending=False).index).head(3)
            kpi_row([
                {"label": str(r["WAREHOUSE_NAME"]),
                 "value": format_usd(safe_float(r["DELTA_USD"])),
                 "help": f"{format_usd(safe_float(r['USD_PRIOR']))} → "
                         f"{format_usd(safe_float(r['USD_CURRENT']))}"}
                for _, r in top.iterrows()
            ])
            if st.button("Full spend movers → Cost & Contract", key="cr_movers_cost"):
                request_navigation("Cost & Contract", "Spend & Attribution")
            result_caption(movers)

    elif section == "Freshness & replay":
        _freshness_board()
        st.divider()
        # r19 #2: six reads for a bottom-of-page feature ran on every rerun —
        # the flight recorder now loads only when asked (results stay cached).
        if st.toggle("Load day replay", key="cr_replay_on",
                     help="Six reads across spend, activity, DDL, grants, tasks, and "
                          "alerts for one chosen day. Loads when on; caches after."):
            _day_replay()

    elif section == "Entity 360":
        # Watchlist row click: land on the Entity sub-tab so the drilled entity
        # actually shows (the flag is set by render_watchlist just before it
        # navigates; writing the widget key pre-render is lazy_sections' own
        # seeding pattern).
        pending_view = st.session_state.pop("_ow_entity_view_pending", None)
        if pending_view in ("Entity", "Watchlist"):
            st.session_state["cr_entity_view"] = pending_view
        entity_view = nested_sections(["Entity", "Watchlist"], key="cr_entity_view")
        if entity_view == "Entity":
            render_entity_360(company)
        else:
            render_watchlist()
