"""Warehouse-fleet consolidation recommender (rec#20). Pure functions; no Streamlit.

The sizing simulator only scales warehouses UP/DOWN in isolation; nothing looked
across the fleet for warehouses that could share one. Two warehouses of the SAME
size class and owner whose active hours barely overlap can plausibly run on ONE —
the workloads never collide — retiring the mostly-idle one's fixed overhead. This
is a REVIEW-ONLY shortlist: it names the pair and a conservative saving (the
retired warehouse's idle tail), not an automatic change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.logic.formulas import safe_float


@dataclass(frozen=True)
class WarehouseProfile:
    name: str
    size_class: str
    owner: str
    active_hours: frozenset[int]     # hours-of-day (0..23) the warehouse actually runs queries
    monthly_idle_usd: float


@dataclass(frozen=True)
class ConsolidationCandidate:
    keep: str
    retire: str
    size_class: str
    owner: str
    shared_hours: int
    est_monthly_saving_usd: float


def consolidation_candidates(profiles: list[WarehouseProfile], *,
                             max_overlap_hours: int = 1,
                             min_saving_usd: float = 5.0) -> list[ConsolidationCandidate]:
    """Pair same-size-class, same-owner warehouses whose active hours barely overlap.

    A pair qualifies when it shares at most ``max_overlap_hours`` active hours (no
    concurrency collision). The retire target is the warehouse with FEWER active
    hours (less disruptive to move, and usually the more idle one); the estimated
    saving is that warehouse's monthly idle tail. Candidates are ranked by saving
    and assigned greedily so each warehouse appears in at most one merge.
    """
    eligible = [p for p in profiles if p.active_hours]
    pairs: list[ConsolidationCandidate] = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            if a.size_class != b.size_class or a.owner != b.owner:
                continue
            shared = len(a.active_hours & b.active_hours)
            if shared > max_overlap_hours:
                continue
            retire, keep = (a, b) if len(a.active_hours) <= len(b.active_hours) else (b, a)
            saving = safe_float(retire.monthly_idle_usd)
            if saving < min_saving_usd:
                continue
            pairs.append(ConsolidationCandidate(
                keep=keep.name, retire=retire.name, size_class=a.size_class,
                owner=a.owner, shared_hours=shared, est_monthly_saving_usd=round(saving, 2)))

    pairs.sort(key=lambda c: c.est_monthly_saving_usd, reverse=True)
    used: set[str] = set()
    chosen: list[ConsolidationCandidate] = []
    for cand in pairs:
        if cand.keep in used or cand.retire in used:
            continue
        used.add(cand.keep)
        used.add(cand.retire)
        chosen.append(cand)
    return chosen
