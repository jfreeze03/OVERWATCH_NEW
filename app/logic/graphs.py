"""Task-graph cost math. Pure module.

Dollars come from measured warehouse credits (QUERY_ATTRIBUTION_HISTORY roll-
up per graph run) at the configured rate. $/run divides a day's pipeline cost
by that day's runs — labeled allocated, since attribution lag can shift a few
credits across midnight.
"""

from __future__ import annotations

import pandas as pd

from .formulas import credits_to_usd, safe_div, safe_float

TREND_FLAT_PCT = 10.0  # |Δ $/run| below this between halves = FLAT


def enrich_graph_daily(df: pd.DataFrame, rate_usd: float) -> pd.DataFrame:
    """Add USD, USD_PER_RUN and SUCCESS_PCT to the day x pipeline frame."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["DAY", "PIPELINE", "USD", "USD_PER_RUN", "SUCCESS_PCT"])
    out = df.copy()
    credits = pd.to_numeric(out["WH_CREDITS"], errors="coerce").fillna(0.0)
    runs = pd.to_numeric(out["GRAPH_RUNS"], errors="coerce").fillna(0.0)
    fails = pd.to_numeric(out["RUNS_WITH_FAILURES"], errors="coerce").fillna(0.0)
    out["USD"] = credits.map(lambda c: credits_to_usd(c, rate_usd))
    out["USD_PER_RUN"] = [
        round(safe_div(u, r), 4) for u, r in zip(out["USD"], runs, strict=True)]
    out["SUCCESS_PCT"] = [
        round(100.0 * safe_div(r - f, r, 1.0), 1) for r, f in zip(runs, fails, strict=True)]
    return out


def pipeline_summary(daily: pd.DataFrame) -> pd.DataFrame:
    """Roll the enriched day frame up to one row per pipeline with a trend.

    TREND compares mean $/run between the first and second half of the
    window's days for that pipeline: CHEAPER / PRICIER beyond ±10%, FLAT
    inside it, and 'n/a' with fewer than 4 active days (too thin to split).
    """
    cols = ["PIPELINE", "DATABASE_NAME", "SCHEMA_NAME", "GRAPH_RUNS", "TASK_RUNS",
            "USD", "USD_PER_RUN", "SUCCESS_PCT", "P95_WALL_SEC", "TREND"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for (pipe, db, schema), grp in daily.groupby(["PIPELINE", "DATABASE_NAME", "SCHEMA_NAME"]):
        grp = grp.sort_values("DAY")
        runs = float(pd.to_numeric(grp["GRAPH_RUNS"], errors="coerce").fillna(0).sum())
        fails = float(pd.to_numeric(grp["RUNS_WITH_FAILURES"], errors="coerce").fillna(0).sum())
        usd = float(pd.to_numeric(grp["USD"], errors="coerce").fillna(0).sum())
        per_run = grp["USD_PER_RUN"].astype(float)
        days_active = len(grp)
        if days_active >= 4:
            half = days_active // 2
            first = float(per_run.iloc[:half].mean())
            second = float(per_run.iloc[half:].mean())
            if first <= 0:
                trend = "n/a"
            else:
                delta_pct = (second - first) / first * 100.0
                trend = ("PRICIER" if delta_pct > TREND_FLAT_PCT
                         else "CHEAPER" if delta_pct < -TREND_FLAT_PCT else "FLAT")
        else:
            trend = "n/a"
        rows.append({
            "PIPELINE": pipe, "DATABASE_NAME": db, "SCHEMA_NAME": schema,
            "GRAPH_RUNS": int(runs),
            "TASK_RUNS": int(pd.to_numeric(grp["TASK_RUNS"], errors="coerce").fillna(0).sum()),
            "USD": round(usd, 2),
            "USD_PER_RUN": round(safe_div(usd, runs), 4),
            "SUCCESS_PCT": round(100.0 * safe_div(runs - fails, runs, 1.0), 1),
            "P95_WALL_SEC": round(safe_float(pd.to_numeric(grp["P95_WALL_SEC"], errors="coerce").max()), 1),
            "TREND": trend,
        })
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("USD", ascending=False).reset_index(drop=True)
