"""Cortex user-attribution math and severity classification.

Ported from the original OVERWATCH User Attribution thresholds:
- projected 30d credits > AI budget            -> Critical "Budget breach"
- projected 30d credits > 50% of AI budget     -> High     "Budget concentration"
- credits per request  > 0.10                  -> High     "Cost per request spike"
- projected 30d credits > 25% of AI budget     -> Medium   "High usage" (exception floor)

New-app contract: with no AI budget configured (0), budget severities are
skipped honestly — only the cost-per-request spike rule applies. Dollarization
uses the configured Cortex rate; nothing is baked into SQL.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_float

CPR_SPIKE_THRESHOLD = 0.10  # credits per request
_SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2}


def enrich_user_rollup(df: pd.DataFrame, ai_rate_usd: float,
                       window_days: int = 30) -> pd.DataFrame:
    """Add projected-30d credits/cost columns to the SQL rollup.

    Projection basis is the CALENDAR window (TOTAL_CREDITS / window * 30) —
    the same basis rollup_summary uses. The old basis (active-day average
    x30) projected a user active 2 of 30 days at 15x their real monthly
    burn, and the two surfaces on the same page disagreed (review finding
    #11). AVG_DAILY_CREDITS stays as the intensity-on-active-days metric.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("TOTAL_CREDITS", "AVG_DAILY_CREDITS", "CREDITS_PER_REQUEST", "TOTAL_REQUESTS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    window = max(int(window_days or 30), 1)
    out["PROJECTED_30D_CREDITS"] = out.get("TOTAL_CREDITS", 0.0) / window * 30.0
    rate = safe_float(ai_rate_usd, 2.20)
    out["SPEND_USD"] = (out.get("TOTAL_CREDITS", 0.0) * rate).round(2)
    out["PROJECTED_30D_USD"] = (out["PROJECTED_30D_CREDITS"] * rate).round(2)
    # Owner ask (v4.50): charts read better as people than as login names.
    # "First Last" when USERS carries them; the login name when it doesn't
    # (service accounts, dropped users) — never blank, never invented.
    if "FIRST_NAME" in out.columns and "LAST_NAME" in out.columns:
        full = (out["FIRST_NAME"].fillna("").astype(str).str.strip() + " "
                + out["LAST_NAME"].fillna("").astype(str).str.strip()).str.strip()
        out["DISPLAY_NAME"] = full.where(full != "", out["USER_NAME"])
    else:
        out["DISPLAY_NAME"] = out["USER_NAME"]
    return out


def classify_exceptions(enriched: pd.DataFrame, ai_budget_usd: float, ai_rate_usd: float) -> pd.DataFrame:
    """Return exception rows with SEVERITY and SIGNAL, strongest first."""
    if enriched is None or enriched.empty:
        return pd.DataFrame()
    rate = max(safe_float(ai_rate_usd, 2.20), 0.01)
    budget_credits = safe_float(ai_budget_usd) / rate  # 0 when unconfigured

    src = enriched.copy()
    for col in ("PROJECTED_30D_CREDITS", "PROJECTED_30D_USD", "TOTAL_REQUESTS",
                "TOTAL_CREDITS", "CREDITS_PER_REQUEST"):
        if col in src.columns:
            src[col] = src[col].map(safe_float)
    rows: list[dict] = []
    # --- Budget signals are PER-USER: a user's TOTAL 30d projection across every
    # source (and every identity) vs the budget, with AGGREGATE dollars/requests
    # so the reported ESTIMATED_USD is the real breach (audit #17: the per-source
    # version both missed split breaches and under-reported the ones it caught).
    # Sum ALL of a user's rows — a login NAME reused across USER_IDs (recreated
    # user) yields distinct rows that are still that person's spend; dropping one
    # (an interim fix did) would under-count and silently miss a breach.
    if budget_credits > 0 and "USER_NAME" in src.columns:
        sum_cols = [c for c in ("PROJECTED_30D_CREDITS", "PROJECTED_30D_USD",
                                "TOTAL_REQUESTS", "TOTAL_CREDITS") if c in src.columns]
        agg = src.groupby("USER_NAME", as_index=False)[sum_cols].sum()
        if {"TOTAL_CREDITS", "TOTAL_REQUESTS"}.issubset(agg.columns):
            agg["CREDITS_PER_REQUEST"] = (agg["TOTAL_CREDITS"]
                / agg["TOTAL_REQUESTS"].where(agg["TOTAL_REQUESTS"] > 0, 1))
        for _, u in agg.iterrows():
            proj = safe_float(u.get("PROJECTED_30D_CREDITS"))
            sig: tuple[str, str] | None = None
            if proj > budget_credits:
                sig = ("Critical", "Budget breach")
            elif proj > budget_credits * 0.50:
                sig = ("High", "Budget concentration")
            elif proj > budget_credits * 0.25:
                sig = ("Medium", "High usage")
            if sig:
                item = u.to_dict()
                item["SOURCE"] = "(all sources)"
                item["SEVERITY"], item["SIGNAL"] = sig
                rows.append(item)
    # --- CPR spikes are PER-SOURCE and independent of budget, so a spiking source
    # still surfaces even for a user who is also a budget breach. De-dup on
    # (user, source) keeping the worst variant so a source is never listed twice.
    cpr_src = src
    if {"USER_NAME", "SOURCE"}.issubset(src.columns):
        cpr_src = (src.sort_values("CREDITS_PER_REQUEST", ascending=False)
                      .drop_duplicates(subset=["USER_NAME", "SOURCE"], keep="first"))
    for _, row in cpr_src.iterrows():
        if safe_float(row.get("CREDITS_PER_REQUEST")) > CPR_SPIKE_THRESHOLD:
            item = row.to_dict()
            item["SEVERITY"], item["SIGNAL"] = "High", "Cost per request spike"
            rows.append(item)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    if "PROJECTED_30D_USD" not in out.columns:
        out["PROJECTED_30D_USD"] = 0.0
    out["_S"] = out["SEVERITY"].map(_SEVERITY_ORDER).fillna(9)
    out = out.sort_values(["_S", "PROJECTED_30D_USD"], ascending=[True, False]).drop(columns="_S")
    return out.reset_index(drop=True)


def rollup_summary(enriched: pd.DataFrame, window_days: int) -> dict:
    """Window totals + 30d projection for the KPI row."""
    if enriched is None or enriched.empty:
        return {"active_users": 0, "total_requests": 0, "total_credits": 0.0,
                "spend_usd": 0.0, "projected_30d_usd": 0.0}
    days = max(int(window_days or 1), 1)
    total_credits = float(enriched["TOTAL_CREDITS"].sum())
    spend = float(enriched["SPEND_USD"].sum())
    return {
        "active_users": int(enriched["USER_NAME"].nunique()),
        "total_requests": int(enriched["TOTAL_REQUESTS"].sum()),
        "total_credits": round(total_credits, 4),
        "spend_usd": round(spend, 2),
        "projected_30d_usd": round(spend / days * 30.0, 2),
    }
