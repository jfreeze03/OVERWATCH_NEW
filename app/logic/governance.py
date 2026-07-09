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


# Per-unit penalty weights (SETTINGS overrides via resolve_gov_weights);
# caps stay fixed so no single category dominates.
DEFAULT_GOV_WEIGHTS = {
    "GOV_PTS_MFA_GAP": 5.0,
    "GOV_PTS_EXPIRED_CRED": 8.0,
    "GOV_PTS_EXPIRING_CRED": 2.0,
    "GOV_PTS_BREAKGLASS_GRANT": 6.0,
    "GOV_PTS_NO_MONITOR": 4.0,
    "GOV_PTS_NO_AUTOSUSPEND": 3.0,
}


def resolve_gov_weights(settings: dict | None) -> dict:
    """Merge SETTINGS overrides onto defaults (bad values fall back)."""
    weights = dict(DEFAULT_GOV_WEIGHTS)
    for key, default in DEFAULT_GOV_WEIGHTS.items():
        value = safe_float((settings or {}).get(key), -1.0)
        weights[key] = value if value >= 0 else default
    return weights


def governance_drift(inputs: dict, weights: dict | None = None) -> GovernanceDrift:
    """Inputs (all optional counts): mfa_gap_users, expired_credentials,
    expiring_credentials, breakglass_grants_30d, warehouses_no_monitor,
    warehouses_no_autosuspend. Weights from resolve_gov_weights(settings)."""
    w = dict(DEFAULT_GOV_WEIGHTS)
    w.update(weights or {})
    drivers: list[ScoreDriver] = []

    mfa = safe_float(inputs.get("mfa_gap_users"))
    if mfa > 0:
        drivers.append(ScoreDriver(
            "MFA gaps", _cap(mfa * w["GOV_PTS_MFA_GAP"], 25),
            f"{mfa:.0f} users without MFA who password-logged-in within 30d (same definition as Security > Access)."))

    expired = safe_float(inputs.get("expired_credentials"))
    if expired > 0:
        drivers.append(ScoreDriver(
            "Expired credentials", _cap(expired * w["GOV_PTS_EXPIRED_CRED"], 24),
            f"{expired:.0f} active credentials already past EXPIRES_AT."))

    expiring = safe_float(inputs.get("expiring_credentials"))
    if expiring > 0:
        drivers.append(ScoreDriver(
            "Expiring credentials", _cap(expiring * w["GOV_PTS_EXPIRING_CRED"], 10),
            f"{expiring:.0f} credentials expire within 10 days."))

    breakglass = safe_float(inputs.get("breakglass_grants_30d"))
    if breakglass > 0:
        drivers.append(ScoreDriver(
            "Break-glass grants", _cap(breakglass * w["GOV_PTS_BREAKGLASS_GRANT"], 18),
            f"{breakglass:.0f} ACCOUNTADMIN-tier grants in the last 30 days."))

    no_monitor = safe_float(inputs.get("warehouses_no_monitor"))
    if no_monitor > 0:
        drivers.append(ScoreDriver(
            "No resource monitor", _cap(no_monitor * w["GOV_PTS_NO_MONITOR"], 12),
            f"{no_monitor:.0f} warehouses without a resource monitor."))

    no_suspend = safe_float(inputs.get("warehouses_no_autosuspend"))
    if no_suspend > 0:
        drivers.append(ScoreDriver(
            "No auto-suspend", _cap(no_suspend * w["GOV_PTS_NO_AUTOSUSPEND"], 12),
            f"{no_suspend:.0f} warehouses without auto-suspend."))

    score = round(max(0.0, 100.0 - sum(d.penalty for d in drivers)))
    state = "Healthy" if score >= 90 else ("Watch" if score >= 75 else "Act")
    return GovernanceDrift(score=score, state=state, drivers=tuple(drivers))
