"""Contract renewal scenarios from observed burn. Pure module.

Straight-line projections deliberately labeled as such — no seasonality is
invented. If the 365-day facts backfill lands, a monthly index can replace
the flat daily rate.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.logic.formulas import account_today

# r33: cap the exhaustion horizon (~10y) so a near-idle account's huge days-left can't overflow
# anchor + timedelta past date.max; anything beyond is reported ">10y", not a fabricated date.
_HORIZON_DAYS = 3653
# Next-Fifty #19 (cost-08): the exec runway from billing truth
ORG_BALANCE_DAYS = 120          # the ONE window every org-balance read uses -> one shared cache entry
BALANCE_MAX_LAG_DAYS = 3        # ORG_USAGE latency up to ~72h (metric_registry org_reconciliation)
RUNWAY_GAP_DISCLOSE_PCT = 15.0  # disclose when balance vs credits runways differ by more


def remaining_balance_summary(df: pd.DataFrame, burn_window_days: int = 14) -> dict:
    """Summarize ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY rows.

    Expects DAY + TOTAL_REMAINING (multiple contracts per day are summed).
    Burn/day averages only the day-over-day DROPS in the trailing window —
    a renewal top-up is a rise, and treating it as negative burn would poison
    the runway. Returns ok=False (with a reason) whenever the frame cannot
    support the math; the UI degrades instead of inventing a number.
    """
    if df is None or len(df) == 0 or "TOTAL_REMAINING" not in getattr(df, "columns", ()):
        return {"ok": False, "reason": "No balance rows visible."}
    grouped = (df.assign(_v=pd.to_numeric(df["TOTAL_REMAINING"], errors="coerce"))
                 .groupby("DAY")["_v"].sum().dropna().sort_index())
    if len(grouped) == 0:
        return {"ok": False, "reason": "No balance rows visible."}
    # Keep the NATIVE last-day key (matches df["DAY"]'s dtype) for the balance /
    # as-of / on-demand lookups below; the burn math uses a datetime-reindexed copy.
    last_day = grouped.index[-1]
    remaining = float(grouped.iloc[-1])
    as_of = str(pd.Timestamp(last_day).date()) if last_day is not None else "n/a"
    on_demand = 0.0
    if "ON_DEMAND_CONSUMPTION_BALANCE" in df.columns:
        od = pd.to_numeric(df.loc[df["DAY"] == last_day, "ON_DEMAND_CONSUMPTION_BALANCE"],
                           errors="coerce").fillna(0)
        on_demand = float(od.sum())
    # #35: reindex to a CONTIGUOUS daily index before diff(). Without it, diff()
    # steps between whichever days are PRESENT, so a multi-day observation gap (the
    # org view didn't refresh) collapses into one diff step — a 3-day, 30-credit
    # draw-down then reads as a single 30/day burn instead of 10/day, and the gap
    # days vanish from the denominator, halving the runway. Reindexing forward-fills
    # the balance across missing days (unchanged until the next observation), so the
    # whole draw-down is spread across every calendar day it truly spanned and each
    # gap day counts as the zero-burn day it was.
    daily = grouped.copy()
    daily.index = pd.to_datetime(daily.index, errors="coerce")
    daily = daily[daily.index.notna()].sort_index()
    if len(daily) >= 2:
        daily = daily.reindex(
            pd.date_range(daily.index.min(), daily.index.max(), freq="D")).ffill()
    deltas = daily.diff().dropna().tail(max(1, int(burn_window_days)))
    # burn = total draw-down / the count of NON-top-up days. Idle 0-consumption days
    # (weekends) STAY in the denominator — averaging only over drop-days (the old
    # drops.mean()) excluded them, inflating burn and understating runway (~13d vs a
    # true ~26d). Only genuine renewal RISES are excluded from both numerator and
    # denominator (a top-up day's own consumption is unknown, so counting it as a
    # 0-burn day would understate a steady burn). Aligns with the canonical
    # trailing-complete-days burn in metric_registry.contract_runway.
    drops = -deltas[deltas < 0]
    non_topup_days = int((deltas <= 0).sum())
    burn = float(drops.sum()) / non_topup_days if non_topup_days else 0.0
    runway = (remaining / burn) if burn > 0 and remaining > 0 else None
    # Currency guard (recon audit 2026-08-17): REMAINING_BALANCE_DAILY is org rate-card
    # currency, which can be non-USD. Surface it so the UI labels balance/burn in the
    # real currency instead of a hardcoded '$' (matches org_accounts_spend).
    currency = "USD"
    if "CURRENCY" in getattr(df, "columns", ()) and df["CURRENCY"].notna().any():
        currency = str(df["CURRENCY"].dropna().iloc[0]).upper()
    return {
        "ok": True,
        "as_of": as_of,
        "currency": currency,
        "remaining_usd": remaining,
        "on_demand_usd": on_demand,
        "burn_per_day_usd": burn,
        "runway_days": round(runway, 0) if runway is not None else None,
        # E1: burn divides by non_topup_days, so THAT is the number a caption
        # may call "the average". burn_days_observed (drop-days only) is kept
        # because it answers a different, still-useful question — how many of
        # those days actually drew the balance down — but a UI that prints it
        # as the averaging basis contradicts the math above.
        "burn_basis_days": non_topup_days,
        "burn_days_observed": len(drops),
    }


def plan_scenarios(daily_burn_usd: float, term_months: int, buffer_pct: float,
                   remaining_usd: float, growth_pcts: tuple = (-10, 0, 10, 25),
                   today: date | None = None) -> list[dict]:
    """One row per growth scenario: term consumption, exhaustion of the
    CURRENT contract, and a recommended next commit with buffer.

    ``today`` anchors the exhaustion date and defaults to account time, not the
    server's: under SiS the process clock is UTC, so a bare ``date.today()``
    reads a day ahead from 18:00 Chicago onward and dates the contract running
    dry one day late.
    """
    daily = max(0.0, float(daily_burn_usd))
    months = max(1, min(int(term_months), 60))
    buffer = max(0.0, min(float(buffer_pct), 100.0)) / 100
    remaining = max(0.0, float(remaining_usd))
    anchor = today or account_today()
    rows = []
    for g in growth_pcts:
        rate = daily * (1 + g / 100)
        term_usd = rate * 30.44 * months
        if rate > 0 and remaining > 0:
            days_left = remaining / rate
            # r33: a near-idle account (tiny rate) x a large remaining balance yields a huge
            # days_left; anchor + timedelta(days=int(days_left)) then raises OverflowError
            # (beyond date.max). Past a ~10y horizon a runway is not meaningfully a date anyway.
            if days_left >= _HORIZON_DAYS:
                exhaustion_s = ">10y"
            else:
                exhaustion = anchor + timedelta(days=int(days_left))
                exhaustion_s = exhaustion.isoformat()
        else:
            exhaustion_s = "n/a"
        rows.append({
            "GROWTH": f"{g:+d}%",
            "DAILY_BURN_USD": round(rate, 2),
            "TERM_CONSUMPTION_USD": round(term_usd, 0),
            "CURRENT_CONTRACT_EXHAUSTED": exhaustion_s,
            "RECOMMENDED_COMMIT_USD": round(term_usd * (1 + buffer), 0),
        })
    return rows


def _runway_severity(days_left: float) -> str:
    if days_left < 0:
        return "warn"
    if days_left <= 30:
        return "bad"
    if days_left <= 90:
        return "warn"
    return "ok"


def best_runway(balance_df: pd.DataFrame | None, credits_runway: dict | None, *,
                today: date | None = None, lead_days: int = 30,
                max_lag_days: int = BALANCE_MAX_LAG_DAYS) -> dict | None:
    """cost-08: the exec runway from BILLING TRUTH when readable, else the credits model.

    balance_df = ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY rows (org_remaining_balance) or None;
    credits_runway = formulas.contract_runway(<contract_exhaustion row>) or None. Uses the
    balance basis only when remaining_balance_summary is ok, the as-of is <= max_lag_days old,
    and a burn was observed (or the balance is already exhausted). Days are currency-neutral
    (balance/burn in the same org currency), so a non-USD org still gets a runway. Days count
    from TODAY (runway minus the as-of lag). Falls back to credits_runway tagged basis='credits'
    with the reason; None when neither basis is available."""
    anchor = today or account_today()
    reason = "billing balance not readable"
    if balance_df is not None and len(balance_df):
        s = remaining_balance_summary(balance_df)
        if not s.get("ok"):
            reason = str(s.get("reason") or reason)
        else:
            ts = pd.to_datetime(s.get("as_of"), errors="coerce")
            if pd.isna(ts):
                reason = "balance as-of date unreadable"
            else:
                as_of = ts.date()
                lag = max(0, (anchor - as_of).days)
                remaining = float(s.get("remaining_usd") or 0.0)
                runway = s.get("runway_days")
                if lag > max_lag_days:
                    reason = f"billing balance stale (as of {as_of.isoformat()})"
                elif remaining <= 0 or runway is not None:
                    days_left = 0.0 if remaining <= 0 else max(0.0, float(runway or 0.0) - lag)
                    exhaust = (anchor + timedelta(days=int(days_left))
                               if days_left < _HORIZON_DAYS else None)
                    decide_by = exhaust - timedelta(days=max(0, int(lead_days))) if exhaust else None
                    alt = None
                    if credits_runway is not None and float(credits_runway.get("days_left", -1)) >= 0:
                        alt = float(credits_runway["days_left"])
                    gap = abs(days_left - alt) / alt * 100.0 if alt else None
                    return {
                        "pct_consumed": None, "days_left": days_left,
                        "exhaust_date": exhaust.isoformat() if exhaust else None,
                        "decide_by": decide_by.isoformat() if decide_by else None,
                        "severity": _runway_severity(days_left), "lead_days": int(lead_days),
                        "basis": "balance", "basis_label": "billing balance",
                        "as_of": as_of.isoformat(), "currency": s.get("currency", "USD"),
                        "remaining": remaining, "fallback_reason": None,
                        "alt_days_left": alt,
                        "gap_pct": round(gap, 1) if gap is not None else None,
                        "gap_disclose": gap is not None and gap > RUNWAY_GAP_DISCLOSE_PCT,
                    }
                else:
                    reason = "no balance burn observed"
    if credits_runway is None:
        return None
    return {**credits_runway, "basis": "credits", "basis_label": "configured credits",
            "as_of": None, "currency": None, "remaining": None, "fallback_reason": reason,
            "alt_days_left": None, "gap_pct": None, "gap_disclose": False}


def runway_basis_note(best: dict | None) -> str:
    """One plain sentence naming the runway basis (help text / caption). No '$' (Markdown math)."""
    if not best:
        return ""
    if best.get("basis") == "balance":
        note = (f"Runway from the Snowflake billing balance (as of {best.get('as_of')}, "
                f"{best.get('currency')}), which includes storage and transfer.")
        alt = best.get("alt_days_left")
        if alt is not None:
            note += f" The configured-credits model says {alt:,.0f} days"
            note += (f", {best.get('gap_pct'):.0f}% apart. Check CONTRACT_CREDITS in Settings."
                     if best.get("gap_disclose") else ".")
            note += " The COST_CONTRACT_BREACH paging alert stays on the configured-credits basis."
        return note
    note = ("Configured-rate credit runway from trailing 30 complete days. It excludes storage, "
            "transfer, and organization currency adjustments.")
    if best.get("fallback_reason"):
        note += f" Billing balance not used: {best['fallback_reason']}."
    return note
