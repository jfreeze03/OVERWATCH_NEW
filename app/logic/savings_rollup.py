"""Aggregate the addressable-savings backlog into one de-duplicated headline (rec#16).

Each advisor estimates recoverable dollars independently and books into
SAVINGS_LEDGER; nothing summed them into a single "total addressable monthly
savings". Naive summing DOUBLE-COUNTS, because idle-tune and size-down on the SAME
warehouse both recover the same idle credits. This module de-duplicates those
overlaps (keep the larger), then ranks the rest by confidence x dollars. Pure; no
Streamlit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.logic.formulas import safe_float

# Sources that address the SAME money on the same target — keep only the largest.
# Idle-tune and right-sizing both recover a warehouse's idle credits.
_OVERLAP_GROUPS: tuple[frozenset[str], ...] = (frozenset({"IDLE", "RESIZE"}),)

_CONFIDENCE_WEIGHT = {"HIGH": 1.0, "VERIFIED": 1.0, "MEDIUM": 0.6,
                      "ESTIMATED": 0.5, "LOW": 0.3}


def confidence_weight(label: object) -> float:
    """Map an advisor's confidence label to a 0..1 weight (unknown -> low)."""
    return _CONFIDENCE_WEIGHT.get(str(label).upper(), 0.3)


@dataclass(frozen=True)
class SavingsOpportunity:
    source: str            # IDLE | RESIZE | STORAGE | RETENTION | CLUSTERING | WASTE | LEDGER
    target: str            # the warehouse / table / db the saving is on
    monthly_usd: float
    confidence: float      # 0..1


@dataclass(frozen=True)
class SavingsRollup:
    total_monthly_usd: float
    items: tuple[SavingsOpportunity, ...]     # kept, ranked by confidence x dollars desc
    dropped: tuple[SavingsOpportunity, ...]   # overlap double-counts removed


def _overlap_group(source: str) -> frozenset[str] | None:
    for group in _OVERLAP_GROUPS:
        if source in group:
            return group
    return None


def rollup_savings(opportunities: list[SavingsOpportunity]) -> SavingsRollup:
    """De-duplicate overlapping opportunities on the same target, then rank.

    Within an overlap group (e.g. IDLE + RESIZE) on the same target, only the
    largest dollar estimate survives — the rest are dropped as double-counts.
    Opportunities in no overlap group are always kept. Non-positive estimates are
    ignored. Kept items are ranked by confidence x dollars; the total sums the
    kept items.
    """
    positive = [o for o in opportunities if safe_float(o.monthly_usd) > 0]
    kept: list[SavingsOpportunity] = []
    dropped: list[SavingsOpportunity] = []
    overlap_buckets: dict[tuple[str, frozenset[str]], list[SavingsOpportunity]] = defaultdict(list)

    for opp in positive:
        group = _overlap_group(opp.source)
        if group is None:
            kept.append(opp)
        else:
            overlap_buckets[(opp.target, group)].append(opp)

    for bucket in overlap_buckets.values():
        ranked = sorted(bucket, key=lambda o: safe_float(o.monthly_usd), reverse=True)
        kept.append(ranked[0])
        dropped.extend(ranked[1:])

    kept.sort(key=lambda o: safe_float(o.confidence) * safe_float(o.monthly_usd), reverse=True)
    total = round(sum(safe_float(o.monthly_usd) for o in kept), 2)
    return SavingsRollup(total, tuple(kept), tuple(dropped))
