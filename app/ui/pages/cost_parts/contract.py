"""Cost & Contract — the Contract & Forecast section bodies (year projection,
rate-card reconciliation, org billing truth, pacing, steering, renewal planner).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.core.query import run
from app.data import cost_sql, insights_sql, mart27_sql, mart_sql
from app.logic import contract_planner, steering
from app.logic.forecast import contract_pace
from app.logic.formulas import account_today, format_usd, safe_float
from app.logic.insights import idle_advisor
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    panel_help,
    result_caption,
    run_mart_first,
    styled_table,
)

_PAGE = "Cost & Contract"


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

def _org_truth_panel() -> bool:
    """Contract balance straight from Snowflake billing metadata.

    ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY is the balance that burns
    down each day (the number Snowsight shows under Admin → Cost
    Management); CONTRACT_ITEMS carries the committed amount and term
    dates. Zero configuration — when the role can see the views this panel
    is the truth, and the SETTINGS-based credits pacing below becomes the
    steering layer. Returns True when it rendered."""
    bal = run(cost_sql.org_remaining_balance(120), page=_PAGE, key="org_balance",
              tier="historical", source="ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY")
    if not bal.usable():
        st.caption(
            "Snowflake's contract balance (ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY) "
            "isn't visible to this role, so pacing uses SETTINGS below. Granting org "
            "visibility unlocks balance, burn, and runway automatically."
        )
        return False
    summary = contract_planner.remaining_balance_summary(bal.df)
    if not summary.get("ok"):
        st.caption(f"Org balance view returned no usable rows — {summary.get('reason')}")
        return False
    items = run(cost_sql.org_contract_items(), page=_PAGE, key="org_items",
                tier="metadata", source="ORGANIZATION_USAGE.CONTRACT_ITEMS")
    end_note = None
    if items.usable() and "END_DATE" in items.df.columns:
        ends = pd.to_datetime(items.df["END_DATE"], errors="coerce").dropna()
        if len(ends):
            end_note = str(ends.max().date())
    st.markdown("**Contract balance — billing truth (Snowflake org rate card, $)**")
    burn = safe_float(summary.get("burn_per_day_usd"))
    runway = summary.get("runway_days")
    kpis = [
        {"label": "Remaining balance", "value": format_usd(summary["remaining_usd"]),
         "delta": f"as of {summary['as_of']}", "delta_color": "off"},
        {"label": "Burn / day", "value": format_usd(burn) if burn > 0 else "n/a",
         "delta": f"avg of {summary['burn_days_observed']} burn day(s)" if burn > 0 else "no burn days observed",
         "delta_color": "off"},
        {"label": "Runway at this burn",
         "value": f"{runway:,.0f} days" if runway is not None else "n/a",
         "delta": f"contract ends {end_note}" if end_note else None,
         "delta_color": "off"},
    ]
    on_demand = safe_float(summary.get("on_demand_usd"))
    if on_demand < 0:
        kpis.append({"label": "On-demand overrun", "value": format_usd(-on_demand),
                     "severity": "warn",
                     "help": "Usage past the capacity commitment, billed on demand."})
    kpi_row(kpis)
    daily = bal.df.groupby("DAY", as_index=False)["TOTAL_REMAINING"].sum()
    charts.daily_metric_line(daily, "DAY", "TOTAL_REMAINING", title="Remaining balance ($)")
    if items.usable():
        styled_table(items.df, height=160)
    result_caption(bal, note="Dollars at your contract rates, straight from Snowflake "
                             "billing (refreshed daily; can lag up to a day). Burn/day "
                             "averages only down-days, so renewal top-ups don't distort it.")
    return True


def _year_projection_strip(settings: dict) -> None:
    """Calendar-year framing (COST_DB recon R9): the exec asks "what will
    this YEAR total?" Straight-line here, honestly labeled — the
    seasonality-aware month-end engines live on Overview."""
    cy = run(mart_sql.fact_daily_spend_year(), page=_PAGE, key="cy_projection",
             tier="recent", source="FACT_METERING_DAILY (calendar year)")
    if not cy.usable():
        return
    cydf = cy.df.copy()
    cydf["DAY"] = pd.to_datetime(cydf["DAY"], errors="coerce").dt.date
    today = account_today()
    ytd = float(cydf["CREDITS_BILLED"].map(safe_float).sum())
    tail = cydf[cydf["DAY"] >= today - timedelta(days=30)]
    burn = (float(tail["CREDITS_BILLED"].map(safe_float).sum()) / max(len(tail), 1))
    days_left = (date(today.year, 12, 31) - today).days
    rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    projected = ytd + burn * days_left
    kpi_row([
        {"label": f"{today.year} YTD (billed)", "value": f"{ytd:,.0f} cr",
         "delta": format_usd(ytd * rate_now), "delta_color": "off"},
        {"label": f"Projected {today.year} total", "value": f"{projected:,.0f} cr",
         "delta": format_usd(projected * rate_now), "delta_color": "off",
         "help": "Straight-line: YTD billed credits + trailing-30d daily burn x "
                 f"{days_left} days remaining (early in a year the burn basis is "
                 "YTD itself). Seasonality-aware month-end projections live on "
                 "Overview; contract pacing below is term-aware."},
    ])


def _rate_card_reconciliation(settings: dict) -> None:
    """Billing truth vs the app model, for THIS account (moved from Admin, v4.48).

    Lives directly under the year projection because it audits the very rate
    the projection (and the pacing and planner below) price with: a steady
    drift here means every dollar on this page is scaled wrong."""
    st.markdown("**Billing truth vs app model (this account)**")
    st.caption(
        "Org rate-card dollars for THIS account vs the app's credits x configured rate. "
        "The compute bucket should track closely; the residual is rate-card reality "
        "(storage, transfer, serverless, discounts), not a bug in either number."
    )
    org_m = run(cost_sql.org_account_month_usd(2), page=_PAGE, key="org_month_this",
                tier="historical", source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (this account)")
    model_m = run(mart_sql.fact_daily_spend(70), page=_PAGE, key="fact_daily_45",
                  tier="recent", source="FACT_METERING_DAILY")
    if not org_m.usable():
        st.info("Needs ORGANIZATION_USAGE visibility (the org accounts panel below "
                "has the grant).")
    elif not model_m.usable():
        st.info("Needs the daily metering facts (V002) for the model side.")
    else:
        rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
        mdf = model_m.df.copy()
        mdf["MONTH"] = pd.to_datetime(mdf["DAY"], errors="coerce").dt.to_period("M").dt.to_timestamp()
        model_by_month = mdf.groupby("MONTH")["CREDITS_BILLED"].sum() * rate_now
        odf = org_m.df.copy()
        odf["MONTH"] = pd.to_datetime(odf["MONTH"], errors="coerce")
        rows_rc = []
        for _, orow in odf.iterrows():
            month = orow["MONTH"]
            model_usd = float(model_by_month.get(month, 0.0))
            org_usd = safe_float(orow.get("COMPUTE_USD"))
            drift = (100.0 * (model_usd - org_usd) / org_usd) if org_usd else None
            rows_rc.append({
                "MONTH": month.strftime("%Y-%m") if pd.notna(month) else "?",
                "ORG_COMPUTE_USD": round(org_usd, 2),
                "APP_MODEL_USD": round(model_usd, 2),
                "DELTA_PCT": round(drift, 2) if drift is not None else None,
                "ORG_AI_USD": round(safe_float(orow.get("AI_USD")), 2),
                "ORG_STORAGE_USD": round(safe_float(orow.get("STORAGE_USD")), 2),
                "ORG_TRANSFER_USD": round(safe_float(orow.get("TRANSFER_USD")), 2),
                "ORG_ADJUSTMENT_USD": round(safe_float(orow.get("ADJUSTMENT_USD")), 2),
                "ORG_TOTAL_USD": round(safe_float(orow.get("TOTAL_USD")), 2),
            })
        styled_table(pd.DataFrame(rows_rc), column_config={
            "DELTA_PCT": st.column_config.NumberColumn("Model vs org %", format="%.2f%%")})
        st.caption(
            f"Model = FACT_METERING_DAILY billed credits x ${rate_now:.2f} (SETTINGS). The current "
            "month is partial on both sides; judge the prior month. A steady gap means the "
            "contract rate in SETTINGS no longer matches the rate card — fix it on Admin → Settings."
        )


def _org_accounts_spend() -> None:
    """Accounts Spend Summary from ORGANIZATION_USAGE (moved from Admin, v4.48).

    Org-grain billed spend is cost analysis, not app plumbing — it sits with
    the contract's billing truth, on the one Cost section that is already
    company-agnostic (org data has no company grain)."""
    st.markdown("**Org accounts spend (all accounts, billed currency)**")
    st.caption(
        "Org-level billed spend in currency per account and usage type — the same source "
        "as Snowsight's Accounts Spend Summary (USAGE_IN_CURRENCY_DAILY, lags up to 24-72h)."
    )
    res = run(cost_sql.org_usage_in_currency(30), page=_PAGE, key="org_spend",
              tier="historical", source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY")
    if not res.ok:
        st.info(
            "ORGANIZATION_USAGE is not visible to this role/account. Grant the "
            "ORGANIZATION_USAGE_VIEWER application role (or enable org views on this account) "
            f"to light this up. Detail: {res.error}"
        )
        return
    if res.empty:
        st.info("No org usage rows in the last 30 days.")
        return
    df = res.df.copy()
    df["USAGE_IN_CURRENCY"] = pd.to_numeric(df["USAGE_IN_CURRENCY"], errors="coerce").fillna(0)
    currency = str(df["CURRENCY"].dropna().iloc[0]) if df["CURRENCY"].notna().any() else "USD"
    total = float(df["USAGE_IN_CURRENCY"].sum())
    by_account = (df.groupby("ACCOUNT_NAME", as_index=False)["USAGE_IN_CURRENCY"].sum()
                  .sort_values("USAGE_IN_CURRENCY", ascending=False))
    kpi_row([
        {"label": f"Org spend (30d, {currency})", "value": f"{total:,.0f}",
         "help": "Billed currency across every account in the organization."},
        {"label": "Accounts", "value": f"{by_account['ACCOUNT_NAME'].nunique()}"},
        {"label": "Largest account",
         "value": str(by_account.iloc[0]["ACCOUNT_NAME"]) if not by_account.empty else "n/a",
         "delta": f"{float(by_account.iloc[0]['USAGE_IN_CURRENCY']):,.0f} {currency}" if not by_account.empty else None,
         "delta_color": "off"},
    ])
    charts.daily_stacked_usd(
        df.rename(columns={"USAGE_IN_CURRENCY": "USD"}), "DAY", "ACCOUNT_NAME", "USD")
    st.caption(f"Amounts are {currency} from the org rate card, not credits x app rate.")
    pivot = (df.groupby(["ACCOUNT_NAME", "SERVICE_TYPE"], as_index=False)["USAGE_IN_CURRENCY"].sum()
             .sort_values(["ACCOUNT_NAME", "USAGE_IN_CURRENCY"], ascending=[True, False]))
    st.dataframe(pivot, hide_index=True, use_container_width=True,
                 column_config={"USAGE_IN_CURRENCY": st.column_config.NumberColumn(
                     f"Spend ({currency})", format="%.2f")})
    result_caption(res)


def _contract_tab(settings: dict) -> None:
    _year_projection_strip(settings)
    _rate_card_reconciliation(settings)
    st.divider()
    org_shown = _org_truth_panel()
    _org_accounts_spend()
    contract_credits = safe_float(settings.get("CONTRACT_CREDITS"))
    start_s = str(settings.get("CONTRACT_START_DATE") or "").strip()
    end_s = str(settings.get("CONTRACT_END_DATE") or "").strip()
    if contract_credits <= 0 or not start_s or not end_s:
        if org_shown:
            st.caption(
                "For credits-grain pacing and the steering levers below the balance, set "
                "CONTRACT_CREDITS, CONTRACT_START_DATE and CONTRACT_END_DATE on the Admin page."
            )
        else:
            st.info(
                "Contract pacing is not configured. Set CONTRACT_CREDITS, CONTRACT_START_DATE and "
                "CONTRACT_END_DATE on the Admin page. Nothing is assumed."
            )
        return
    if org_shown:
        st.divider()
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        st.error(f"Contract dates in SETTINGS are not YYYY-MM-DD: {start_s!r} / {end_s!r}.")
        return
    # r13 #7: facts first — the live reader rescans METERING_DAILY_HISTORY
    # from contract start on every cold cache. mart_accept verifies the fact
    # actually REACHES the contract start before trusting the sum.
    res = run_mart_first(
        mart_sql.fact_contract_consumed(start_s),
        cost_sql.contract_consumed_credits(start_s),
        page=_PAGE, key="contract_consumed",
        mart_source="FACT_METERING_DAILY (contract window)",
        live_source="ACCOUNT_USAGE.METERING_DAILY_HISTORY (coverage fallback)",
        mart_accept=lambda df: (not df.empty and df.iloc[0].get("FACT_FIRST_DAY") is not None
                                and str(pd.to_datetime(df.iloc[0]["FACT_FIRST_DAY"]).date()) <= start_s))
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
    # r13 #6: mart-first steering — the live idle join and pattern
    # allocation were the last Account Usage scans on this section's
    # default render (fleet slow-key evidence 2026-07-11).
    idle_lv = run_mart_first(
        mart27_sql.eff_idle_analysis(30, "ALL"),
        insights_sql.idle_warehouse_analysis(30, "ALL"),
        page=_PAGE, key="steer_idle",
        mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (idle contract)",
        live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")
    pats_lv = run_mart_first(
        mart27_sql.pattern_cost(30, "ALL", 10),
        insights_sql.expensive_patterns_usd(30, "ALL", 10),
        page=_PAGE, key="steer_pats",
        mart_source="MART_PATTERN_COST_DAILY (measured, V037)",
        live_source="QUERY_HISTORY x METERING (hour-share, live fallback)")
    rate_st = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    levers: dict = {}
    if idle_lv.usable():
        adv_st = idle_advisor(idle_lv.df, rate_st, 30)
        levers["Auto-suspend tuning (idle burn)"] = float(
            adv_st["PROJECTED_MONTHLY_IDLE_USD"].sum())
    if pats_lv.usable():
        top5 = pats_lv.df.head(5)
        if "CREDITS_PER_DAY" in top5.columns:      # live shape: per-day allocated
            _pat_monthly = float((pd.to_numeric(top5["CREDITS_PER_DAY"],
                                                errors="coerce").fillna(0) * rate_st).sum() * 30)
        else:                                      # mart shape: measured window total
            _pat_monthly = float(pd.to_numeric(top5.get("CREDITS"),
                                               errors="coerce").fillna(0).sum() / 30 * 30 * rate_st)
        levers["Top-5 recurring patterns (cache/materialize)"] = _pat_monthly
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
