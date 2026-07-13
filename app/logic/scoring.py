"""Evidence-based platform operating score.

The score is computed from observed signals with named penalty drivers, so an
executive can always ask "why 74?" and get the exact deductions. It never
reads a self-reported score from a mart as truth (old-app principle, kept).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .formulas import safe_float


@dataclass(frozen=True)
class ScoreDriver:
    driver: str
    penalty: float
    evidence: str


@dataclass(frozen=True)
class PlatformScore:
    score: int
    state: str
    drivers: tuple[ScoreDriver, ...] = field(default_factory=tuple)


def _cap(value: float, cap: float) -> float:
    return min(max(value, 0.0), cap)


# Per-unit penalty weights. UNCALIBRATED STARTING POINTS — tune them against
# your incident history via SETTINGS (SCORE_PTS_*); caps stay fixed so no
# single driver can dominate the score.
DEFAULT_WEIGHTS = {
    "SCORE_PTS_BUDGET_PER_PCT": 0.5,
    "SCORE_PTS_PER_CRITICAL": 6.0,
    "SCORE_PTS_PER_HIGH": 2.0,
    "SCORE_PTS_QUERY_FAIL_PER_PCT": 1.5,
    "SCORE_PTS_QUEUE_PER_MIN": 0.3,
    "SCORE_PTS_SPILL_PER_GB": 0.5,
    "SCORE_PTS_PER_STALE_SOURCE": 4.0,
    "SCORE_PTS_PER_OPEN_ACTION": 1.5,
}


def resolve_weights(settings: dict | None) -> dict:
    """Merge SETTINGS overrides onto the defaults (bad values fall back)."""
    weights = dict(DEFAULT_WEIGHTS)
    for key, default in DEFAULT_WEIGHTS.items():
        raw = (settings or {}).get(key)
        value = safe_float(raw, -1.0)
        if value >= 0:
            weights[key] = value
        else:
            weights[key] = default
    return weights


def platform_score(signals: dict, weights: dict | None = None) -> PlatformScore:
    """Score 0-100 from a signals dict. Missing signals simply add no penalty.

    Expected keys (all optional):
      budget_pct, critical_alerts, high_alerts, query_fail_pct,
      queue_minutes, spill_gb, stale_sources, open_high_actions
    Weights come from resolve_weights(settings) so executives can ask "why is
    a critical worth N points?" and get "because we set it" — not magic.
    """
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    drivers: list[ScoreDriver] = []

    budget_pct = safe_float(signals.get("budget_pct"))
    if budget_pct > 100:
        penalty = _cap((budget_pct - 100) * w["SCORE_PTS_BUDGET_PER_PCT"], 20)
        drivers.append(ScoreDriver("Over budget", penalty, f"Spend at {budget_pct:.0f}% of monthly budget."))

    critical = safe_float(signals.get("critical_alerts"))
    if critical > 0:
        drivers.append(ScoreDriver("Critical alerts", _cap(critical * w["SCORE_PTS_PER_CRITICAL"], 24), f"{critical:.0f} open critical alerts."))

    high = safe_float(signals.get("high_alerts"))
    if high > 0:
        drivers.append(ScoreDriver("High alerts", _cap(high * w["SCORE_PTS_PER_HIGH"], 10), f"{high:.0f} open high alerts."))

    query_fail = safe_float(signals.get("query_fail_pct"))
    if query_fail > 2:
        drivers.append(
            ScoreDriver("Query failures", _cap((query_fail - 2) * w["SCORE_PTS_QUERY_FAIL_PER_PCT"], 12), f"{query_fail:.1f}% of queries failed.")
        )

    queue_minutes = safe_float(signals.get("queue_minutes"))
    if queue_minutes > 10:
        drivers.append(
            ScoreDriver("Queueing", _cap((queue_minutes - 10) * w["SCORE_PTS_QUEUE_PER_MIN"], 10), f"{queue_minutes:.0f} queued minutes in window.")
        )

    spill_gb = safe_float(signals.get("spill_gb"))
    if spill_gb > 5:
        drivers.append(
            ScoreDriver("Remote spill", _cap((spill_gb - 5) * w["SCORE_PTS_SPILL_PER_GB"], 8), f"{spill_gb:.1f} GB spilled to remote storage.")
        )

    stale = safe_float(signals.get("stale_sources"))
    if stale > 0:
        drivers.append(ScoreDriver("Stale telemetry", _cap(stale * w["SCORE_PTS_PER_STALE_SOURCE"], 12), f"{stale:.0f} fact sources stale."))

    open_high_actions = safe_float(signals.get("open_high_actions"))
    if open_high_actions > 0:
        drivers.append(
            ScoreDriver("Owner queue", _cap(open_high_actions * w["SCORE_PTS_PER_OPEN_ACTION"], 9), f"{open_high_actions:.0f} open high-severity actions.")
        )

    total_penalty = sum(d.penalty for d in drivers)
    score = round(max(0.0, 100.0 - total_penalty))
    state = "Healthy" if score >= 85 else "Watch" if score >= 70 else "Degraded" if score >= 50 else "At risk"
    ranked = tuple(sorted(drivers, key=lambda d: d.penalty, reverse=True))
    return PlatformScore(score=score, state=state, drivers=ranked)


def score_history(inputs: pd.DataFrame, weights: dict | None = None,
                  monthly_budget_usd: float = 0.0, rate_usd: float = 3.68) -> pd.DataFrame:
    """Retro platform score per day from fact-derived signals.

    ``inputs`` (one row per DAY): CREDITS_BILLED, CRIT_RAISED, HIGH_RAISED,
    QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB.
    Budget pct uses the month-to-date cumulative spend against the monthly
    budget, like the live score. Labeled RETRO: the live score also counts
    stale sources and open actions, which facts don't carry per-day — the
    trend is comparable, the absolute value can differ by a few points.
    """
    if inputs is None or inputs.empty or "DAY" not in inputs.columns:
        return pd.DataFrame()
    frame = inputs.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce")
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")
    for col in ("CREDITS_BILLED", "CRIT_RAISED", "HIGH_RAISED", "QUERY_COUNT",
                "FAILED_COUNT", "QUEUED_SEC", "SPILL_GB"):
        frame[col] = frame.get(col, 0).map(safe_float) if col in frame.columns else 0.0
    frame["_MONTH"] = frame["DAY"].dt.to_period("M")
    frame["_MTD_USD"] = frame.groupby("_MONTH")["CREDITS_BILLED"].cumsum() * safe_float(rate_usd, 3.68)
    budget = safe_float(monthly_budget_usd)
    rows = []
    for _, r in frame.iterrows():
        queries = r["QUERY_COUNT"]
        result = platform_score(signals={
            "budget_pct": (r["_MTD_USD"] / budget * 100) if budget > 0 else 0,
            "critical_alerts": r["CRIT_RAISED"],
            "high_alerts": r["HIGH_RAISED"],
            "query_fail_pct": (r["FAILED_COUNT"] / queries * 100) if queries else 0,
            "queue_minutes": r["QUEUED_SEC"] / 60.0,
            "spill_gb": r["SPILL_GB"],
        }, weights=weights)
        rows.append({"DAY": r["DAY"].date(), "SCORE": result.score, "STATE": result.state})
    return pd.DataFrame(rows)
