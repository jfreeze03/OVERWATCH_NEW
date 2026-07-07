"""Governance-drift score: hygiene debt with named deductions. Pure module.

Same philosophy as the platform score — an executive can ask "why 82?" and
get exact items. Weights are fixed and documented here (drift items are
countable facts, unlike the platform score's tunable severities); caps stop
any one category from dominating.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .formulas import safe_float
from .scoring import ScoreDriver


@dataclass(frozen=True)
class GovernanceDrift:
    score: int
    state: str
    drivers: tuple[ScoreDriver, ...] = field(default_factory=tuple)


def _cap(value: float, cap: float) -> float:
    return min(max(value, 0.0), cap)


def governance_drift(inputs: dict) -> GovernanceDrift:
    """Inputs (all optional counts): mfa_gap_users, expired_credentials,
    expiring_credentials, breakglass_grants_30d, warehouses_no_monitor,
    warehouses_no_autosuspend."""
    drivers: list[ScoreDriver] = []

    mfa = safe_float(inputs.get("mfa_gap_users"))
    if mfa > 0:
        drivers.append(ScoreDriver(
            "MFA gaps", _cap(mfa * 5, 25),
            f"{mfa:.0f} enabled password-login users without MFA 7+ days after creation."))

    expired = safe_float(inputs.get("expired_credentials"))
    if expired > 0:
        drivers.append(ScoreDriver(
            "Expired credentials", _cap(expired * 8, 24),
            f"{expired:.0f} active credentials already past EXPIRES_AT."))

    expiring = safe_float(inputs.get("expiring_credentials"))
    if expiring > 0:
        drivers.append(ScoreDriver(
            "Expiring credentials", _cap(expiring * 2, 10),
            f"{expiring:.0f} credentials expire within 30 days."))

    breakglass = safe_float(inputs.get("breakglass_grants_30d"))
    if breakglass > 0:
        drivers.append(ScoreDriver(
            "Break-glass grants", _cap(breakglass * 6, 18),
            f"{breakglass:.0f} ACCOUNTADMIN-tier grants in the last 30 days."))

    no_monitor = safe_float(inputs.get("warehouses_no_monitor"))
    if no_monitor > 0:
        drivers.append(ScoreDriver(
            "No resource monitor", _cap(no_monitor * 4, 12),
            f"{no_monitor:.0f} warehouses without a resource monitor."))

    no_suspend = safe_float(inputs.get("warehouses_no_autosuspend"))
    if no_suspend > 0:
        drivers.append(ScoreDriver(
            "No auto-suspend", _cap(no_suspend * 3, 12),
            f"{no_suspend:.0f} warehouses without auto-suspend."))

    score = int(round(max(0.0, 100.0 - sum(d.penalty for d in drivers))))
    state = "Healthy" if score >= 90 else ("Watch" if score >= 75 else "Act")
    return GovernanceDrift(score=score, state=state, drivers=tuple(drivers))
