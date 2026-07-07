"""Control Room — DBA morning triage on one screen.

Ranked queue (alerts + task failures + spend anomalies), telemetry freshness,
24h operations pulse, and spend movers. No button maze: the queue is visible
on entry.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import THRESHOLDS
from app.core.errors import safe_page
from app.core.query import run
from app.core.state import filters
from app.data import cost_sql, mart_sql, ops_sql
from app.logic.actions import triage_queue
from app.logic.anomaly import anomaly_summary, flag_anomalies
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
    selectable_table,
    styled_table,
)

_PAGE = "Control Room"


def _freshness_board() -> None:
    res = run(mart_sql.source_freshness(), page=_PAGE, key="freshness", tier="live",
              source="MART_SOURCE_FRESHNESS")
    st.subheader("Telemetry freshness")
    if not res.ok:
        st.info("Freshness board is not installed yet; the live fallbacks below still work.")
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
    page_header("Control Room", "Morning triage: what broke, what's burning, what's stale.",
                scope_note=f"{company} · last {days} days")

    # ---- 24h pulse -----------------------------------------------------------
    pulse = run(ops_sql.query_window_summary(1, company, database=f["database"], schema_contains=f["schema_contains"]),
                page=_PAGE, key=f"pulse_{company}",
                tier="live", source="ACCOUNT_USAGE.QUERY_HISTORY (24h)")
    if pulse.usable():
        row = pulse.df.iloc[0]
        qcount = safe_float(row.get("QUERY_COUNT"))
        failed = safe_float(row.get("FAILED_COUNT"))
        kpi_row([
            {"label": "Queries (24h)", "value": f"{qcount:,.0f}"},
            {"label": "Failed", "value": f"{failed:,.0f}",
             "delta": f"{(failed / qcount * 100) if qcount else 0:.1f}%",
             "delta_color": "inverse" if qcount and failed / qcount > 0.02 else "off"},
            {"label": "p95 runtime", "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s"},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.1f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(pulse)
    elif not pulse.ok:
        st.error(f"24h pulse unavailable: {pulse.error}")
    else:
        st.info("No queries recorded in the last 24h for this scope.")

    # ---- Triage queue ----------------------------------------------------------
    st.subheader("Triage queue")
    alerts = run(mart_sql.open_alert_events(100), page=_PAGE, key="cr_alerts", tier="live",
                 source="ALERT_EVENTS")
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
            wh_daily.df.assign(USD=lambda d: d["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))),
            "USD", group_col="WAREHOUSE_NAME",
        )
        anomalies = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")

    queue = triage_queue(
        alerts.df if alerts.usable() else None,
        tasks.df if tasks.usable() else None,
        anomalies,
    )
    if queue.empty:
        sources_ok = alerts.ok and tasks.ok
        if sources_ok:
            st.success("Nothing to triage: no open alerts, task failures, or spend anomalies in scope.")
        else:
            st.info("Triage inputs incomplete: "
                    + ("alert tables not installed; " if not alerts.ok else "")
                    + ("task facts not installed." if not tasks.ok else ""))
    else:
        styled_table(queue)
        st.caption(f"{len(queue)} item(s), ranked by severity. Sources: alerts, task facts, spend anomalies.")

    # ---- Spend movers ----------------------------------------------------------
    st.subheader("Incident correlation timeline")
    panel_help(
        "Alerts, task failures, and DDL changes on one time axis (7 days). Click a row "
        "below the chart to see everything else that happened within ±30 minutes — the "
        "'what changed right before this broke?' view."
    )
    tl_win = st.radio("Window", ["48h (fresh)", "7d (cached hourly)"], horizontal=True,
                      key="cr_tl_win", label_visibility="collapsed",
                      help="Mid-incident you want fresh; the 7-day retrospective is the "
                           "heavy three-source join, so it caches for an hour.")
    tl_days, tl_tier = (2, "recent") if tl_win.startswith("48h") else (7, "historical")
    tl = run(mart_sql.incident_timeline(tl_days, f["company"] if isinstance(f, dict) and "company" in f else "ALL"),
             page=_PAGE, key=f"incident_timeline_{tl_days}", tier=tl_tier,
             source="ALERT_EVENTS + TASK_HISTORY + QUERY_HISTORY (DDL)")
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
                st.dataframe(nearby, hide_index=True, use_container_width=True)
            except (KeyError, ValueError, TypeError) as exc:
                st.caption(f"±30 min window unavailable for this row — {type(exc).__name__}: "
                           f"{str(exc)[:120]} (usually a non-timestamp AT value from a new source).")
        result_caption(tl)

    st.subheader("Spend movers (window vs prior)")
    movers = run(cost_sql.warehouse_window_vs_prior(days, company), page=_PAGE,
                 key=f"cr_movers_{company}_{days}", tier="historical",
                 source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY")
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
