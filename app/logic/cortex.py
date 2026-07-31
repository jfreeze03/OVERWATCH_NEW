"""Cortex user-attribution math and severity classification.

Ported from the original OVERWATCH User Attribution thresholds:
- projected 30d credits > AI budget            -> Critical "Budget breach"
- projected 30d credits > 50% of AI budget     -> High     "Budget concentration"
- credits per request  > 0.10                  -> High     "Cost per request spike"
- projected 30d credits > 25% of AI budget     -> Medium   "High usage" (exception floor)

C7 additions:
- the ladder above is PER USER; the scope TOTAL gets its own Critical row
  (aggregate_budget_row) so ten users at 20% each cannot read as "nobody
  over 25%".
- the cost-per-request rule needs a real sample and real money behind it
  (CPR_MIN_REQUESTS / CPR_MIN_PROJECTED_USD), not one expensive request.
- every projection divides by effective_window_days, never by a window the
  scope has not existed for.

New-app contract: with no AI budget configured (0), budget severities are
skipped honestly — only the cost-per-request spike rule applies. Dollarization
uses the configured Cortex rate; nothing is baked into SQL.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .formulas import account_today, safe_float

CPR_SPIKE_THRESHOLD = 0.10  # credits per request
# C7: a spike is a TREND, not one expensive call. The old rule flagged High
# off a single request — 1 request x 0.11 credits is $0.24 and cannot be
# actioned. Both floors must clear: enough requests for the ratio to mean
# something, and enough projected money for the exception to be worth an
# operator's attention. Tune here, and the panel prints these numbers.
CPR_MIN_REQUESTS = 20
CPR_MIN_PROJECTED_USD = 10.0
# Budget ladder (fractions of the configured AI monthly budget).
BUDGET_LADDER = ((1.00, "Critical", "Budget breach"),
                 (0.50, "High", "Budget concentration"),
                 (0.25, "Medium", "High usage"))
_SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2}


def effective_window_days(rollup: pd.DataFrame, window_days: int) -> int:
    """The projection divisor: days this SCOPE has actually been observable.

    C7: dividing by the ASKED window is wrong whenever the data is younger
    than the question. A 365d window over a scope whose first Cortex request
    landed 10 days ago divides 10 days of credits by 365 — a 30d projection
    ~36x under the real burn, so the budget ladder never fires and the panel
    reports a breach as comfortable. Clamp the divisor to days-since-first-
    usage.

    Scope-level (the MIN across every row), not per-user, on purpose: a
    per-user denominator would divide a brand-new user's first afternoon by
    1 day and project them at 30x, re-creating the active-day inflation that
    review finding #11 removed.
    """
    asked = max(int(window_days or 30), 1)
    if rollup is None or rollup.empty or "FIRST_USAGE" not in rollup.columns:
        return asked
    try:
        first = pd.to_datetime(rollup["FIRST_USAGE"], errors="coerce", utc=True).min()
    except (TypeError, ValueError):      # exotic/mixed dtypes: keep the asked window
        return asked
    if pd.isna(first):
        return asked
    # +1: a first-usage timestamp of today means one observed day, not zero.
    elapsed = (account_today() - first.date()).days + 1
    return max(1, min(asked, elapsed))


# ---------------------------------------------------------------------------
# P9: deriving both live aggregates from ONE 365d fetch.
#
# cortex_sql.cortex_code_user_daily is the single live scan behind the AI-user
# panels. It is days-independent (one cache entry for every window), so the
# window is applied HERE and the two aggregates the panels need are folded in
# pandas instead of re-paying a 22s secure-view scan per window per panel.
# These two functions reproduce cortex_code_user_rollup / cortex_code_daily
# column-for-column — those builders remain the contract of record.
# ---------------------------------------------------------------------------

def _window_slice(user_daily: pd.DataFrame, days: int) -> pd.DataFrame:
    """Rows of the 365d fetch inside the asked window.

    Day-grain anchor (see app/data/common.py): "N days" is N complete days
    plus today — the SAME bound mart27_sql.ai_code_* apply, so the mart leg
    and the live leg of the same panel can never disagree by a day.
    """
    if user_daily is None or user_daily.empty or "USAGE_DATE" not in user_daily.columns:
        return pd.DataFrame()
    dates = pd.to_datetime(user_daily["USAGE_DATE"], errors="coerce")
    cutoff = pd.Timestamp(account_today() - timedelta(days=max(int(days or 1), 1)))
    return user_daily[dates >= cutoff]      # NaT compares False — unparseable rows drop


def rollup_from_user_daily(user_daily: pd.DataFrame, days: int) -> pd.DataFrame:
    """cortex_sql.cortex_code_user_rollup's output, folded in pandas."""
    win = _window_slice(user_daily, days)
    if win.empty:
        return pd.DataFrame()
    keys = [c for c in ("USER_NAME", "EMAIL", "FIRST_NAME", "LAST_NAME", "SOURCE")
            if c in win.columns]
    # dropna=False: a service account with no EMAIL/name is still a spender and
    # must not vanish from chargeback because a dimension column is NULL.
    out = (win.groupby(keys, dropna=False)
              .agg(ACTIVE_DAYS=("USAGE_DATE", "nunique"),
                   TOTAL_REQUESTS=("REQUESTS", "sum"),
                   TOTAL_CREDITS=("CREDITS", "sum"),
                   TOTAL_TOKENS=("TOKENS", "sum"),
                   FIRST_USAGE=("FIRST_TS", "min"),
                   LAST_USAGE=("LAST_TS", "max"))
              .reset_index())
    # .where(>0) reproduces SQL NULLIF: no requests / no active days is NULL,
    # not a ZeroDivisionError and not a misleading 0.0.
    out["CREDITS_PER_REQUEST"] = out["TOTAL_CREDITS"] / out["TOTAL_REQUESTS"].where(
        out["TOTAL_REQUESTS"] > 0)
    out["AVG_DAILY_CREDITS"] = out["TOTAL_CREDITS"] / out["ACTIVE_DAYS"].where(
        out["ACTIVE_DAYS"] > 0)
    return (out.sort_values("TOTAL_CREDITS", ascending=False)
               .head(500)                    # same LIMIT 500 as the SQL builder
               .reset_index(drop=True))


def daily_from_user_daily(user_daily: pd.DataFrame, days: int) -> pd.DataFrame:
    """cortex_sql.cortex_code_daily's output, folded in pandas."""
    win = _window_slice(user_daily, days)
    if win.empty:
        return pd.DataFrame()
    out = (win.groupby(["USAGE_DATE", "SOURCE"], dropna=False)
              .agg(ACTIVE_USERS=("USER_NAME", "nunique"),
                   TOTAL_REQUESTS=("REQUESTS", "sum"),
                   TOTAL_CREDITS=("CREDITS", "sum"),
                   TOTAL_TOKENS=("TOKENS", "sum"))
              .reset_index()
              .rename(columns={"USAGE_DATE": "DAY"}))
    return out.sort_values(["DAY", "SOURCE"]).reset_index(drop=True)


def enrich_user_rollup(df: pd.DataFrame, ai_rate_usd: float,
                       window_days: int = 30) -> pd.DataFrame:
    """Add projected-30d credits/cost columns to the SQL rollup.

    Projection basis is the CALENDAR window (TOTAL_CREDITS / window * 30) —
    the same basis rollup_summary uses. The old basis (active-day average
    x30) projected a user active 2 of 30 days at 15x their real monthly
    burn, and the two surfaces on the same page disagreed (review finding
    #11). AVG_DAILY_CREDITS stays as the intensity-on-active-days metric.

    The window is clamped by effective_window_days so a young scope is not
    projected against a window it never lived through.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("TOTAL_CREDITS", "AVG_DAILY_CREDITS", "CREDITS_PER_REQUEST", "TOTAL_REQUESTS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    window = effective_window_days(out, window_days)
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
            for frac, severity, signal in BUDGET_LADDER:
                if proj > budget_credits * frac:
                    sig = (severity, signal)
                    break
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
        # C7: the ratio alone is not evidence. One request at 0.11 cr/request
        # used to book a High; require a sample big enough for the ratio to be
        # a trend AND enough projected money to be worth chasing.
        if (safe_float(row.get("CREDITS_PER_REQUEST")) > CPR_SPIKE_THRESHOLD
                and safe_float(row.get("TOTAL_REQUESTS")) >= CPR_MIN_REQUESTS
                and safe_float(row.get("PROJECTED_30D_USD")) >= CPR_MIN_PROJECTED_USD):
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
    """Window totals + 30d projection for the KPI row.

    Uses the SAME clamped divisor as enrich_user_rollup — if the KPI and the
    per-user rows disagreed about the projection basis we would be back to
    review finding #11, only in the opposite direction."""
    if enriched is None or enriched.empty:
        return {"active_users": 0, "total_requests": 0, "total_credits": 0.0,
                "spend_usd": 0.0, "projected_30d_usd": 0.0, "window_days": 1}
    days = effective_window_days(enriched, window_days)
    total_credits = float(enriched["TOTAL_CREDITS"].sum())
    spend = float(enriched["SPEND_USD"].sum())
    return {
        "active_users": int(enriched["USER_NAME"].nunique()),
        "total_requests": int(enriched["TOTAL_REQUESTS"].sum()),
        "total_credits": round(total_credits, 4),
        "spend_usd": round(spend, 2),
        "projected_30d_usd": round(spend / days * 30.0, 2),
        "window_days": days,
    }


def aggregate_budget_row(summary: dict, ai_budget_usd: float) -> dict | None:
    """The ONE scope-wide budget exception, or None.

    C7: every budget severity in classify_exceptions is PER USER, so ten
    users at 20% of budget each — 200% of the budget, a live breach — left
    the panel printing "No users over 25% of the AI budget". The scope total
    is a first-class signal and gets its own Critical row; it is not
    double-counting the per-user rows, it is the number the budget is
    actually written against.
    """
    budget = safe_float(ai_budget_usd)
    projected = safe_float((summary or {}).get("projected_30d_usd"))
    if budget <= 0 or projected <= budget:
        return None
    requests = safe_float((summary or {}).get("total_requests"))
    credits = safe_float((summary or {}).get("total_credits"))
    return {
        "SEVERITY": "Critical",
        "SIGNAL": "AI budget breach (all users)",
        # Placeholders in the identity columns, so the row reads as the scope
        # total in the table AND in the Action Queue title it generates.
        "USER_NAME": "(all users)",
        "SOURCE": "(all sources)",
        "TOTAL_REQUESTS": requests,
        "TOTAL_CREDITS": credits,
        "CREDITS_PER_REQUEST": (credits / requests) if requests > 0 else 0.0,
        "PROJECTED_30D_USD": projected,
    }


def with_aggregate_budget_row(exceptions: pd.DataFrame, summary: dict,
                              ai_budget_usd: float) -> pd.DataFrame:
    """classify_exceptions output with the scope-wide row prepended (if any).

    Prepended rather than re-sorted: the aggregate breach is the headline,
    and a per-user Critical below it is the detail that explains it.
    """
    agg = aggregate_budget_row(summary, ai_budget_usd)
    if agg is None:
        return exceptions if exceptions is not None else pd.DataFrame()
    head = pd.DataFrame([agg])
    if exceptions is None or exceptions.empty:
        return head
    return pd.concat([head, exceptions], ignore_index=True)
