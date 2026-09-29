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

import pandas as pd

from app.logic.formulas import safe_float
from app.logic.unread_maintenance import ACTION_VERDICTS

# Sources that address the SAME money on the same target — keep only the largest.
# Idle-tune and right-sizing both recover a warehouse's idle credits; suspending clustering on an
# unread table (Next-Fifty #30) and fixing its churny clustering both recover its clustering credits
# (both keyed on the same db.schema.table concatenation).
_OVERLAP_GROUPS: tuple[frozenset[str], ...] = (frozenset({"IDLE", "RESIZE"}),
                                                frozenset({"CLUSTERING", "UNREAD_MAINT"}))

_CONFIDENCE_WEIGHT = {"HIGH": 1.0, "VERIFIED": 1.0, "MEDIUM": 0.6,
                      "ESTIMATED": 0.5, "LOW": 0.3}


def confidence_weight(label: object) -> float:
    """Map an advisor's confidence label to a 0..1 weight (unknown -> low)."""
    return _CONFIDENCE_WEIGHT.get(str(label).upper(), 0.3)


# Effort proxy from the advisor source: a single ALTER (IDLE/RESIZE) vs a costly
# re-cluster (CLUSTERING). Lets the panel flag quick wins a dollar ranking hides.
_EFFORT_TIER = {
    "IDLE": "LOW", "RESIZE": "LOW", "UNREAD_MAINT": "LOW",
    "WASTE": "MEDIUM", "STORAGE": "MEDIUM", "RETENTION": "MEDIUM", "LEDGER": "MEDIUM",
    "CLUSTERING": "HIGH",
}


def effort_tier(source: object) -> str:
    """Rough implementation effort for an advisor source (Cost #8) — the quick-win
    signal a dollar ranking hides. IDLE/RESIZE are one ALTER; CLUSTERING is a costly
    re-cluster. Unknown sources default to MEDIUM."""
    return _EFFORT_TIER.get(str(source).upper(), "MEDIUM")


@dataclass(frozen=True)
class SavingsOpportunity:
    source: str            # IDLE | RESIZE | STORAGE | RETENTION | CLUSTERING | WASTE | LEDGER | UNREAD_MAINT
    target: str            # the warehouse / table / db the saving is on
    monthly_usd: float
    confidence: float      # 0..1


@dataclass(frozen=True)
class SavingsRollup:
    total_monthly_usd: float
    items: tuple[SavingsOpportunity, ...]     # kept, ranked by confidence x dollars desc
    dropped: tuple[SavingsOpportunity, ...]   # overlap double-counts removed


def idle_opportunities(advisor: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """rec#16 IDLE leg, extracted from Cost ▸ Optimize (v4.597) so Optimize and Proof ▸ Pipeline build
    the identical list: one SavingsOpportunity per insights.idle_advisor row with a positive NET
    actionable timer saving (ACTIONABLE_MONTHLY_USD), weighted by its SAVINGS_CONFIDENCE label.
    Behaviour is byte-equal to the inline generator it replaced; None / empty -> []."""
    if advisor is None or advisor.empty:
        return []
    return [SavingsOpportunity("IDLE", str(r["WAREHOUSE_NAME"]),
                               safe_float(r["ACTIONABLE_MONTHLY_USD"]),
                               confidence_weight(r.get("SAVINGS_CONFIDENCE")))
            for _, r in advisor.iterrows()
            if safe_float(r["ACTIONABLE_MONTHLY_USD"]) > 0]


def resize_opportunities(sized: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """rec#16 RESIZE leg (right-sizing; overlaps IDLE per warehouse — rollup_savings keeps the larger),
    extracted from Cost ▸ Optimize (v4.597): one SavingsOpportunity per sizing.size_recommendations row
    with a positive POTENTIAL_MONTHLY_SAVING_USD, weighted by its CONFIDENCE label. Byte-equal to the
    inline generator it replaced; None / empty -> []."""
    if sized is None or sized.empty:
        return []
    return [SavingsOpportunity("RESIZE", str(r["WAREHOUSE_NAME"]),
                               safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")),
                               confidence_weight(r.get("CONFIDENCE")))
            for _, r in sized.iterrows()
            if safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")) > 0]


def unread_maintenance_opportunities(verdicts: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """Next-Fifty #30 UNREAD_MAINT leg (registered for the #35 de-duplicated total; not yet in the Optimize
    headline): one SavingsOpportunity per unread_maintenance.unread_maintenance_verdicts row with an ACTION
    verdict and a positive EST_MONTHLY_USD (ESTIMATED, so MEDIUM confidence), keyed on OBJECT_FQN so it
    de-duplicates against a CLUSTERING leg on the same table. None / empty -> []."""
    if verdicts is None or verdicts.empty or "VERDICT" not in verdicts.columns:
        return []
    return [SavingsOpportunity("UNREAD_MAINT", str(r["OBJECT_FQN"]), safe_float(r.get("EST_MONTHLY_USD")),
                               confidence_weight("MEDIUM"))
            for _, r in verdicts.iterrows()
            if str(r.get("VERDICT")) in ACTION_VERDICTS and safe_float(r.get("EST_MONTHLY_USD")) > 0]


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
