"""Cortex user-attribution math and severity classification.

Ported from the original OVERWATCH User Attribution thresholds:
- projected 30d credits > AI budget            -> Critical "Budget breach"
- projected 30d credits > 50% of AI budget     -> High     "Budget concentration"
- credits/request a positive cohort outlier    -> High     "Cost per request spike"
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

from .anomaly import robust_zscores
from .formulas import account_today, safe_float

# A spike is UNUSUAL FOR THIS ACCOUNT, not just an expensive model. Score each
# (user, source)'s credits-per-request against a robust median/MAD baseline over
# the eligible cohort (the SAME estimator as the warehouse anomaly scorer) and
# flag only positive outliers at/above this modified-z. The old flat > 0.10
# cr/request permanently flagged every heavy-but-normal user of a pricier Cortex
# model — a flat cut is a model-price floor, not a spike.
CPR_SPIKE_Z = 3.5
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


# rec #38: minimum per-user observable days before the budget ladder will project
# a monthly breach — below this the projection is too noisy to trust (a first
# afternoon must not be extrapolated into a false breach).
_MIN_OBSERVABLE_DAYS = 4


def enrich_user_rollup(df: pd.DataFrame, ai_rate_usd: float,
                       window_days: int = 30) -> pd.DataFrame:
    """Add projected-30d credits/cost columns to the SQL rollup.

    Projection basis (rec #38) is each user's OWN observable window —
    ``TOTAL_CREDITS / OBSERVABLE_DAYS * 30`` where OBSERVABLE_DAYS is the days
    since that user's first request, clamped to ``[1, window_days]``. The prior
    basis divided by the SCOPE window (the oldest user's history), which
    under-projected a heavy user new to a mature scope (2 days of credits / 30 ~=
    15x too low) so the budget ladder missed the breach. A small-N guard in
    ``classify_exceptions`` keeps a brand-new user's first afternoon from
    inflating into a false breach. AVG_DAILY_CREDITS stays the intensity-on-
    active-days metric; do not confuse it with this monthly projection.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("TOTAL_CREDITS", "AVG_DAILY_CREDITS", "CREDITS_PER_REQUEST", "TOTAL_REQUESTS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    window = effective_window_days(out, window_days)
    # rec #38: project each user against their OWN observable window (days since
    # THAT user's first Cortex request), not the scope minimum. A heavy user new to
    # a mature 30-day scope was divided by the full scope window (2 days of credits
    # / 30 ~= 15x too low), so a real new breacher never tripped the budget ladder.
    # OBSERVABLE_DAYS also carries the small-N guard classify_exceptions applies, so
    # a brand-new user's first afternoon is not inflated into a false breach.
    cap = max(int(window_days or 30), 1)
    if "FIRST_USAGE" in out.columns:
        _first = pd.to_datetime(out["FIRST_USAGE"], errors="coerce", utc=True)
        _now = pd.Timestamp(account_today()).tz_localize("UTC")
        # .normalize() to date-grain FIRST: a first-usage timestamp carries a time-of-day, and
        # a bare (_now - _first).dt.days would FLOOR the intraday fraction, landing one day short
        # for every real (non-midnight) request — so this must match effective_window_days'
        # date-based inclusive '(today - first.date()).days + 1', or the per-user projection and
        # the _MIN_OBSERVABLE_DAYS breach guard drift a day off the scope divisor.
        _elapsed = (_now - _first.dt.normalize()).dt.days + 1
        out["OBSERVABLE_DAYS"] = _elapsed.clip(lower=1, upper=cap).fillna(float(window))
    else:
        out["OBSERVABLE_DAYS"] = float(window)
    out["PROJECTED_30D_CREDITS"] = out.get("TOTAL_CREDITS", 0.0) / out["OBSERVABLE_DAYS"] * 30.0
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
        # rec #38: a user's observable window = the MAX across their rows (earliest
        # first-usage = most days seen = the most reliable projection basis).
        if "OBSERVABLE_DAYS" in src.columns:
            _obs = src.groupby("USER_NAME")["OBSERVABLE_DAYS"].max()
            agg["OBSERVABLE_DAYS"] = agg["USER_NAME"].map(_obs)
            # rec #38: re-project the user's SUMMED credits over their most-reliable (MAX)
            # observable window, rather than summing per-source projections that each divide
            # by their OWN (possibly 1-day) window — else a brand-new second source's first-day
            # burst is extrapolated 30x into the total and trips a false Critical breach. Mirrors
            # enrich_user_rollup's projection over the same window the small-N guard trusts.
            if "TOTAL_CREDITS" in agg.columns:
                _obs_days = agg["OBSERVABLE_DAYS"].where(agg["OBSERVABLE_DAYS"] > 0, 1.0)
                agg["PROJECTED_30D_CREDITS"] = agg["TOTAL_CREDITS"] / _obs_days * 30.0
                agg["PROJECTED_30D_USD"] = (agg["PROJECTED_30D_CREDITS"] * rate).round(2)
        for _, u in agg.iterrows():
            # rec #38: skip users with too few observed days — projecting a brand-
            # new user's first day/two at full intensity would be a false breach.
            # Absent OBSERVABLE_DAYS (old shape) defaults inert, so nothing is lost.
            if safe_float(u.get("OBSERVABLE_DAYS", _MIN_OBSERVABLE_DAYS)) < _MIN_OBSERVABLE_DAYS:
                continue
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
    # --- CPR spikes are PER-SOURCE, RELATIVE, and independent of budget, so a
    # spiking source still surfaces even for a user who is also a budget breach.
    # De-dup on (user, source) keeping the worst variant so a source is never
    # listed twice.
    cpr_src = src.copy()
    if {"USER_NAME", "SOURCE"}.issubset(cpr_src.columns):
        cpr_src = (cpr_src.sort_values("CREDITS_PER_REQUEST", ascending=False)
                          .drop_duplicates(subset=["USER_NAME", "SOURCE"], keep="first"))
    # Baseline over rows with a MEANINGFUL sample only, so a 1-request ratio can't
    # define the cohort. robust_zscores needs >=5 points and returns 0 when
    # dispersion collapses (a uniformly-priced cohort => no spikes) — exactly the
    # heavy-but-normal case the flat threshold got wrong. With <5 eligible peers
    # there is no cohort to be an outlier of, so nothing is flagged (honest).
    eligible = (cpr_src.get("TOTAL_REQUESTS", pd.Series(dtype=float)).map(safe_float)
                >= CPR_MIN_REQUESTS)
    cpr_src["_CPR_Z"] = 0.0
    if int(eligible.sum()) >= 5:
        cpr_src.loc[eligible, "_CPR_Z"] = robust_zscores(
            cpr_src.loc[eligible, "CREDITS_PER_REQUEST"])
    for _, row in cpr_src.iterrows():
        # A spike is a POSITIVE cohort outlier that also clears the materiality
        # floors: enough requests for the ratio to be a trend AND enough projected
        # money to be worth chasing (one expensive call is not a trend).
        if (safe_float(row.get("_CPR_Z")) >= CPR_SPIKE_Z
                and safe_float(row.get("TOTAL_REQUESTS")) >= CPR_MIN_REQUESTS
                and safe_float(row.get("PROJECTED_30D_USD")) >= CPR_MIN_PROJECTED_USD):
            item = {k: v for k, v in row.to_dict().items() if k != "_CPR_Z"}
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

    The 30d projection is the SUM of the per-user PROJECTED_30D_USD column (rec #38:
    each user projected over their OWN observable window), so the headline KPI equals the
    total of the 'Proj. 30d $' column in the detail table it sits above. Dividing scope
    spend by the scope-wide window instead systematically under-projects a new heavy user
    in a mature scope (their big spend spread over the oldest user's long tenure)."""
    if enriched is None or enriched.empty:
        return {"active_users": 0, "total_requests": 0, "total_credits": 0.0,
                "spend_usd": 0.0, "projected_30d_usd": 0.0, "window_days": 1}
    days = effective_window_days(enriched, window_days)
    total_credits = float(enriched["TOTAL_CREDITS"].sum())
    spend = float(enriched["SPEND_USD"].sum())
    projected = (float(enriched["PROJECTED_30D_USD"].sum())
                 if "PROJECTED_30D_USD" in enriched.columns else spend / days * 30.0)
    # The headline KPI (projected_30d_usd) is the full column sum so it reconciles with the
    # detail table. The BUDGET aggregate (aggregate_budget_row) instead reads the GUARDED total
    # below, built on the SAME per-USER basis classify_exceptions uses: group by user, SUM credits,
    # re-project over the user's MAX observable window, and drop only whole users below the small-N
    # floor. A per-ROW drop would instead discard a young secondary source's credits entirely and
    # let a distributed breach fall below budget (surfacing in neither the aggregate nor per-user
    # rows); re-projecting the user's summed credits keeps that spend in the guarded total while
    # still refusing to extrapolate a brand-new user's first-afternoon burst 30x.
    projected_guarded = projected
    if {"USER_NAME", "TOTAL_CREDITS", "OBSERVABLE_DAYS"}.issubset(enriched.columns):
        _rate = (spend / total_credits) if total_credits > 0 else 0.0
        _pu = pd.DataFrame({
            "USER_NAME": enriched["USER_NAME"],
            "_CR": pd.to_numeric(enriched["TOTAL_CREDITS"], errors="coerce").fillna(0.0),
            "_OBS": pd.to_numeric(enriched["OBSERVABLE_DAYS"], errors="coerce").fillna(0.0),
        }).groupby("USER_NAME").agg(_CR=("_CR", "sum"), _OBS=("_OBS", "max"))
        _pu = _pu[_pu["_OBS"] >= _MIN_OBSERVABLE_DAYS]
        _proj_cr = _pu["_CR"] / _pu["_OBS"].where(_pu["_OBS"] > 0, 1.0) * 30.0
        projected_guarded = float((_proj_cr * _rate).sum())
    return {
        "active_users": int(enriched["USER_NAME"].nunique()),
        "total_requests": int(enriched["TOTAL_REQUESTS"].sum()),
        "total_credits": round(total_credits, 4),
        "spend_usd": round(spend, 2),
        "projected_30d_usd": round(projected, 2),
        "projected_30d_usd_guarded": round(projected_guarded, 2),
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
    # Fire on the small-N-GUARDED projection (falls back to the full projection when the guard
    # column is absent, e.g. an old-shape summary), so the scope breach uses the SAME reliable
    # basis as the per-user ladder — a brand-new user's un-guarded 30x burst must not manufacture
    # a Critical the Exceptions table then can't explain with any per-user row.
    _s = summary or {}
    projected = safe_float(_s.get("projected_30d_usd_guarded", _s.get("projected_30d_usd")))
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
