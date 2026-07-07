"""Evidence-based platform operating score.

The score is computed from observed signals with named penalty drivers, so an
executive can always ask "why 74?" and get the exact deductions. It never
reads a self-reported score from a mart as truth (old-app principle, kept).
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def platform_score(signals: dict) -> PlatformScore:
    """Score 0-100 from a signals dict. Missing signals simply add no penalty.

    Expected keys (all optional):
      budget_pct, critical_alerts, high_alerts, query_fail_pct, task_fail_pct,
      queue_minutes, spill_gb, stale_sources, open_high_actions
    """
    drivers: list[ScoreDriver] = []

    budget_pct = safe_float(signals.get("budget_pct"))
    if budget_pct > 100:
        penalty = _cap((budget_pct - 100) * 0.5, 20)
        drivers.append(ScoreDriver("Over budget", penalty, f"Spend at {budget_pct:.0f}% of monthly budget."))

    critical = safe_float(signals.get("critical_alerts"))
    if critical > 0:
        drivers.append(ScoreDriver("Critical alerts", _cap(critical * 6, 24), f"{critical:.0f} open critical alerts."))

    high = safe_float(signals.get("high_alerts"))
    if high > 0:
        drivers.append(ScoreDriver("High alerts", _cap(high * 2, 10), f"{high:.0f} open high alerts."))

    query_fail = safe_float(signals.get("query_fail_pct"))
    if query_fail > 2:
        drivers.append(
            ScoreDriver("Query failures", _cap((query_fail - 2) * 1.5, 12), f"{query_fail:.1f}% of queries failed.")
        )

    task_fail = safe_float(signals.get("task_fail_pct"))
    if task_fail > 1:
        drivers.append(
            ScoreDriver("Task failures", _cap((task_fail - 1) * 2, 14), f"{task_fail:.1f}% of task runs failed.")
        )

    queue_minutes = safe_float(signals.get("queue_minutes"))
    if queue_minutes > 10:
        drivers.append(
            ScoreDriver("Queueing", _cap((queue_minutes - 10) * 0.3, 10), f"{queue_minutes:.0f} queued minutes in window.")
        )

    spill_gb = safe_float(signals.get("spill_gb"))
    if spill_gb > 5:
        drivers.append(
            ScoreDriver("Remote spill", _cap((spill_gb - 5) * 0.5, 8), f"{spill_gb:.1f} GB spilled to remote storage.")
        )

    stale = safe_float(signals.get("stale_sources"))
    if stale > 0:
        drivers.append(ScoreDriver("Stale telemetry", _cap(stale * 4, 12), f"{stale:.0f} fact sources stale."))

    open_high_actions = safe_float(signals.get("open_high_actions"))
    if open_high_actions > 0:
        drivers.append(
            ScoreDriver("Owner queue", _cap(open_high_actions * 1.5, 9), f"{open_high_actions:.0f} open high-severity actions.")
        )

    total_penalty = sum(d.penalty for d in drivers)
    score = int(round(max(0.0, 100.0 - total_penalty)))
    state = "Healthy" if score >= 85 else "Watch" if score >= 70 else "Degraded" if score >= 50 else "At risk"
    ranked = tuple(sorted(drivers, key=lambda d: d.penalty, reverse=True))
    return PlatformScore(score=score, state=state, drivers=ranked)
