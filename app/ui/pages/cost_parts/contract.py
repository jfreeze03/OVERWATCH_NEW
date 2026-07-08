"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.core.query import run
from app.data import cost_sql, insights_sql, mart_sql
from app.logic import contract_planner, steering
from app.logic.forecast import contract_pace
from app.logic.formulas import account_today, safe_float
from app.logic.insights import idle_advisor
from app.ui.components import (
    guard,
    kpi_row,
    panel_help,
    result_caption,
    styled_table,
)

_PAGE = "Cost & Contract"

_SERVICE_CATEGORY = {
    "WAREHOUSE_METERING": "Warehouse",
    "WAREHOUSE_METERING_READER": "Warehouse (reader)",
    "SNOWPIPE": "Serverless", "SNOWPIPE_STREAMING": "Serverless",
    "SERVERLESS_TASK": "Serverless", "SERVERLESS_ALERTS": "Serverless",
    "AUTOMATIC_CLUSTERING": "Serverless", "MATERIALIZED_VIEW": "Serverless",
    "SEARCH_OPTIMIZATION": "Serverless", "QUERY_ACCELERATION": "Serverless",
    "SNOWPARK_CONTAINER_SERVICES": "Serverless", "COPY_FILES": "Serverless",
    "REPLICATION": "Replication", "STORAGE": "Storage",
}


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

def _contract_tab(settings: dict) -> None:
    contract_credits = safe_float(settings.get("CONTRACT_CREDITS"))
    start_s = str(settings.get("CONTRACT_START_DATE") or "").strip()
    end_s = str(settings.get("CONTRACT_END_DATE") or "").strip()
    if contract_credits <= 0 or not start_s or not end_s:
        st.info(
            "Contract pacing is not configured. Set CONTRACT_CREDITS, CONTRACT_START_DATE and "
            "CONTRACT_END_DATE on the Admin page. Nothing is assumed."
        )
        return
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        st.error(f"Contract dates in SETTINGS are not YYYY-MM-DD: {start_s!r} / {end_s!r}.")
        return
    res = run(cost_sql.contract_consumed_credits(start_s), page=_PAGE, key="contract_consumed",
              tier="historical", source="ACCOUNT_USAGE.METERING_DAILY_HISTORY")
    if not guard(res, "No metering rows since the contract start."):
        return
    consumed = safe_float(res.df.iloc[0].get("CREDITS_BILLED_TO_DATE"))
    pace = contract_pace(consumed, contract_credits, start, end, account_today())
    if not pace.get("ok"):
        st.info(str(pace.get("reason")))
        return
    kpi_row([
        {"label": "Consumed", "value": f"{consumed:,.0f} cr", "delta": f"{pace['consumed_share']:.1f}% of contract"},
        {"label": "Contract clock", "value": f"{pace['time_share']:.1f}%", "help": f"{pace['days_remaining']} days remaining."},
        {"label": "Pace", "value": f"{pace['pace_ratio']:.2f}x",
         "delta": "burning fast" if pace["pace_ratio"] > 1 else "under pace",
         "delta_color": "inverse" if pace["pace_ratio"] > 1 else "normal"},
        {"label": "Projected term total", "value": f"{pace['projected_term_credits']:,.0f} cr",
         "delta": (f"+{pace['projected_overage_credits']:,.0f} cr overage" if pace["projected_overage_credits"] > 0 else "within contract"),
         "delta_color": "inverse" if pace["projected_overage_credits"] > 0 else "normal"},
    ])
    result_caption(res, note="Billed credits (cloud-services adjustment applied) since contract start.")

    st.markdown("**Steering to commit — the levers, in dollars per day**")
    idle_lv = run(insights_sql.idle_warehouse_analysis(30, "ALL"), page=_PAGE,
                  key="steer_idle", tier="historical",
                  source="idle advisor (30d, account-wide)")
    pats_lv = run(insights_sql.expensive_patterns_usd(30, "ALL", 10), page=_PAGE,
                  key="steer_pats", tier="historical",
                  source="recurring patterns (30d, account-wide)")
    rate_st = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    levers: dict = {}
    if idle_lv.usable():
        adv_st = idle_advisor(idle_lv.df, rate_st, 30)
        levers["Auto-suspend tuning (idle burn)"] = float(
            adv_st["PROJECTED_MONTHLY_IDLE_USD"].sum())
    if pats_lv.usable():
        top5 = pats_lv.df.head(5)
        levers["Top-5 recurring patterns (cache/materialize)"] = float(
            (pd.to_numeric(top5["CREDITS_PER_DAY"], errors="coerce").fillna(0) * rate_st).sum() * 30)
    plan = steering.steering_plan(
        projected_term_credits=pace["projected_term_credits"],
        contract_credits=contract_credits,
        days_remaining=pace["days_remaining"],
        rate_usd=rate_st, levers_monthly_usd=levers,
    )
    if not plan.get("ok"):
        st.info(str(plan.get("verdict")))
    else:
        (st.success if plan["gap_usd"] <= 0 or plan["coverage_pct"] >= 100 else st.warning)(
            plan["verdict"])
        if plan["rows"]:
            styled_table(pd.DataFrame(plan["rows"]), height=140)
        st.caption(
            "Lever estimates come straight from the idle advisor and recurring-pattern "
            "panels (execute them on Optimization & Savings). Estimates, not promises — "
            "the savings verifier proves them after the fact."
        )

    st.divider()
    st.markdown("**Renewal planner (what-if)**")
    panel_help(
        "Straight-line scenarios from the trailing 30-day burn — no seasonality is "
        "invented. Recommended commit = term consumption plus your buffer. Use it to "
        "walk into the renewal with a number instead of a feeling."
    )
    burn_res = run(mart_sql.fact_daily_spend(30), page=_PAGE, key="planner_burn",
                   tier="recent", source="FACT_METERING_DAILY")
    if guard(burn_res, "Need the metering fact loaded to plan (run the hourly task once)."):
        rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
        bdf = burn_res.df.copy()
        daily_usd = float(pd.to_numeric(bdf["CREDITS_BILLED"], errors="coerce").fillna(0).mean()) * rate_now
        remaining_usd = max(0.0, (contract_credits - consumed) * rate_now)
        col1, col2, col3 = st.columns(3)
        term_months = col1.slider("Next term (months)", 12, 36, 12, step=6, key="plan_term")
        buffer_pct = col2.slider("Safety buffer %", 0, 40, 15, step=5, key="plan_buffer")
        extra_credits = col3.number_input("What-if: add load (credits/day)", 0, 10000, 0,
                                          step=10, key="plan_extra",
                                          help="Hypothetical new workload (e.g. a planned XL "
                                               "warehouse). Reprojects every scenario and the "
                                               "exhaustion date.")
        daily_usd_adj = daily_usd + float(extra_credits) * rate_now
        rows = contract_planner.plan_scenarios(daily_usd_adj, term_months, buffer_pct, remaining_usd)
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={
                         "TERM_CONSUMPTION_USD": st.column_config.NumberColumn("Term consumption", format="$%.0f"),
                         "RECOMMENDED_COMMIT_USD": st.column_config.NumberColumn("Recommended commit", format="$%.0f"),
                         "DAILY_BURN_USD": st.column_config.NumberColumn("Daily burn", format="$%.2f"),
                     })
        st.caption(f"Basis: ${daily_usd:,.0f}/day observed over 30d at ${rate_now}/credit"
                   + (f" + ${float(extra_credits) * rate_now:,.0f}/day hypothetical load"
                      if extra_credits else "") + ". "
                   "Exhaustion applies to the current contract's remaining "
                   f"{contract_credits - consumed:,.0f} credits.")
