"""Serverless-feature ROI verdicts (rec#6). Pure functions; no Streamlit.

The app tracks serverless SPEND (search-opt / QAS / MV / auto-clustering credits)
but never the BENEFIT side. For Query Acceleration the benefit signal Snowflake
does expose is QUERY_ACCELERATION_ELIGIBLE — how much query time was ELIGIBLE for
acceleration — so a warehouse can be classified by whether its QAS spend is
matched by eligible workload. Eligibility is a utilization signal, not a
dollarized compute saving, and the verdict says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.logic.formulas import safe_float

_SPEND_FLOOR_USD = 5.0     # below this, QAS spend is immaterial
_ELIGIBLE_FLOOR = 5        # below this many eligible queries, the workload is immaterial


@dataclass(frozen=True)
class QasVerdict:
    verdict: str
    action: str            # "drop" | "enable" | "keep" | ""


def classify_qas_roi(qas_usd: float, eligible_queries: object, *,
                     spend_floor: float = _SPEND_FLOOR_USD,
                     eligible_floor: int = _ELIGIBLE_FLOOR) -> QasVerdict:
    """Classify one warehouse's Query Acceleration ROI from its QAS spend and the
    count of queries eligible for acceleration.

    - paying but little eligible workload -> "Paying, little benefit" (drop)
    - eligible workload but not paying     -> "Eligible, QAS off" (enable)
    - paying and eligible                  -> "Working" (keep)
    - neither material                     -> "Minimal" ("")
    """
    paying = safe_float(qas_usd) >= safe_float(spend_floor)
    try:
        eligible = int(safe_float(eligible_queries)) >= int(eligible_floor)
    except (TypeError, ValueError):
        eligible = False
    if paying and not eligible:
        return QasVerdict("Paying, little benefit — QAS rarely helps here", "drop")
    if eligible and not paying:
        return QasVerdict("Eligible workload, QAS off — acceleration opportunity", "enable")
    if paying and eligible:
        return QasVerdict("Working — QAS spend is matched by eligible workload", "keep")
    return QasVerdict("Minimal — no material QAS spend or eligibility", "")
