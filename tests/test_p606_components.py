"""v4.606.0 PR-1 (cluster c04-components): behaviour locks for the shared UI machinery fixes in
app/ui/components.py, app/ui/charts.py and app/ui/status_colors.py.

Each test drives the real function (with ``st`` stubbed where it renders) and fails on the
pre-fix code at 04fd374e:

  R1-211  delta_css painted a NULL (NaN) delta cell green/red.
"""

from __future__ import annotations

import math

import pandas as pd


# ---------------------------------------------------------------------------
# R1-211: a NULL delta has no direction
# ---------------------------------------------------------------------------

def test_delta_css_leaves_a_null_or_infinite_delta_uncolored():
    from app.ui.status_colors import delta_css
    # Styler.map hands a NULL delta in as float NaN (_coerce_object_numerics); a missing
    # prior (pct_delta -> None) is not "better" or "worse".
    assert delta_css(float("nan"), "DELTA_USD") == ""
    assert delta_css(float("nan"), "DELTA_HIT_PCT") == ""
    assert delta_css(math.inf, "DELTA_USD") == "" and delta_css(-math.inf, "DELTA_USD") == ""
    assert delta_css(pd.NA, "DELTA_USD") == "" and delta_css(None, "DELTA_USD") == ""
    # real movements keep their polarity colors
    assert delta_css(5.0, "DELTA_USD") != "" and delta_css(-5.0, "DELTA_USD") != ""
    assert delta_css(5.0, "DELTA_USD") != delta_css(-5.0, "DELTA_USD")


def test_delta_css_nan_cell_is_uncolored_in_the_rendered_styler():
    from app.ui.status_colors import delta_css
    df = pd.DataFrame({"A_USD": [500.0, 100.0], "DELTA_PCT": [float("nan"), 25.0]})
    ctx = df.style.map(lambda v: delta_css(v, "DELTA_PCT"), subset=["DELTA_PCT"])._compute().ctx
    assert not ctx.get((0, 1))           # the new-spend row's empty Δ% carries no color
    assert ctx.get((1, 1))               # a real +25% still does
