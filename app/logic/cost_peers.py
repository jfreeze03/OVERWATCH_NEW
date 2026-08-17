"""Cross-sectional (peer) cost-per-query outliers (repo review "What to Borrow",
wave 3).

Our anomaly z-score (app/logic/anomaly.py) is TEMPORAL — a warehouse vs its own
history. This is CROSS-SECTIONAL: each warehouse's $/query vs the POPULATION of
warehouses in the same company, so a DBA sees "this warehouse costs 3.4x the fleet
median per query" — an outlier a time-only rule can't surface.

Reuses anomaly.robust_zscores (median/MAD, returns all-zeros for <5 points or no
dispersion, so a tiny fleet never false-flags). A materiality floor drops trivial
warehouses (a 3-query sandbox with a huge $/query would otherwise swamp the median
and the MAD). Pure pandas; tested in tests/test_wave3_efficiency.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.anomaly import robust_zscores
from app.logic.formulas import credits_to_usd

OUTLIER_Z = 3.5           # high side only — a CHEAP warehouse is not a cost problem
MIN_QUERIES = 100         # materiality: ignore trivial-volume warehouses ...
MIN_CREDITS = 1.0         # ... on both query count and credits


def cost_per_query_peers(df: pd.DataFrame | None, rate_usd: float = 3.68, *,
                         threshold: float = OUTLIER_Z, min_queries: int = MIN_QUERIES,
                         min_credits: float = MIN_CREDITS,
                         group_col: str = "COMPANY") -> pd.DataFrame:
    """Per-warehouse $/query with a cross-sectional (within-company) robust z-score.

    ``df`` (warehouse_sizing_profile / eff_sizing_profile): WAREHOUSE_NAME,
    CREDITS_TOTAL, QUERY_COUNT, COMPANY. Returns the MATERIAL warehouses (past the
    floor) sorted by Z_SCORE desc: WAREHOUSE_NAME, COMPANY, USD_PER_QUERY,
    MULT_OF_MEDIAN, Z_SCORE, IS_OUTLIER, QUERY_COUNT. Empty in -> empty out."""
    cols = ["WAREHOUSE_NAME", "COMPANY", "USD_PER_QUERY", "MULT_OF_MEDIAN",
            "Z_SCORE", "IS_OUTLIER", "QUERY_COUNT"]
    if df is None or df.empty or "WAREHOUSE_NAME" not in df.columns:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    out["QUERY_COUNT"] = pd.to_numeric(out.get("QUERY_COUNT"), errors="coerce").fillna(0.0)
    out["CREDITS_TOTAL"] = pd.to_numeric(out.get("CREDITS_TOTAL"), errors="coerce").fillna(0.0)
    # Materiality floor: eliminates 0-query div-by-zero AND stops a trivial WH from
    # setting a wild median / swamping the MAD.
    out = out[(out["QUERY_COUNT"] >= min_queries) & (out["CREDITS_TOTAL"] >= min_credits)]
    if out.empty:
        return pd.DataFrame(columns=cols)
    if group_col not in out.columns:
        out[group_col] = "ALL"
    out["USD_PER_QUERY"] = (out["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate_usd, round_cents=False))
                            / out["QUERY_COUNT"].clip(lower=1))
    # Peer comparison WITHIN company (a single-company frame degenerates to one group).
    out["Z_SCORE"] = (out.groupby(group_col, dropna=False)["USD_PER_QUERY"]
                      .transform(robust_zscores).fillna(0.0)).round(1)
    med = out.groupby(group_col, dropna=False)["USD_PER_QUERY"].transform("median")
    out["MULT_OF_MEDIAN"] = (out["USD_PER_QUERY"] / med.where(med > 0)).fillna(0.0).round(1)
    out["IS_OUTLIER"] = out["Z_SCORE"] >= float(threshold)
    out["USD_PER_QUERY"] = out["USD_PER_QUERY"].round(4)
    out["QUERY_COUNT"] = out["QUERY_COUNT"].round(0).astype(int)
    # Secondary sort on the multiple (review #4): on a small fleet (<5 warehouses)
    # every robust z is 0, so a z-only sort would bury the priciest warehouse — the
    # multiple-of-median still surfaces it.
    return (out.sort_values(["Z_SCORE", "MULT_OF_MEDIAN"], ascending=False)
            .reset_index(drop=True)[cols])
