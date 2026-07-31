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


def _cap_note(raw: float, cap: float) -> str:
    """E3: ' (capped)' when the per-driver cap actually bit.

    The caps are deliberate — no single driver may dominate the score — but they
    were INVISIBLE. '12 fact sources stale' at 4 pts each reads as a 48-point hit
    while the driver is pinned at 12, so the deductions expander did not add up
    and nothing told the reader that fixing 6 of the 12 would not move the score
    at all. Naming the saturation makes both facts legible.
    """
    return " (capped)" if raw > cap else ""


# Per-unit penalty weights. UNCALIBRATED STARTING POINTS — tune them against
# your incident history via SETTINGS (SCORE_PTS_*); caps stay fixed so no
# single driver can dominate the score.
DEFAULT_WEIGHTS = {
    "SCORE_PTS_BUDGET_PER_PCT": 0.5,
    "SCORE_PTS_PER_CRITICAL": 6.0,
    "SCORE_PTS_PER_HIGH": 2.0,
    "SCORE_PTS_QUERY_FAIL_PER_PCT": 1.5,
    "SCORE_PTS_TASK_FAIL_PER_PCT": 2.0,
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


#: Sources whose failure silently zeros health penalties — the score is only
#: trustworthy when these loaded. Passed via ``available`` from the caller.
#: C2/N5: throughput = the fixed-window FACT_QUERY_HOURLY read that feeds
#: query/task/queue/spill (replaced the exec board, which was windowed to the
#: user's spend scope and so silently redefined the score).
REQUIRED_SIGNAL_SOURCES = frozenset({"throughput", "alerts"})


def degraded_sources(results: dict) -> set[str]:
    """A-score-2: the penalty-bearing sources whose read is a genuine OUTAGE — i.e.
    ``ok is False`` AND ``error_kind != 'absent'``. An 'absent' read means the mart
    is simply not installed (a legitimate zero on a partial deployment), NOT a
    suppressed signal; those stay zero-penalty. A timeout / unknown_function / other
    failure, by contrast, could be hiding a real task-failure / staleness / owner-queue
    / over-budget condition, so it must fail the score closed (extends C1's principle
    beyond the two REQUIRED sources). ``results`` maps source-name -> QueryResult-like
    (anything with ``.ok`` and ``.error_kind``)."""
    return {name for name, r in results.items()
            if (not getattr(r, "ok", True)) and getattr(r, "error_kind", "") != "absent"}


def platform_score(signals: dict, weights: dict | None = None,
                   available: set[str] | None = None,
                   degraded: set[str] | None = None) -> PlatformScore:
    """Score 0-100 from a signals dict. Missing signals simply add no penalty.

    Expected keys (all optional):
      budget_pct, critical_alerts, high_alerts, query_fail_pct, task_fail_pct,
      queue_minutes, spill_gb, stale_sources, open_high_actions
    queue_minutes and spill_gb are PER-DAY rates (C8) — pass a window total and
    the penalty scales with the window length instead of with the platform.
    Weights come from resolve_weights(settings) so executives can ask "why is
    a critical worth N points?" and get "because we set it" — not magic.

    C1: a *failed* read (vs a legitimate zero) removes that source's penalty, so
    an outage that suppresses real failures/alerts would IMPROVE the score — the
    cardinal monitoring sin. Two gates report ``Incomplete`` instead of a false green:
    ``available`` (the REQUIRED source keys that loaded — throughput+alerts) must be
    complete, AND ``degraded`` (A-score-2: penalty-bearing sources that hit a genuine
    outage, via ``degraded_sources``) must be empty. An 'absent' mart is a legitimate
    zero on a partial deployment and is NOT degraded.
    """
    _missing = (available is not None and not REQUIRED_SIGNAL_SOURCES.issubset(available))
    if _missing or degraded:
        parts = []
        if _missing:
            parts.append(", ".join(sorted(REQUIRED_SIGNAL_SOURCES - set(available or ()))))
        if degraded:
            parts.append(", ".join(sorted(degraded)))
        return PlatformScore(score=0, state="Incomplete",
                             drivers=(ScoreDriver("Inputs unavailable", 0.0,
                                                  f"Health signals did not load: {'; '.join(parts)}."),))
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    drivers: list[ScoreDriver] = []

    budget_pct = safe_float(signals.get("budget_pct"))
    if budget_pct > 100:
        _raw = (budget_pct - 100) * w["SCORE_PTS_BUDGET_PER_PCT"]
        drivers.append(ScoreDriver("Over budget", _cap(_raw, 20),
                                   f"Spend at {budget_pct:.0f}% of monthly budget." + _cap_note(_raw, 20)))

    critical = safe_float(signals.get("critical_alerts"))
    if critical > 0:
        _raw = critical * w["SCORE_PTS_PER_CRITICAL"]
        drivers.append(ScoreDriver("Critical alerts", _cap(_raw, 24),
                                   f"{critical:.0f} open critical alerts." + _cap_note(_raw, 24)))

    high = safe_float(signals.get("high_alerts"))
    if high > 0:
        _raw = high * w["SCORE_PTS_PER_HIGH"]
        drivers.append(ScoreDriver("High alerts", _cap(_raw, 10),
                                   f"{high:.0f} open high alerts." + _cap_note(_raw, 10)))

    query_fail = safe_float(signals.get("query_fail_pct"))
    if query_fail > 2:
        _raw = (query_fail - 2) * w["SCORE_PTS_QUERY_FAIL_PER_PCT"]
        drivers.append(
            ScoreDriver("Query failures", _cap(_raw, 12),
                        f"{query_fail:.1f}% of queries failed." + _cap_note(_raw, 12))
        )

    task_fail = safe_float(signals.get("task_fail_pct"))
    if task_fail > 1:
        _raw = (task_fail - 1) * w["SCORE_PTS_TASK_FAIL_PER_PCT"]
        drivers.append(
            ScoreDriver("Task failures", _cap(_raw, 14),
                        f"{task_fail:.1f}% of task runs failed." + _cap_note(_raw, 14))
        )

    # C8: queue_minutes and spill_gb are PER-DAY RATES, not window totals. Callers
    # reading a multi-hour cumulative sum must divide by the days the window covers
    # (overview.py does) — otherwise the same steady load trips these fixed
    # thresholds purely as a function of how long the window has been open.
    queue_minutes = safe_float(signals.get("queue_minutes"))
    if queue_minutes > 10:
        _raw = (queue_minutes - 10) * w["SCORE_PTS_QUEUE_PER_MIN"]
        drivers.append(
            ScoreDriver("Queueing", _cap(_raw, 10),
                        f"{queue_minutes:.0f} queued minutes per day." + _cap_note(_raw, 10))
        )

    spill_gb = safe_float(signals.get("spill_gb"))
    if spill_gb > 5:
        _raw = (spill_gb - 5) * w["SCORE_PTS_SPILL_PER_GB"]
        drivers.append(
            ScoreDriver("Remote spill", _cap(_raw, 8),
                        f"{spill_gb:.1f} GB per day spilled to remote storage." + _cap_note(_raw, 8))
        )

    stale = safe_float(signals.get("stale_sources"))
    if stale > 0:
        _raw = stale * w["SCORE_PTS_PER_STALE_SOURCE"]
        drivers.append(ScoreDriver("Stale telemetry", _cap(_raw, 12),
                                   f"{stale:.0f} fact sources stale." + _cap_note(_raw, 12)))

    open_high_actions = safe_float(signals.get("open_high_actions"))
    if open_high_actions > 0:
        _raw = open_high_actions * w["SCORE_PTS_PER_OPEN_ACTION"]
        drivers.append(
            ScoreDriver("Owner queue", _cap(_raw, 9),
                        f"{open_high_actions:.0f} open high-severity actions." + _cap_note(_raw, 9))
        )

    total_penalty = sum(d.penalty for d in drivers)
    score = round(max(0.0, 100.0 - total_penalty))
    state = "Healthy" if score >= 85 else "Watch" if score >= 70 else "Degraded" if score >= 50 else "At risk"
    ranked = tuple(sorted(drivers, key=lambda d: d.penalty, reverse=True))
    return PlatformScore(score=score, state=state, drivers=ranked)


def score_history(inputs: pd.DataFrame, weights: dict | None = None,
                  monthly_budget_usd: float = 0.0, rate_usd: float = 3.68,
                  ai_rate_usd: float = 2.20) -> pd.DataFrame:
    """Retro platform score per day from fact-derived signals.

    ``inputs`` (one row per DAY): CREDITS_BILLED, CREDITS_BILLED_AI, CRIT_RAISED,
    HIGH_RAISED, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB, TASK_RUNS,
    TASK_FAILED. Budget pct uses the month-to-date cumulative spend against the
    monthly budget, like the live score. Labeled RETRO: the live score also counts
    stale sources and open actions, which facts don't carry per-day — the
    trend is comparable, the absolute value can differ by a few points.

    E3 — first-partial-month artifact: MTD is a cumsum WITHIN each ``_MONTH`` group
    of the frame it is handed, so when the window opens mid-month (a 30-day call on
    the 12th starts on the 13th of the prior month) that first month's cumsum
    restarts at the window edge and understates true month-to-date spend. The
    budget penalty is therefore too small — the score too HIGH — for the leading
    partial month, and steps down when the first whole month begins. Read the left
    edge of the trend as unreliable rather than as an improvement that reversed;
    it self-corrects at the first month boundary inside the window.
    """
    if inputs is None or inputs.empty or "DAY" not in inputs.columns:
        return pd.DataFrame()
    frame = inputs.copy()
    frame["DAY"] = pd.to_datetime(frame["DAY"], errors="coerce")
    frame = frame.dropna(subset=["DAY"]).sort_values("DAY")
    for col in ("CREDITS_BILLED", "CREDITS_BILLED_AI", "CRIT_RAISED", "HIGH_RAISED", "QUERY_COUNT",
                "FAILED_COUNT", "QUEUED_SEC", "SPILL_GB", "TASK_RUNS", "TASK_FAILED"):
        frame[col] = frame.get(col, 0).map(safe_float) if col in frame.columns else 0.0
    frame["_MONTH"] = frame["DAY"].dt.to_period("M")
    # C1 (V061): price AI/Cortex credits at the AI rate. MTD_USD = cumulative
    # OTHER credits x rate + cumulative AI credits x ai_rate (CREDITS_BILLED_AI is 0
    # for pre-V061 rows / readers that predate the split, so it degrades to all-compute).
    _rate = safe_float(rate_usd, 3.68)
    _ai_rate = safe_float(ai_rate_usd, 2.20)
    frame["_OTHER_CR"] = frame["CREDITS_BILLED"] - frame["CREDITS_BILLED_AI"]
    frame["_MTD_USD"] = (frame.groupby("_MONTH")["_OTHER_CR"].cumsum() * _rate
                         + frame.groupby("_MONTH")["CREDITS_BILLED_AI"].cumsum() * _ai_rate)
    budget = safe_float(monthly_budget_usd)
    rows = []
    for _, r in frame.iterrows():
        queries = r["QUERY_COUNT"]
        tasks = r["TASK_RUNS"]
        result = platform_score(signals={
            "budget_pct": (r["_MTD_USD"] / budget * 100) if budget > 0 else 0,
            "critical_alerts": r["CRIT_RAISED"],
            "high_alerts": r["HIGH_RAISED"],
            "query_fail_pct": (r["FAILED_COUNT"] / queries * 100) if queries else 0,
            "task_fail_pct": (r["TASK_FAILED"] / tasks * 100) if tasks else 0,
            "queue_minutes": r["QUEUED_SEC"] / 60.0,
            "spill_gb": r["SPILL_GB"],
        }, weights=weights)
        rows.append({"DAY": r["DAY"].date(), "SCORE": result.score, "STATE": result.state})
    return pd.DataFrame(rows)
