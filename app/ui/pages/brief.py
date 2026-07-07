"""Morning brief — one phone-friendly scroll: the numbers, the fires, the asks.

Deliberately tiny: five figures, open criticals, top three actions, a spend
sparkline. Everything links into the full pages for depth.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.state import request_navigation
from app.data import mart_sql
from app.logic.actions import rank_actions
from app.logic.formulas import format_usd, safe_float
from app.ui import charts
from app.ui.components import kpi_row, load_settings, page_header, styled_table

_PAGE = "Brief"


@safe_page(_PAGE)
def render() -> None:
    page_header("Morning brief", "The one-scroll version. Numbers first, fires second, asks third.")
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)

    strip = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="live",
                source="ALERT_EVENTS + MART_SOURCE_FRESHNESS + FACT_METERING_DAILY")
    vals = ({str(r["METRIC"]): str(r["VALUE"]) for _, r in strip.df.iterrows()}
            if strip.ok and not strip.empty else {})
    mtd_credits = safe_float(vals.get("MTD_CREDITS"))
    kpis = [
        {"label": "MTD spend", "value": format_usd(mtd_credits * rate),
         "delta": f"{mtd_credits:,.0f} credits", "delta_color": "off"},
        {"label": "Open criticals", "value": vals.get("OPEN_CRITICAL", "0"),
         "delta_color": "inverse" if vals.get("OPEN_CRITICAL", "0") not in ("0", "") else "off"},
        {"label": "Stalest telemetry", "value": f"{vals.get('STALEST_SOURCE_H', '?')}h"},
    ]
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
    events = run(mart_sql.open_alert_events(50), page=_PAGE, key="brief_events", tier="live",
                 source="ALERT_EVENTS")
    if events.ok and not events.empty:
        crit = events.df[events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"])]
        if crit.empty:
            st.success("No open critical or high alerts.")
        else:
            styled_table(crit[["RAISED_AT", "SEVERITY", "TITLE"]].head(5), height=220)
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
