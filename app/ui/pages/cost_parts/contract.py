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
from app.data import cost_sql, insights_sql, mart27_sql, mart_sql, security_sql
from app.logic import contract_planner, steering
from app.logic.forecast import contract_pace
from app.logic.formulas import (
    account_today,
    blended_billed_usd,
    blended_credit_rate,
    format_usd,
    md_dollars,
    safe_float,
)
from app.logic.insights import idle_advisor, with_auto_suspend_settings
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    panel_help,
    result_caption,
    run_mart_first,
    styled_table,
    subsection_header,
)

_PAGE = "Cost & Contract"


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

# C4 (audit 2026-07-31): the window BOTH steering reads ask for. The monthly
# lever projections below MUST normalize by this same number — the two SQL
# builders keep the literal 30 inline (a mart-first perf lock greps for it),
# so a change here has to be mirrored there, deliberately, in one place.
_STEER_WINDOW_DAYS = 30

# C4: how much of an ESTIMATED lever a team actually banks. These are planning
# haircuts, not measurements — no realization rate has been proven on this
# account yet, so they are set from how executable each lever is:
#   idle 0.70    — one ALTER WAREHOUSE ... SET AUTO_SUSPEND per named warehouse;
#                  the residual is resume overhead the timer cannot remove.
#   patterns 0.40 — every query family needs someone to cache/materialize/rewrite
#                  it, and some of that spend is load-bearing work that never goes
#                  away; the app's own savings verifier is the arbiter after the fact.
# Deliberately conservative: an overstated lever tells an exec the renewal is
# handled when it is not. Replace both with the verifier's MEASURED realization
# once it has enough proven remediations to average.
_REALIZABLE_IDLE = 0.70
_REALIZABLE_PATTERNS = 0.40


def _whole_day_rows(df: pd.DataFrame) -> pd.DataFrame:
    """N1/#36: keep only COMPLETE (pre-today) metering rows for burn averaging.

    Today is still filling, so averaging it in drags every burn-derived figure
    (trailing daily, blended rate, projected term, planner scenarios) low. When
    the ONLY row available is today's partial one there is no complete-day
    baseline at all — the old fallback returned that partial row and every burn
    was then projected from a fraction of a day. Return the EMPTY frame instead
    and let callers surface "insufficient complete history" and skip the
    projection."""
    if "DAY" not in df.columns:
        return df
    return df[pd.to_datetime(df["DAY"], errors="coerce").dt.date < account_today()]


def _blended_rate(df: pd.DataFrame, rate_now: float, ai_rate: float) -> float:
    """C1/C4: ONE effective $/credit from the window's observed compute+AI mix.

    Hoisted out of the renewal planner (audit 2026-07-31): the steering gap was
    priced at the flat compute rate while the planner immediately below priced
    the same credits at the blended rate, so the page put two different dollar
    values on one overage. Falls back to the flat compute rate when the split
    columns are absent (older cache / live fallback shape)."""
    if not {"CREDITS_BILLED_OTHER", "CREDITS_BILLED_AI"} <= set(df.columns):
        return rate_now
    sum_other = float(pd.to_numeric(df["CREDITS_BILLED_OTHER"], errors="coerce").fillna(0).sum())
    sum_ai = float(pd.to_numeric(df["CREDITS_BILLED_AI"], errors="coerce").fillna(0).sum())
    return blended_credit_rate(sum_other, sum_ai, rate_now, ai_rate)


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
    subsection_header('Contract balance — billing truth (Snowflake org rate card, $)')
    burn = safe_float(summary.get("burn_per_day_usd"))
    runway = summary.get("runway_days")
    kpis = [
        {"label": "Remaining balance", "value": format_usd(summary["remaining_usd"]),
         "delta": f"as of {summary['as_of']}", "delta_color": "off"},
        # E1: the delta must name the number the burn actually divides by.
        # burn = total draw-down / non-top-up days, but this said "avg of N burn
        # day(s)" using the DROP-day count — on a weekend-idle account those two
        # differ by ~2x, so the caption claimed a burn the KPI had not computed.
        {"label": "Burn / day", "value": format_usd(burn) if burn > 0 else "n/a",
         "delta": (f"avg over {summary.get('burn_basis_days', 0)} non-top-up day(s)"
                   if burn > 0 else "no burn days observed"),
         "delta_color": "off",
         "help": ("Total draw-down divided by every non-top-up day in the window, "
                  f"including the {summary.get('burn_basis_days', 0) - summary.get('burn_days_observed', 0)} "
                  "idle day(s) that drew nothing. Renewal top-ups (balance rises) are "
                  "excluded from both sides.") if burn > 0 else None},
        {"label": "Runway at this burn",
         "value": f"{runway:,.0f} days" if runway is not None else "n/a",
         "delta": f"contract ends {end_note}" if end_note else None,
         "delta_color": "off",
         # E1: name the basis — this is the DOLLAR-balance answer to "when does
         # the contract run dry". The renewal planner below answers the same
         # question from SETTINGS credits x the blended rate, and the two can
         # disagree; each now says which basis it used.
         "help": "Org balance dollars / the burn at left — billing truth. The renewal "
                 "planner below re-answers this from SETTINGS contract credits."},
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
    # E1: the caption contradicted the (already corrected) math — the burn stopped
    # averaging "only down-days" when idle days were put back in the denominator.
    result_caption(bal, note="Dollars at your contract rates, straight from Snowflake "
                             "billing (refreshed daily; can lag up to a day). Burn/day "
                             "averages all non-top-up days — idle days count as the zero-burn "
                             "days they were, and renewal top-ups drop out entirely.")
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
    rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    # C1: project AI and compute credits on their own straight lines so both the
    # YTD and projected dollar figures price AI at the AI rate (the credit totals
    # shown are the two partitions summed, unchanged). Falls back to a flat total
    # if a live/older frame lacks the split columns.
    _split = "CREDITS_BILLED_OTHER" in cydf.columns and "CREDITS_BILLED_AI" in cydf.columns
    other_col = "CREDITS_BILLED_OTHER" if _split else "CREDITS_BILLED"
    ai_col = "CREDITS_BILLED_AI" if _split else None
    ytd_other = float(cydf[other_col].map(safe_float).sum())
    ytd_ai = float(cydf[ai_col].map(safe_float).sum()) if ai_col else 0.0
    # N1: today is partial — dividing trailing spend that includes it by a
    # day-count that treats today as whole biases the daily burn (and the
    # year projection) low. YTD keeps today's actual; the burn uses whole days.
    tail = cydf[(cydf["DAY"] >= today - timedelta(days=30)) & (cydf["DAY"] < today)]
    n_tail = max(len(tail), 1)
    burn_other = float(tail[other_col].map(safe_float).sum()) / n_tail
    burn_ai = (float(tail[ai_col].map(safe_float).sum()) / n_tail) if ai_col else 0.0
    days_left = (date(today.year, 12, 31) - today).days
    ytd = ytd_other + ytd_ai
    proj_other = ytd_other + burn_other * days_left
    proj_ai = ytd_ai + burn_ai * days_left
    projected = proj_other + proj_ai
    kpi_row([
        {"label": f"{today.year} YTD (billed)", "value": f"{ytd:,.0f} cr",
         "delta": format_usd(blended_billed_usd(ytd_other, ytd_ai, rate_now, ai_rate)),
         "delta_color": "off"},
        {"label": f"Projected {today.year} total", "value": f"{projected:,.0f} cr",
         "delta": format_usd(blended_billed_usd(proj_other, proj_ai, rate_now, ai_rate)),
         "delta_color": "off",
         "help": "Straight-line: YTD billed credits + trailing-30d daily burn x "
                 f"{days_left} days remaining (early in a year the burn basis is "
                 "YTD itself). AI/Cortex credits price at the AI rate, the rest at "
                 "the compute rate. Seasonality-aware month-end projections live on "
                 "Overview; contract pacing below is term-aware."},
    ])


def _rate_card_reconciliation(settings: dict) -> None:
    """Billing truth vs the app model, for THIS account (moved from Admin, v4.48).

    Lives directly under the year projection because it audits the very rate
    the projection (and the pacing and planner below) price with: a steady
    drift here means every dollar on this page is scaled wrong."""
    subsection_header('Billing truth vs app model (this account)')
    st.caption(
        "Org rate-card dollars for THIS account vs the app's credits x configured rate. "
        "The compute bucket should track closely; the residual is rate-card reality "
        "(storage, transfer, serverless, discounts), not a bug in either number."
    )
    org_m = run(cost_sql.org_account_month_usd(2), page=_PAGE, key="org_month_this",
                tier="historical", source="ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (this account)")
    # triage #6: compute-ONLY so the model side matches the org COMPUTE_USD side
    # (AI/Cortex priced at the compute rate was biasing DELTA_PCT up on any account
    # with Cortex usage, and the caption invited "fixing" the global rate).
    model_m = run(mart_sql.fact_daily_spend_compute(70), page=_PAGE, key="fact_daily_compute_70",
                  tier="recent", source="FACT_METERING_DAILY (compute only, excl AI/Cortex)")
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
            # Effective realized rate = what the org actually charged per billed
            # compute credit this month. This is the number to reconcile
            # CREDIT_PRICE_USD (SETTINGS) against when the model-vs-org gap is steady.
            credits_month = (model_usd / rate_now) if rate_now else 0.0
            eff_rate = (org_usd / credits_month) if credits_month else None
            rows_rc.append({
                "MONTH": month.strftime("%Y-%m") if pd.notna(month) else "?",
                "ORG_COMPUTE_USD": round(org_usd, 2),
                "APP_MODEL_USD": round(model_usd, 2),
                "DELTA_PCT": round(drift, 2) if drift is not None else None,
                "EFF_RATE": round(eff_rate, 3) if eff_rate is not None else None,
                "ORG_AI_USD": round(safe_float(orow.get("AI_USD")), 2),
                "ORG_STORAGE_USD": round(safe_float(orow.get("STORAGE_USD")), 2),
                "ORG_TRANSFER_USD": round(safe_float(orow.get("TRANSFER_USD")), 2),
                "ORG_ADJUSTMENT_USD": round(safe_float(orow.get("ADJUSTMENT_USD")), 2),
                "ORG_TOTAL_USD": round(safe_float(orow.get("TOTAL_USD")), 2),
            })
        styled_table(pd.DataFrame(rows_rc), column_config={
            "DELTA_PCT": st.column_config.NumberColumn("Model vs org %", format="%.2f%%"),
            "EFF_RATE": st.column_config.NumberColumn("Effective $/cr", format="$%.3f")})
        st.caption(
            f"Model = FACT_METERING_DAILY billed credits x ${rate_now:.2f} (SETTINGS). The current "
            "month is partial on both sides; judge the prior full month. The **Effective "
            "rate** column (org compute ÷ billed credits) is the realized per-credit rate — "
            "reconcile CREDIT_PRICE_USD to it on Admin → Settings when the gap is steady."
        )


def _org_accounts_spend() -> None:
    """Accounts Spend Summary from ORGANIZATION_USAGE (moved from Admin, v4.48).

    Org-grain billed spend is cost analysis, not app plumbing — it sits with
    the contract's billing truth, on the one Cost section that is already
    company-agnostic (org data has no company grain)."""
    subsection_header('Org accounts spend (all accounts, billed currency)')
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
        df.rename(columns={"USAGE_IN_CURRENCY": "USD"}), "DAY", "ACCOUNT_NAME", "USD",
        takeaway=False)  # rec35: amounts are the org rate-card CURRENCY, not $ — the caption below states the top
    st.caption(f"Amounts are {currency} from the org rate card, not credits x app rate.")
    pivot = (df.groupby(["ACCOUNT_NAME", "SERVICE_TYPE"], as_index=False)["USAGE_IN_CURRENCY"].sum()
             .sort_values(["ACCOUNT_NAME", "USAGE_IN_CURRENCY"], ascending=[True, False]))
    styled_table(pivot,  # rec21
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
    # C7 coverage: the mart leg is gated upstream (FACT_FIRST_DAY <= start), so it
    # is always complete. The live fallback only sees ~365d of METERING_DAILY_HISTORY;
    # when its earliest in-window day is AFTER the contract start, consumed is a
    # FLOOR, not the true total, and pace / projected-term read low. Detect the gap
    # from whichever leg served (mart: FACT_FIRST_DAY, live: FIRST_DAY) and label it.
    _row = res.df.iloc[0]
    # Both legs expose an UNFILTERED earliest retained day (mart: FACT_FIRST_DAY,
    # live: SOURCE_FIRST_DAY) — the retention floor, not a contract-filtered MIN —
    # so a quiet-start contract is not misread as a coverage gap (r14 #8).
    _first_raw = (_row.get("FACT_FIRST_DAY") if "FACT_FIRST_DAY" in res.df.columns
                  else _row.get("SOURCE_FIRST_DAY"))
    coverage_from = None
    _fd = pd.to_datetime(_first_raw, errors="coerce")
    if pd.notna(_fd) and _fd.date() > start:
        coverage_from = _fd.date()
    # N11: project the term on the SAME trailing-30d burn the renewal planner (and
    # the year strip) use, so the prominent "Projected term total" can't disagree
    # with the planner below. Excludes today's partial day (N1); reuses the
    # planner's cache key so it costs no extra query.
    _burn = run(mart_sql.fact_daily_spend(30), page=_PAGE, key="planner_burn",
                tier="recent", source="FACT_METERING_DAILY")
    rate_now = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    # C4: the burn frame is read ONCE here and shared by everything below it —
    # the projected term, the steering gap and the renewal planner. Hoisting the
    # blended $/credit out of the planner is the point: the gap used to be priced
    # at the flat compute rate while the planner beside it used the blended rate,
    # so one overage carried two dollar values on the same screen.
    _burn_df = _whole_day_rows(_burn.df.copy()) if _burn.usable() else None
    eff_rate = _blended_rate(_burn_df, rate_now, ai_rate) if _burn_df is not None else rate_now
    _trailing_daily = None
    # #36: only when a COMPLETE day exists — a mean over an empty whole-day frame
    # is NaN, and projecting a term from it would be projecting from nothing.
    if _burn_df is not None and not _burn_df.empty and "CREDITS_BILLED" in _burn_df.columns:
        _trailing_daily = float(pd.to_numeric(_burn_df["CREDITS_BILLED"], errors="coerce").fillna(0).mean())
    pace = contract_pace(consumed, contract_credits, start, end, account_today(),
                         trailing_daily_credits=_trailing_daily)
    if not pace.get("ok"):
        st.info(str(pace.get("reason")))
        return
    if coverage_from:
        st.warning(
            f"Retained metering history only reaches {coverage_from.isoformat()}, after the "
            f"contract start ({start.isoformat()}). **Consumed is a floor** — pace and "
            "projected-term understate. "
            + ("Use the org REMAINING_BALANCE panel above for the dollar truth."
               if org_shown else "Backfill FACT_METERING_DAILY to the contract start for exact pacing.")
        )
    _floor = " (floor — see coverage note)" if coverage_from else ""
    kpi_row([
        {"label": "Consumed", "value": f"{consumed:,.0f} cr",
         "delta": f"{pace['consumed_share']:.1f}% of contract",
         # r6-bug14: share-of-contract is informational, not a good/bad move — a
         # near-exhaustion 95% must NOT render as a reassuring green up-arrow.
         "delta_color": "off",
         "help": f"Billed credits since contract start.{_floor}"},
        {"label": "Contract clock", "value": f"{pace['time_share']:.1f}%", "help": f"{pace['days_remaining']} days remaining."},
        {"label": "Pace", "value": f"{pace['pace_ratio']:.2f}x",
         "delta": "burning fast" if pace["pace_ratio"] > 1 else "under pace",
         "delta_color": "inverse" if pace["pace_ratio"] > 1 else "normal"},
        {"label": "Projected term total", "value": f"{pace['projected_term_credits']:,.0f} cr",
         "delta": (f"+{pace['projected_overage_credits']:,.0f} cr overage" if pace["projected_overage_credits"] > 0 else "within contract"),
         "delta_color": "inverse" if pace["projected_overage_credits"] > 0 else "normal",
         "help": f"Booked consumption + remaining days at the {pace.get('basis', 'trailing-30d burn')} — "
                 "the same basis as the renewal planner below and the year projection above "
                 "(no longer the optimistic lifetime average)."},
    ])
    result_caption(res, note="Billed credits (cloud-services adjustment applied) since contract start.")
    # C8: a Snowflake capacity commitment is a DOLLAR balance drawn by compute,
    # storage AND transfer; this pacing counts credit-billed services only
    # (compute + serverless + AI). Storage and data-transfer dollars draw the same
    # commitment and are not in these credits, so the real balance paces to exhaust
    # earlier than shown. The org REMAINING_BALANCE panel above is the dollar truth.
    st.caption(
        "Credits burn only — storage and data-transfer dollars draw the same "
        + ("commitment but are not counted here; the org balance panel above is the dollar truth."
           if org_shown
           else "commitment but are not counted here. Grant ORGANIZATION_USAGE to see the dollar balance.")
    )

    subsection_header('Steering to commit — the levers, in dollars per day')
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
    # C4: the LEVERS are warehouse compute credits (idle burn, attributed pattern
    # credits) — they price at the compute rate, never the blended one. The GAP is
    # projected-term vs contract credits, a compute+AI mix, so it prices at the same
    # blended eff_rate the renewal planner below uses. Two rates, on purpose.
    levers: dict = {}
    if idle_lv.usable():
        _steer_whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="steer_wh_settings",
                         tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        _steer_idle = with_auto_suspend_settings(
            idle_lv.df,
            _steer_whs.df if _steer_whs.ok and not _steer_whs.empty else pd.DataFrame(),
        )
        adv_st = idle_advisor(_steer_idle, rate_now, _STEER_WINDOW_DAYS)
        # Gross flagged idle, already-tuned residuals, and unknown settings are
        # measured waste, but they are not executable contract-steering levers.
        _actionable = adv_st[adv_st["ACTIONABLE"]]
        _idle_monthly = float(pd.to_numeric(
            _actionable.get("ACTIONABLE_MONTHLY_USD", pd.Series(dtype=float)),
            errors="coerce").fillna(0).sum()) * _REALIZABLE_IDLE
        if _idle_monthly > 0:
            levers[f"Auto-suspend tuning, verified targets ({_REALIZABLE_IDLE:.0%} realizable)"] = _idle_monthly
    if pats_lv.usable():
        top5 = pats_lv.df.head(5)
        if "CREDITS_PER_DAY" in top5.columns:      # live shape: per-day allocated
            _pat_monthly = float((pd.to_numeric(top5["CREDITS_PER_DAY"],
                                                errors="coerce").fillna(0) * rate_now).sum() * 30)
        else:                                      # mart shape: measured window total
            # C4: normalize by the window actually asked for, then re-scale to a
            # month. The old `/ 30 * 30` cancelled to a raw window TOTAL that only
            # happened to be monthly because the read is 30d — widening the steering
            # window would have silently relabelled a bigger total as "monthly".
            _pat_monthly = (float(pd.to_numeric(top5.get("CREDITS"), errors="coerce").fillna(0).sum())
                            / _STEER_WINDOW_DAYS * 30 * rate_now)
        _pat_monthly *= _REALIZABLE_PATTERNS
        if _pat_monthly > 0:
            levers[f"Top-5 recurring patterns ({_REALIZABLE_PATTERNS:.0%} realizable)"] = _pat_monthly
    plan = steering.steering_plan(
        projected_term_credits=pace["projected_term_credits"],
        contract_credits=contract_credits,
        days_remaining=pace["days_remaining"],
        rate_usd=eff_rate, levers_monthly_usd=levers,
    )
    if not plan.get("ok"):
        st.info(str(plan.get("verdict")))
    else:
        (st.success if plan["gap_usd"] <= 0 or plan["coverage_pct"] >= 100 else st.warning)(
            plan["verdict"])
        if plan["rows"]:
            styled_table(pd.DataFrame(plan["rows"]), height=140)
        # $-escape: the rate pair below is two '$' tokens in one markdown sink
        st.caption(md_dollars(
            "Lever estimates come from the idle advisor (settings-verified actionable warehouses only) and the "
            "recurring-pattern panel — execute them on Optimization & Savings. Each is shown "
            f"AFTER a realizable haircut ({_REALIZABLE_IDLE:.0%} idle, {_REALIZABLE_PATTERNS:.0%} "
            "patterns), so the coverage % is what the levers plausibly bank, not what they "
            "theoretically total. Levers price at the compute rate "
            f"(${rate_now:,.2f}/credit); the gap prices at the blended ${eff_rate:,.2f}/credit "
            "the renewal planner uses. Estimates, not promises — the savings verifier proves "
            "them after the fact."))

    st.divider()
    subsection_header('Renewal planner (what-if)')
    panel_help(
        "Straight-line scenarios from the trailing 30-day burn — no seasonality is "
        "invented. Recommended commit = term consumption plus your buffer. Use it to "
        "walk into the renewal with a number instead of a feeling. Basis is credit-billed "
        "services (compute + serverless + AI); storage and data-transfer dollars draw the "
        "same commitment, so the recommended commit is biased slightly short — add their "
        "share (org rate-card panel above) before signing."
    )
    burn_res = run(mart_sql.fact_daily_spend(30), page=_PAGE, key="planner_burn",
                   tier="recent", source="FACT_METERING_DAILY")
    if guard(burn_res, "Need the metering fact loaded to plan (run the hourly task once)."):
        # C1/C4: rate_now, ai_rate, the whole-day filter (N1) and the blended
        # eff_rate are all hoisted above the steering block now — the planner
        # reuses the SAME numbers instead of re-deriving its own, which is how
        # the gap and the planner came to disagree in the first place. Same
        # cache key as `_burn`, so this read costs nothing extra.
        bdf = _burn_df if _burn_df is not None else _whole_day_rows(burn_res.df.copy())
        # #36: if the only metering row is today's partial one, _whole_day_rows
        # returns empty — there is no complete-day burn baseline, so withhold the
        # planner instead of projecting a term from a fraction of a day.
        if bdf is None or bdf.empty:
            st.info(
                "Only today's partial metering row is available — that is not a "
                "complete-day burn baseline, so the renewal planner is withheld "
                "until at least one whole day of history lands."
            )
            return
        daily_usd = float(pd.to_numeric(bdf["CREDITS_BILLED"], errors="coerce").fillna(0).mean()) * eff_rate
        remaining_usd = max(0.0, (contract_credits - consumed) * eff_rate)
        col1, col2, col3 = st.columns(3)
        term_months = col1.slider("Next term (months)", 12, 36, 12, step=6, key="plan_term")
        buffer_pct = col2.slider("Safety buffer %", 0, 40, 15, step=5, key="plan_buffer")
        extra_credits = col3.number_input("What-if: add load (credits/day)", 0, 10000, 0,
                                          step=10, key="plan_extra",
                                          help="Hypothetical new workload (e.g. a planned XL "
                                               "warehouse). Reprojects every scenario and the "
                                               "exhaustion date.")
        daily_usd_adj = daily_usd + float(extra_credits) * eff_rate
        rows = contract_planner.plan_scenarios(daily_usd_adj, term_months, buffer_pct, remaining_usd)
        styled_table(pd.DataFrame(rows),  # rec21
                     column_config={
                         "TERM_CONSUMPTION_USD": st.column_config.NumberColumn("Term consumption", format="$%.0f"),
                         "RECOMMENDED_COMMIT_USD": st.column_config.NumberColumn("Recommended commit", format="$%.0f"),
                         "DAILY_BURN_USD": st.column_config.NumberColumn("Daily burn", format="$%.2f"),
                     })
        # E1: the page answers "when does the money run out?" twice, on two bases —
        # say which is which instead of leaving a reader to reconcile them.
        # CURRENT_CONTRACT_EXHAUSTED here = SETTINGS credits still unconsumed, priced
        # at the blended rate; the org panel's runway = the actual dollar balance
        # Snowflake bills down (which also carries storage/transfer, so it lands
        # EARLIER). We deliberately do NOT feed org dollars into plan_scenarios:
        # credits-remaining and balance-remaining are different quantities, and
        # silently swapping one for the other would make the credits column a lie.
        # $-escape: 4-5 dollar figures in one caption pair into LaTeX math spans
        st.caption(md_dollars(
                   f"Basis: ${daily_usd:,.0f}/day observed over 30d at ${eff_rate:,.2f}/credit "
                   f"blended (compute ${rate_now}, AI ${ai_rate})"
                   + (f" + ${float(extra_credits) * eff_rate:,.0f}/day hypothetical load"
                      if extra_credits else "") + ". "
                   "Exhaustion here is CREDITS-based: the current contract's remaining "
                   f"{contract_credits - consumed:,.0f} credits (SETTINGS) at that rate."
                   + (" The 'Runway at this burn' KPI above is the BALANCE-based answer "
                      "(org billing dollars, which also carry storage and transfer) — it "
                      "normally lands earlier, and it is the one to trust."
                      if org_shown else
                      " Grant ORGANIZATION_USAGE for the balance-based answer, which "
                      "also counts the storage and transfer dollars this one cannot see.")))
