"""UI/UX master list — Wave 2 tables batch (F26 + C34 rank ordinal).

Locks: F26 the primary $/credits column of a RANKED table gets an in-cell
magnitude bar (rates, prices and signed movement columns never do) · C34 a
ranked table shows a display-only # ordinal (the CSV keeps raw columns and row
order is untouched, so positional selections stay valid).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.ui.components import _rank_bar_column

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py").read_text(
    encoding="utf-8")


def _frame(**cols):
    return pd.DataFrame({k: list(v) for k, v in cols.items()})


# ---- F26: bar-column picker --------------------------------------------------

def test_picks_the_first_magnitude_column_on_a_ranked_table():
    df = _frame(WAREHOUSE=["a", "b", "c", "d"], COST_USD=[10.0, 5.0, 2.0, 1.0],
                CREDITS_TOTAL=[3, 2, 1, 1])
    assert _rank_bar_column(df, "by $ desc", set()) == "COST_USD"


def test_unranked_tiny_or_caller_configured_tables_get_no_bar():
    df = _frame(WAREHOUSE=["a", "b", "c", "d"], COST_USD=[10.0, 5.0, 2.0, 1.0])
    assert _rank_bar_column(df, "", set()) is None                 # not ranked
    assert _rank_bar_column(df.head(3), "by $", set()) is None     # too small
    assert _rank_bar_column(df, "by $", {"COST_USD"}) is None      # caller owns it


def test_rates_prices_and_signed_columns_never_bar():
    rate = _frame(K=["a", "b", "c", "d"], COST_USD_PER_TIB=[1.0, 2.0, 3.0, 4.0])
    assert _rank_bar_column(rate, "by rate", set()) is None        # a rate, not a magnitude
    price = _frame(K=["a", "b", "c", "d"], CREDIT_PRICE_USD=[3.68] * 4)
    assert _rank_bar_column(price, "by price", set()) is None
    signed = _frame(K=["a", "b", "c", "d"], DELTA_USD=[-5.0, 2.0, 1.0, 0.5])
    assert _rank_bar_column(signed, "by delta", set()) is None     # 0-floored bar misleads
    zero = _frame(K=["a", "b", "c", "d"], COST_USD=[0.0] * 4)
    assert _rank_bar_column(zero, "by $", set()) is None           # nothing to scale


def test_bar_never_raises_on_garbage():
    junk = _frame(K=["a", "b", "c", "d"], COST_USD=["x", "y", "z", "w"])
    assert _rank_bar_column(junk, "by $", set()) is None
    assert _rank_bar_column(None, "by $", set()) is None


def test_bar_wiring_pops_the_styler_format_and_keeps_help():
    idx = _SRC.index("_bar_col = _rank_bar_column(df, sort_label")
    block = _SRC[idx:idx + 800]
    assert "fmts.pop(_bar_col, None)" in block                     # no format fight
    assert "st.column_config.ProgressColumn(" in block
    assert "COLUMN_HELP.get(str(_bar_col).upper())" in block       # which-dollar help survives


# ---- C34: rank ordinal -------------------------------------------------------

def test_rank_ordinal_is_display_only_and_gated():
    idx = _SRC.index("a ranked table shows its RANK")
    block = _SRC[idx:idx + 900]
    assert 'if sort_label and len(df) >= 4 and "#" not in display_df.columns:' in block
    assert 'display_df.insert(0, "#", range(1, len(display_df) + 1))' in block
    # the CSV keeps the RAW frame (no ordinal) — export still reads df, not display_df
    assert "df.to_csv(index=False)" in _SRC


# ---- review fixes: USD preference, sparse/PERIOD guards, pin gate ------------

def test_bar_prefers_usd_over_credits():
    # a credits column can be non-monotonic against "$ desc" when two credit
    # rates coexist (AI vs standard) — the bar lands on the dollars.
    df = _frame(SERVICE=["a", "b", "c", "d"],
                BILLED_CREDITS=[1000.0, 700.0, 10.0, 5.0],
                BILLED_USD=[2200.0, 2576.0, 36.8, 18.4])
    assert _rank_bar_column(df, "$ desc", set()) == "BILLED_USD"
    # credits still bar when no dollar column qualifies
    only_credits = _frame(K=["a", "b", "c", "d"], CREDITS_TOTAL=[4.0, 3.0, 2.0, 1.0])
    assert _rank_bar_column(only_credits, "by credits", set()) == "CREDITS_TOTAL"


def test_sparse_settlement_columns_never_bar():
    # VERIFIED_USD fills only after close; NULL leading rows would render the top
    # of a ranked table as empty bars that read as zero.
    df = _frame(K=["a", "b", "c", "d"],
                VERIFIED_USD=[float("nan"), float("nan"), float("nan"), 5000.0])
    assert _rank_bar_column(df, "active status, then update", set()) is None


def test_mixed_time_base_tables_never_bar():
    # a PERIOD column marks per-row time bases (monthly vs one-time) — a shared
    # 0..max bar is exactly the cross-row comparison those tables forbid.
    df = _frame(K=["a", "b", "c", "d"], PERIOD=["monthly", "one-time", "monthly", ""],
                ESTIMATED_USD=[900.0, 12000.0, 300.0, 100.0])
    assert _rank_bar_column(df, "action priority order", set()) is None


def test_pin_gate_counts_the_ordinal_column():
    # a 7-raw-column ranked table displays 8 columns after the "#" insert — the
    # identity pin must fire there too, or scroll freezes rank without identity.
    assert "len(data_columns) >= 8" in _SRC
    assert "data_columns = list(display_df.columns)" in _SRC


def test_cost_truth_lenses_table_opts_out_of_rank_and_bar():
    ds = (Path(__file__).resolve().parents[1] / "app" / "ui"
          / "decision_studio.py").read_text(encoding="utf-8")
    # the 4-row non-comparable lenses table must not carry a sort_label (it would
    # trigger the ordinal + a credits race bar against the "do not add" contract)
    assert 'sort_label="semantic basis order"' not in ds
