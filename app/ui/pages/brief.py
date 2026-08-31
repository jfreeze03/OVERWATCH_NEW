"""Morning brief — one phone-friendly scroll: the numbers, the fires, the asks.

Deliberately tiny: five figures, open criticals, top three actions, a spend
sparkline. Everything links into the full pages for depth.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.identity import viewer_name
from app.core.query import run, run_batch
from app.core.state import filters, request_navigation
from app.data import mart_sql
from app.logic import case_file
from app.logic.actions import rank_actions
from app.logic.formulas import (
    ExecutiveSummaryView,
    account_now,
    blended_billed_usd,
    contract_runway,
    daily_spend_last_n,
    executive_slide_bullets,
    executive_summary_csv,
    executive_summary_html,
    format_usd,
    humanize_duration,
    md_dollars,
    safe_float,
)
from app.logic.verdict import Signal, oldest_open_hours, page_verdict
from app.ui import charts
from app.ui.components import (
    alarm_health,
    contract_runway_bar,
    daily_spend_wide,
    download_text_button,
    empty_state,
    export_button,
    kpi_row,
    load_settings,
    methodology_note,
    page_header,
    page_verdict_line,
    panel_help,
    section_filter_contract,
    section_header,
    selectable_table,
)
from app.ui.sizing import TABLE_H_SM
from app.ui.workbench import render_watch_badge

_PAGE = "Brief"


def _stalest_label(vals: dict) -> str:
    """Stalest-telemetry badge text: source name + age, or 'name never loaded'.

    The strip's STALEST_SOURCE_H is -1 exactly when the worst source has no data
    at all (C3); STALEST_SOURCE_NAME says which one (N15)."""
    name = str(vals.get("STALEST_SOURCE_NAME", "") or "")
    src = f"{name} " if name and name != "none" else ""
    # The -1 sentinel arrives from the mart as the scale-1 string "-1.0"
    # (TO_VARCHAR of a NUMBER(.,1)), so compare NUMERICALLY — a literal "-1" match
    # never fired, leaving a never-loaded source rendering "…-1h" instead of "never
    # loaded". A real age is always >= 0; anything below (or unparseable) is the
    # never-loaded sentinel.
    _h = safe_float(vals.get("STALEST_SOURCE_H"), default=float("nan"))
    if not (_h >= 0):
        return f"{src}never loaded" if src else "no data yet"
    return f"{src}{humanize_duration(_h, 'h')}"


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company = f["company"]
    # F1: H1 matches the sidebar nav label verbatim; the subtitle keeps the
    # morning identity.
    page_header("Brief", "Your morning one-scroll: numbers first, fires second, asks third.", icon_name="brief")
    section_filter_contract(
        f,
        applies=(),
        partial=("company",),
        note="Company shapes open incidents/events; spend, contract, freshness, and owner queue use fixed account-wide horizons.",
    )
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)

    # Two tier-grouped parallel batches (live round 10: ten serial reads made
    # the exec page the slow one — p95 8.9s). Any batch failure falls back to
    # the original serial per-query path below, unchanged.
    # health_strip deliberately stays outside this batch: it has its own shell
    # TTL on other pages and the serial call below keeps the same cache identity.
    _b_live = run_batch([
        {"key": "inc", "sql": mart_sql.open_incidents(5, company),
         "source": f"INCIDENTS (open, {company} + account-level)"},
        {"key": "inc_met", "sql": mart_sql.incident_metrics(90, company),
         "source": f"INCIDENTS (open-now count, {company} + account-level)"},
        {"key": f"brief_alert_counts_{company}",
         "sql": mart_sql.open_alert_severity_counts(company),
         "source": f"ALERT_EVENTS counts ({company} + account-level)"},
        {"key": "events", "sql": mart_sql.open_alert_events(50, company),
         "source": "ALERT_EVENTS"},
        {"key": f"acts_{company}", "sql": mart_sql.action_queue(100, company), "source": "ACTION_QUEUE"},
    ], page=_PAGE, tier="live")
    _b_rec = run_batch([
        {"key": "exh", "sql": mart_sql.contract_exhaustion(),
         "source": "SETTINGS + FACT_METERING_DAILY"},
        {"key": "roi", "sql": mart_sql.savings_summary_quarter(), "source": "SAVINGS_LEDGER"},
        {"key": "appq", "sql": mart_sql.app_cost_last_30d(),
         "source": "FACT_WAREHOUSE_DAILY (WH_ALFA_ADMIN trailing 30d)"},
        # PERF #46: the 14d spark moved OUT of this 'recent' batch to the shared hourly
        # daily_spend_wide() read below — batch members cache in a separate 'recent' store
        # that can't share with the solo hourly wide entry Overview/Contract also use.
        {"key": "digest", "sql": mart_sql.latest_digest(),
         "source": "DAILY_DIGEST (Cortex, grounded)"},
    ], page=_PAGE, tier="recent")

    # Perf: 'recent' (300s) — shares the health_strip cache entry with the sidebar/other shells
    # (see main.py); the mart loads hourly and writes invalidate via the domain salt.
    strip = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="recent",
                source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    strip_up = strip.ok and not strip.empty
    vals = ({str(r["METRIC"]): str(r["VALUE"]) for _, r in strip.df.iterrows()}
            if strip_up else {})
    mtd_credits = safe_float(vals.get("MTD_CREDITS"))
    # C1: price AI/Cortex credits at the AI rate, not the flat compute rate. The
    # strip carries the split; fall back to the flat rate only if a stale cache
    # predates it (both split arms absent) so we never silently show $0.
    if "MTD_CREDITS_OTHER" in vals or "MTD_CREDITS_AI" in vals:
        mtd_usd = blended_billed_usd(safe_float(vals.get("MTD_CREDITS_OTHER")),
                                     safe_float(vals.get("MTD_CREDITS_AI")), rate, ai_rate)
    else:
        mtd_usd = mtd_credits * rate
    alert_counts = _b_live.get(f"brief_alert_counts_{company}")
    if alert_counts is None or not alert_counts.ok:
        alert_counts = run(
            mart_sql.open_alert_severity_counts(company),
            page=_PAGE,
            key=f"brief_alert_counts_{company}",
            tier="live",
            source=f"ALERT_EVENTS counts ({company} + account-level)",
        )
    scoped_crit: int | None = None
    if alert_counts is not None and alert_counts.usable():
        scoped_crit = int(safe_float(alert_counts.df.iloc[0].get("CRIT")))
    # CoCo do-first (duration, not count): the oldest still-open CRITICAL drives MTTR
    # urgency a raw count hides. Reuse the events already fetched in the batch — no query.
    _ev = _b_live.get("events")
    _oldest_crit_h = oldest_open_hours(
        _ev.df if (_ev is not None and _ev.usable()) else None,
        now=account_now(), severity="CRITICAL")
    # Honesty contract: when telemetry is unreachable the Brief says SO —
    # a zero here reads as "we spent nothing", which is a lie (review #5).
    kpis = [
        {"label": "MTD credit spend (account)",
         "badge": "mart" if strip_up else "stale",
         "value": format_usd(mtd_usd) if strip_up else "—",
         "delta": (f"{mtd_credits:,.0f} credits" if strip_up else "telemetry unreachable"),
         "delta_color": "off",
         "severity": "" if strip_up else "warn",
         "help": "Account-wide credit-billed services at configured rates this month. "
                 "Storage, transfer, and organization rate-card adjustments are separate; "
                 "the company filter scopes warehouse, attribution, and user views."},
        {"label": "Open criticals",
         "badge": "live" if scoped_crit is not None else "stale",
         "value": f"{scoped_crit}" if scoped_crit is not None else "—",
         "severity": "bad" if scoped_crit else ("" if scoped_crit is not None else "warn"),
         "delta_color": "inverse" if scoped_crit else "off",
         "help": f"Open criticals for {company} plus account-level events — the same scope as Fires."},
        {"label": "Stalest telemetry",
         # N15: name the source, not just the age — "which one?" is the DBA's
         # first question. N14: the strip's age arm is already cadence-aware.
         "value": _stalest_label(vals) if strip_up else "unknown",
         "severity": "" if strip_up else "warn"},
    ]
    if _oldest_crit_h is not None:
        kpis.append({
            "label": "Oldest open critical",
            "value": humanize_duration(_oldest_crit_h, "h"),
            "severity": "bad" if _oldest_crit_h >= 24 else "warn",
            "help": "Time since the oldest still-open (OPEN or ACK) CRITICAL alert was "
                    "raised — the responsiveness signal a raw count hides. Work the Fires below.",
        })
    if not strip_up:
        st.warning("Telemetry marts unreachable — the Brief refuses to invent numbers. "
                   + (strip.error or ""))
    exh = _b_rec.get("exh") or run(mart_sql.contract_exhaustion(), page=_PAGE, key="brief_exhaustion",
              tier="recent", source="SETTINGS + FACT_METERING_DAILY")
    if exh.usable():
        erow = exh.df.iloc[0]
        total = safe_float(erow.get("TOTAL"))
        days_left = safe_float(erow.get("DAYS_LEFT"), -1.0)
        if total > 0 and days_left >= 0:
            kpis.append({
                "label": "Credit commitment exhausts",
                "value": str(erow.get("EXHAUST_DATE")),
                "delta": f"{days_left:,.0f} days at current burn",
                "delta_color": "inverse" if days_left <= 90 else "off",
                "help": "Configured-rate credit runway from trailing 30 complete days. "
                        "It excludes storage, transfer, and organization currency adjustments; "
                        "billing-truth runway is on Cost & Contract > Contract & Forecast.",
            })
    roi = _b_rec.get("roi") or run(mart_sql.savings_summary_quarter(), page=_PAGE, key="brief_roi",
              tier="recent", source="SAVINGS_LEDGER")
    cost_q = _b_rec.get("appq") or run(mart_sql.app_cost_last_30d(), page=_PAGE, key="brief_app_cost",
                 tier="recent", source="FACT_WAREHOUSE_DAILY (WH_ALFA_ADMIN trailing 30d)")
    if roi.usable():
        rrow = roi.df.iloc[0]
        verified = safe_float(rrow.get("VERIFIED_QTD_USD"))
        pipeline = safe_float(rrow.get("ESTIMATED_OPEN_USD"))
        app_usd = (safe_float(cost_q.df.iloc[0].get("APP_CREDITS_30D")) * rate
                   if cost_q.usable() else None)
        kpis.append({
            "label": "Verified savings (QTD)",
            "value": format_usd(verified),
            "delta": (f"vs {format_usd(app_usd)} monthly run cost" if app_usd is not None
                      else "app cost unavailable"),
            "delta_color": ("normal" if verified >= app_usd else "inverse")
                           if app_usd is not None else "off",
            "help": "VERIFIED ledger items only — proven by before/after actuals, never "
                    "mixed with estimates. App cost = the shared app/loader warehouse's trailing 30-day "
                    "(monthly) run cost -- same horizon as the monthly-magnitude savings. Green means "
                    "OVERWATCH pays for itself.",
        })
        if pipeline > 0:
            kpis.append({
                "label": "Estimated pipeline",
                "value": format_usd(pipeline),
                "delta_color": "off",
                "help": "Open ESTIMATED items awaiting the monthly verifier. "
                        "Deliberately shown apart from verified.",
            })
    _inc_company = company
    _inc = _b_live.get("inc") or run(mart_sql.open_incidents(5, _inc_company), page=_PAGE,
               key=f"brief_incidents_{_inc_company}", tier="live",
               source=f"INCIDENTS (open, {_inc_company} + account-level)")
    if _inc.ok:
        # Count from the UNCAPPED incident_metrics.OPEN_NOW (the same builder Control Room reads), not
        # len() of the LIMIT-5 open_incidents feed used for the detail below -- otherwise the KPI
        # saturates at 5 and disagrees with the Control Room queue for >5 open incidents (recon-audit
        # 2026-08-30). Fall back to the capped len only if the metrics read is unavailable.
        _inc_met = _b_live.get("inc_met")
        if _inc_met is not None and _inc_met.usable():
            _n_inc = int(safe_float(_inc_met.df.iloc[0].get("OPEN_NOW")))
        else:
            _n_inc = len(_inc.df)
        kpis.append({
            "label": "Open incidents",
            "value": f"{_n_inc}",
            "severity": "bad" if _n_inc else "ok",
            "help": "Lifecycle objects — declared or auto-declared CRITICALs (true open count, "
                    "matching Control Room). The Control Room owns the queue; this is the "
                    "executive glance.",
        })
    # CoCo do-first #1: a computed "should I worry?" opener, worst-first, above the
    # numbers — built from signals already on the page (no new query).
    _vsig = []
    if not strip_up:
        _vsig.append(Signal("warn", "telemetry marts unreachable — figures withheld"))
    if scoped_crit is None:
        _vsig.append(Signal("warn", "open-critical count unavailable"))
    elif scoped_crit > 0:
        _age = (f", oldest {humanize_duration(_oldest_crit_h, 'h')}"
                if _oldest_crit_h is not None else "")
        _vsig.append(Signal("bad", f"{scoped_crit} open critical alert(s){_age}"))
    if _inc.ok and len(_inc.df) > 0:
        _vsig.append(Signal("bad", f"{len(_inc.df)} open incident(s)"))
    if exh.usable():
        _erow = exh.df.iloc[0]
        if safe_float(_erow.get("TOTAL")) > 0:
            _dl = safe_float(_erow.get("DAYS_LEFT"), -1.0)
            if 0 <= _dl <= 30:
                _vsig.append(Signal("bad", f"contract runway {_dl:,.0f} days"))
            elif 0 <= _dl <= 90:
                _vsig.append(Signal("warn", f"contract runway {_dl:,.0f} days"))
    page_verdict_line(page_verdict(
        _vsig, healthy="no open criticals or incidents, and contract runway is comfortable"))
    contract_runway_bar(contract_runway(exh.df.iloc[0]) if exh.usable() else None)
    panel_help(
        "Your one-scroll morning read: the headline numbers, then open fires, then the "
        "top asks. A dash (—) means telemetry was unreachable, not zero. When a figure "
        "turns red — open criticals above zero, or contract/savings in the danger band — "
        "work the Fires and Asks below, then drill into the linked full page "
        "(Alerts, Cost & Contract, Control Room)."
    )
    kpi_row(kpis)
    # N7: same disclosure as Overview — the headline dollars are credit-billed
    # services; storage and data-transfer bill separately (Cost & Contract).
    # #1: pure billing-basis disclosure → audit-mode only (the note Overview also hides).
    methodology_note("Spend covers credit-billed services (compute, serverless, AI); "
                     "storage and data-transfer bill separately.")

    spend = daily_spend_wide(_PAGE)   # PERF #46: shared wide read; sliced to 14d for the spark
    brief_spend_series: list[float] = []
    if spend.ok and not spend.empty:
        spark_df = daily_spend_last_n(spend.df, 14)
        charts.sparkline_row([("Spend, 14 days", spark_df, "DAY", "CREDITS_BILLED")])
        if {"CREDITS_BILLED_OTHER", "CREDITS_BILLED_AI"}.issubset(spark_df.columns):
            brief_spend_series = [
                blended_billed_usd(row["CREDITS_BILLED_OTHER"], row["CREDITS_BILLED_AI"],
                                   rate, ai_rate)
                for _, row in spark_df.iterrows()
            ]
        else:
            brief_spend_series = [safe_float(value) * rate
                                  for value in spark_df["CREDITS_BILLED"].tolist()]

    # N2: a critical that paged nobody hides behind a green board — call it out
    # on the one surface a half-awake on-call actually reads.
    _und = int(safe_float(vals.get("UNDELIVERED_CRITICAL", "0"))) if strip_up else 0
    _und_age = safe_float(vals.get("UNDELIVERED_OLDEST_MIN", "0")) if strip_up else 0.0
    _und_age_txt = f", oldest {humanize_duration(_und_age, 'min')}" if _und_age > 0 else ""
    if _und and st.button(f"⚠ {_und} critical alert(s) reached nobody{_und_age_txt} — "
                          "check delivery →", key="brief_undelivered", type="primary",
                          width="stretch"):
        request_navigation("Alerts", "Native delivery")

    # Honor the company filter (live finding 2026-07-08: Trexis warehouse
    # fires showed under an ALFA scope). Account-level events always show.
    events = _b_live.get("events") or run(mart_sql.open_alert_events(50, company), page=_PAGE,
                 key=f"brief_events_{company}", tier="live", source="ALERT_EVENTS")
    # C23: "Fires" is amber only when critical/high fires EXIST.
    _crit_n = (int(events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"]).sum())
               if events.ok and not events.empty else (0 if events.ok else None))
    section_header("Fires", alarm_health(_crit_n), "alerts")
    if events.ok and not events.empty:
        crit = events.df[events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"])]
        if crit.empty:
            empty_state("clean", "No open critical or high alerts.")
        else:
            _fires = crit.head(5)
            _fire_sel = selectable_table(_fires[["RAISED_AT", "SEVERITY", "TITLE"]],
                                         key="brief_fires_sel", height=TABLE_H_SM)
            # rec29 sticky-selection guard: st.dataframe re-emits the selection on
            # every rerun, so open the event's drawer only when the row CHANGES.
            if _fire_sel is not None and _fire_sel != st.session_state.get("_brief_fire_sel_last"):
                st.session_state["_brief_fire_sel_last"] = _fire_sel
                _eid = str(_fires.iloc[int(_fire_sel)]["EVENT_ID"])
                request_navigation("Alerts", "Open events", context={"event_id": _eid})
            if company != "ALL":
                st.caption(f"Scoped to {company} plus account-level events.")
            if st.button("Open the alert queue →", key="brief_alerts", width="stretch"):
                request_navigation("Alerts", "Open events")
    else:
        # rec23/house-rule-8: green means VERIFIED CLEAN, never "nothing loaded".
        if events.ok:
            empty_state("clean", "No open alerts.")
        else:
            empty_state("needs_setup", "Alerting not installed yet.")

    section_header("Asks", "info", "bolt")
    brief_action_lines: list[str] = []
    actions = _b_live.get(f"acts_{company}") or run(mart_sql.action_queue(100, company), page=_PAGE,
                  key=f"brief_actions_{company}", tier="live", source="ACTION_QUEUE")
    if actions.ok and not actions.empty:
        ranked = rank_actions(actions.df, limit=3)
        if ranked.empty:
            empty_state("clean", "Nothing waiting on an owner.")
        else:
            for _, a in ranked.iterrows():
                est = safe_float(a.get("ESTIMATED_USD"))
                # DS #7: disclose the estimate's time basis inline so a monthly run-rate
                # and a one-time saving don't read as the same number.
                _basis = {"MONTHLY": "/mo", "ANNUAL": "/yr", "ONE_TIME": " one-time"}.get(
                    str(a.get("PERIOD") or "").strip().upper(), "")
                brief_action_lines.append(
                    f"[{a['SEVERITY']}] {a['TITLE']} - owner {a.get('OWNER') or 'unassigned'}"
                    + (f" - about {format_usd(est)}{_basis}" if est > 0 else "")
                )
                # $-escape: TITLE is data — a '$' in it pairs with format_usd's '$'
                st.markdown(md_dollars(f"- **[{a['SEVERITY']}]** {a['TITLE']} — owner "
                            f"{a.get('OWNER') or 'unassigned'}"
                            + (f" · ~{format_usd(est)}{_basis}" if est > 0 else "")))
            # D1: the top three, by WHAT? Severity first, dollars only as a tiebreak
            # inside a band — without this line a reader takes a $-annotated list for
            # a $-ordered one and asks why the biggest number is not on top.
            st.caption("Top 3 by severity, then overdue, then estimated $, then age.")
    else:
        if actions.ok:
            empty_state("clean", "Action queue is empty.")
        else:
            empty_state("needs_setup", "Action queue not installed yet.")

    # Watch automation (owner ask 2026-08-17): the proactive half of "watch". If
    # any watched entity moved (cost spike/drop or health drop), the badge leads
    # here on the landing page with what moved and a jump to the Watchlist; quiet
    # when steady. Renders nothing when the viewer has no watchlist.
    render_watch_badge(viewer_name(), rate)

    # rec2: the AI narrative is context, not the headline — it sits BELOW the numbers,
    # fires, and asks (the page's "numbers first, fires second, asks third" contract)
    # and is collapsed by default so open fires stay above the fold.
    digest = _b_rec.get("digest") or run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="recent",
                 source="DAILY_DIGEST (Cortex, grounded)")
    if digest.usable():
        drow = digest.df.iloc[0]
        with st.expander(f"AI morning narrative — {drow.get('DIGEST_DATE')}", expanded=False):
            st.markdown(str(drow.get("BODY") or ""))

    _brief_view = ExecutiveSummaryView(
        company=company,
        days=14,
        generated=account_now().strftime("%Y-%m-%d %H:%M") + " (account time)",
        cards=tuple(
            (str(item.get("label", "Metric")),
             str(item.get("value", "-")).replace("â€”", "-")
             + (f" | {item['delta']}" if item.get("delta") else ""))
            for item in kpis
        ),
        actions=tuple(brief_action_lines),
        spend_series=tuple(brief_spend_series),
        scope_notes=(
            "MTD spend, contract, savings, and freshness are account-wide unless the card says otherwise.",
            f"Alerts and incidents honor {company} plus account-level events; the action queue is account-wide.",
            "Metering can lag up to 24 hours. A dash means telemetry was unavailable, not zero.",
        ),
        title="Morning brief",
    )
    with st.expander("Executive export", expanded=False):
        _ex_html, _ex_slide, _ex_csv = st.columns(3)
        with _ex_html:
            export_button(
                "Presentation (HTML)",
                executive_summary_html(_brief_view, presentation=True),
                file_name="overwatch_morning_brief.html",
                mime="text/html",
                width="stretch",
            )
        with _ex_slide:
            download_text_button(
                "Slide bullets (.txt)",
                executive_slide_bullets(_brief_view),
                "overwatch_morning_brief_bullets.txt",
            )
        with _ex_csv:
            export_button(
                "Brief data (CSV)",
                executive_summary_csv(_brief_view),
                file_name="overwatch_morning_brief.csv",
                mime="text/csv",
                width="stretch",
            )

    _case_items = list(st.session_state.get(case_file.CASE_STATE_KEY, []))
    with st.expander(f"Operator Case File ({len(_case_items)})", expanded=bool(_case_items)):
        st.caption(
            "A session-only, cross-section handoff. Click ＋ Add to Case on evidence across "
            "Alerts, Operations, Security and Overview to collect it here, then export one "
            "Markdown document for a ticket or the next shift. Cleared when the session ends.")
        if not _case_items:
            st.caption("The case is empty.")
        else:
            _md = case_file.assemble_markdown(
                _case_items, generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
            _dl, _clr = st.columns([3, 1])
            with _dl:
                download_text_button("Case File (.md)", _md, "overwatch_case_file.md")
            with _clr:
                if st.button("Clear case", key="ow_case_clear", width="stretch"):
                    st.session_state[case_file.CASE_STATE_KEY] = case_file.clear_items()
                    st.rerun()
            for _i, _it in enumerate(_case_items):
                _row, _rm = st.columns([6, 1])
                with _row:
                    st.markdown(
                        f"**{_it.get('title') or _it.get('section')}** — {_it.get('company')} · "
                        f"{_it.get('window')} · `{_it.get('source') or '—'}`")
                with _rm:
                    if st.button("Remove", key=f"ow_case_rm_{_i}", width="stretch"):
                        st.session_state[case_file.CASE_STATE_KEY] = case_file.remove_item(
                            _case_items, _it["id"])
                        st.rerun()
            # Raw markdown as the copy-out surface: a working path even when the SiS
            # download button is inert, and it sidesteps $-as-LaTeX in st.markdown.
            st.code(_md, language="markdown")

    st.caption(pd.Timestamp.now().strftime("Generated %Y-%m-%d %H:%M") +
               " · full detail lives on Overview and Control Room.")
