"""Morning brief — one phone-friendly scroll: the numbers, the fires, the asks.

Deliberately tiny: five figures, open criticals, top three actions, a spend
sparkline. Everything links into the full pages for depth.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.state import filters, request_navigation
from app.data import mart_sql
from app.logic.actions import rank_actions
from app.logic.formulas import format_usd, safe_float
from app.ui import charts
from app.ui.components import kpi_row, load_settings, page_header, styled_table

_PAGE = "Brief"


@safe_page(_PAGE)
def render() -> None:
    page_header("Morning brief", "The one-scroll version. Numbers first, fires second, asks third.", icon_name="brief")
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)

    strip = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="live",
                source="ALERT_EVENTS + MART_SOURCE_FRESHNESS + FACT_METERING_DAILY")
    strip_up = strip.ok and not strip.empty
    vals = ({str(r["METRIC"]): str(r["VALUE"]) for _, r in strip.df.iterrows()}
            if strip_up else {})
    mtd_credits = safe_float(vals.get("MTD_CREDITS"))
    # Honesty contract: when telemetry is unreachable the Brief says SO —
    # a zero here reads as "we spent nothing", which is a lie (review #5).
    kpis = [
        {"label": "MTD spend (account)",
         "value": format_usd(mtd_credits * rate) if strip_up else "n/a",
         "delta": (f"{mtd_credits:,.0f} credits" if strip_up else "telemetry unreachable"),
         "delta_color": "off",
         "severity": "" if strip_up else "warn",
         "help": "Account-wide billed credits this month. Metering-daily has no company "
                 "dimension; the company filter scopes warehouse, attribution, and user views."},
        {"label": "Open criticals",
         "value": vals.get("OPEN_CRITICAL", "0") if strip_up else "?",
         "severity": "" if strip_up else "warn",
         "delta_color": "inverse" if vals.get("OPEN_CRITICAL", "0") not in ("0", "") else "off"},
        {"label": "Stalest telemetry",
         "value": f"{vals.get('STALEST_SOURCE_H', '?')}h" if strip_up else "unknown",
         "severity": "" if strip_up else "warn"},
    ]
    if not strip_up:
        st.warning("Telemetry marts unreachable — the Brief refuses to invent numbers. "
                   + (strip.error or ""))
    exh = run(mart_sql.contract_exhaustion(), page=_PAGE, key="brief_exhaustion",
              tier="recent", source="SETTINGS + FACT_METERING_DAILY")
    if exh.usable():
        erow = exh.df.iloc[0]
        total = safe_float(erow.get("TOTAL"))
        days_left = safe_float(erow.get("DAYS_LEFT"), -1.0)
        if total > 0 and days_left >= 0:
            kpis.append({
                "label": "Contract exhausts",
                "value": str(erow.get("EXHAUST_DATE")),
                "delta": f"{days_left:,.0f} days at current burn",
                "delta_color": "inverse" if days_left <= 90 else "off",
                "help": "Straight-line on trailing 30d billed credits vs contracted credits. "
                        "Scenarios: Cost > Contract > Renewal planner.",
            })
    roi = run(mart_sql.savings_summary_quarter(), page=_PAGE, key="brief_roi",
              tier="recent", source="SAVINGS_LEDGER")
    cost_q = run(mart_sql.app_cost_quarter(), page=_PAGE, key="brief_app_cost",
                 tier="recent", source="WAREHOUSE_METERING_HISTORY (WH_ALFA_OVERWATCH)")
    if roi.usable():
        rrow = roi.df.iloc[0]
        verified = safe_float(rrow.get("VERIFIED_QTD_USD"))
        pipeline = safe_float(rrow.get("ESTIMATED_OPEN_USD"))
        app_usd = (safe_float(cost_q.df.iloc[0].get("APP_CREDITS_QTD")) * rate
                   if cost_q.usable() else None)
        kpis.append({
            "label": "Verified savings (QTD)",
            "value": format_usd(verified),
            "delta": (f"vs {format_usd(app_usd)} app run cost" if app_usd is not None
                      else "app cost unavailable"),
            "delta_color": ("normal" if verified >= app_usd else "inverse")
                           if app_usd is not None else "off",
            "help": "VERIFIED ledger items only — proven by before/after actuals, never "
                    "mixed with estimates. App cost = the dedicated warehouse's quarter "
                    "spend. Green means OVERWATCH pays for itself.",
        })
        if pipeline > 0:
            kpis.append({
                "label": "Estimated pipeline",
                "value": format_usd(pipeline),
                "delta_color": "off",
                "help": "Open ESTIMATED items awaiting the monthly verifier. "
                        "Deliberately shown apart from verified.",
            })
    _inc_company = filters()["company"]
    _inc = run(mart_sql.open_incidents(5, _inc_company), page=_PAGE,
               key=f"brief_incidents_{_inc_company}", tier="live",
               source=f"INCIDENTS (open, {_inc_company} + account-level)")
    if _inc.ok:
        _n_inc = len(_inc.df)
        kpis.append({
            "label": "Open incidents",
            "value": f"{_n_inc}",
            "severity": "bad" if _n_inc else "ok",
            "help": "Lifecycle objects — declared or auto-declared CRITICALs. "
                    "The Control Room owns the queue; this is the executive glance.",
        })
    kpi_row(kpis)

    spend = run(mart_sql.fact_daily_spend(14), page=_PAGE, key="brief_spark", tier="recent",
                source="FACT_METERING_DAILY")
    if spend.ok and not spend.empty:
        charts.sparkline_row([("Spend, 14 days", spend.df, "DAY", "CREDITS_BILLED")])

    digest = run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="recent",
                 source="DAILY_DIGEST (Cortex, grounded)")
    if digest.usable():
        drow = digest.df.iloc[0]
        with st.expander(f"AI morning narrative — {drow.get('DIGEST_DATE')}", expanded=True):
            st.markdown(str(drow.get("BODY") or ""))

    st.markdown("**Fires**")
    # Honor the company filter (live finding 2026-07-08: Trexis warehouse
    # fires showed under an ALFA scope). Account-level events always show.
    company = filters()["company"]
    events = run(mart_sql.open_alert_events(50, company), page=_PAGE,
                 key=f"brief_events_{company}", tier="live", source="ALERT_EVENTS")
    if events.ok and not events.empty:
        crit = events.df[events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"])]
        if crit.empty:
            st.success("No open critical or high alerts.")
        else:
            styled_table(crit[["RAISED_AT", "SEVERITY", "TITLE"]].head(5), height=220)
            if company != "ALL":
                st.caption(f"Scoped to {company} plus account-level events.")
            if st.button("Open the alert queue →", key="brief_alerts", use_container_width=True):
                request_navigation("Alerts", "Open events")
    else:
        st.success("No open alerts." if events.ok else "Alerting not installed yet.")

    st.markdown("**Asks**")
    actions = run(mart_sql.action_queue(100), page=_PAGE, key="brief_actions", tier="live",
                  source="ACTION_QUEUE")
    if actions.ok and not actions.empty:
        ranked = rank_actions(actions.df, limit=3)
        if ranked.empty:
            st.success("Nothing waiting on an owner.")
        else:
            for _, a in ranked.iterrows():
                est = safe_float(a.get("ESTIMATED_USD"))
                st.markdown(f"- **[{a['SEVERITY']}]** {a['TITLE']} — owner "
                            f"{a.get('OWNER') or 'unassigned'}"
                            + (f" · ~{format_usd(est)}" if est > 0 else ""))
    else:
        st.success("Action queue is empty." if actions.ok else "Action queue not installed yet.")

    st.caption(pd.Timestamp.now().strftime("Generated %Y-%m-%d %H:%M") +
               " · full detail lives on Overview and Control Room.")
