"""Estimate a USD column beside each credit-quantity column in an Ask evidence frame.

Credits are priced at the compute rate ($3.68 default) or the AI/Cortex "CoCo" rate
($2.20 default). The rate is chosen per COLUMN, not per question: a column whose name
carries an AI/Cortex segment (AI_CREDITS, CORTEX_CREDITS, TOKEN_CREDITS, ...) prices at
the AI rate; every other credit column prices at the compute rate. An answerer whose
whole intent is AI can force the AI rate via `intent_is_ai`. This keeps a warehouse /
cloud-services credit column from ever being mispriced at the AI rate, while a future
Cortex answerer gets the right rate automatically.

Pure and Streamlit-free so it is unit-tested (tests/test_ask_pricing.py).
"""

from __future__ import annotations

import pandas as pd

# Underscore-delimited column segments that mark an AI/Cortex credit. Matched as whole
# segments (never as substrings) so ALLOC_CREDITS / MAIN_CREDITS never trip on "AI".
_AI_SEGMENTS = frozenset({"AI", "GENAI", "CORTEX", "COCO", "LLM", "TOKEN", "TOKENS"})


def _is_credit_quantity(col: object) -> bool:
    """A column that holds an amount OF credits (ALLOC_CREDITS, CS_CREDITS, CREDITS).

    Deliberately narrow: a ratio/share (CREDIT_SHARE) or a rate (CS_PER_1K_RUNS) is NOT
    a credit amount and must not be dollarized.
    """
    u = str(col).upper()
    return u == "CREDITS" or u.endswith("_CREDITS")


def _column_is_ai(col: object, intent_is_ai: bool) -> bool:
    if intent_is_ai:
        return True
    return bool(_AI_SEGMENTS & set(str(col).upper().split("_")))


def add_usd_estimates(
    ev: pd.DataFrame | None,
    *,
    compute_rate: float,
    ai_rate: float,
    intent_is_ai: bool = False,
) -> tuple[pd.DataFrame | None, list[str], set[float]]:
    """Insert a `<col>_USD` estimate immediately after each credit-quantity column.

    Returns (frame, usd_column_names, rates_used). The frame is returned unchanged
    (a copy) when there is nothing to price, so callers can render it either way.
    `rates_used` lets the caller disclose which rate(s) were applied.
    """
    if ev is None or getattr(ev, "empty", True):
        return ev, [], set()
    out = ev.copy()
    usd_cols: list[str] = []
    rates_used: set[float] = set()
    # Iterate the ORIGINAL column order; inserting shifts positions, so recompute the
    # insert index from the (possibly grown) frame each time.
    for col in list(ev.columns):
        if not _is_credit_quantity(col):
            continue
        usd_name = f"{col}_USD"
        if usd_name in out.columns:
            continue
        rate = ai_rate if _column_is_ai(col, intent_is_ai) else compute_rate
        dollars = (pd.to_numeric(out[col], errors="coerce") * rate).round(2)
        out.insert(out.columns.get_loc(col) + 1, usd_name, dollars)
        usd_cols.append(usd_name)
        rates_used.add(round(float(rate), 2))
    return out, usd_cols, rates_used
