"""Summary-versus-evidence contracts for dense operational surfaces."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadModelContract:
    """Maximum query calls for first-paint summary and incremental evidence."""

    key: str
    summary: str
    evidence: str
    trigger: str
    freshness: str
    summary_reads: int
    evidence_reads: int


READ_MODELS = (
    ReadModelContract(
        "action_center",
        "ranked action queue",
        "activity, comments and typed evidence links",
        "selected work item",
        "live",
        2,
        2,
    ),
    ReadModelContract(
        "entity_360",
        "ownership, service contract, watch state, work and mart metrics",
        "remediation, savings and relationship history",
        "explicit evidence toggle",
        "recent marts + live operator records",
        4,
        3,
    ),
    ReadModelContract(
        "task_run",
        "recent graph-run summaries",
        "one run's node timings, profiles and critical path",
        "explicit run-evidence toggle",
        "bounded 7-day task history",
        1,
        1,
    ),
    ReadModelContract(
        "workload_portfolio",
        "measured family impact and evidence score",
        "persistent fingerprint Entity 360 profile",
        "selected portfolio row",
        "daily marts",
        1,
        4,
    ),
    ReadModelContract(
        "slo_cockpit",
        "objective status and error-budget burn",
        "owning entity's metric and work history",
        "selected objective row",
        "objective window over daily marts",
        1,
        4,
    ),
    ReadModelContract(
        "control_pulse",
        "since-yesterday health",
        "14-day query activity trend",
        "the pulse opens",
        "hourly facts with bounded live fallback",
        2,
        1,
    ),
)


def get_contract(key: str) -> ReadModelContract:
    normalized = str(key or "").strip().lower()
    for contract in READ_MODELS:
        if contract.key == normalized:
            return contract
    raise KeyError(f"Unknown read-model contract: {normalized or 'blank'}")


def validate_contracts() -> list[str]:
    problems: list[str] = []
    keys = [contract.key for contract in READ_MODELS]
    if len(keys) != len(set(keys)):
        problems.append("duplicate read-model key")
    for contract in READ_MODELS:
        if not all((contract.summary, contract.evidence, contract.trigger, contract.freshness)):
            problems.append(f"{contract.key}: incomplete description")
        if contract.summary_reads < 1 or contract.evidence_reads < 1:
            problems.append(f"{contract.key}: read counts must be positive")
    return problems
