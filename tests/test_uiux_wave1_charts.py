"""UI/UX master list — Wave 1 chart-legibility batch (W1b).

Locks: F37 critical-path EDGES carry the accent · F38 static DAG legend ·
F39 compact SI dollar axes on large magnitudes · F40 day-grain tooltips show the
day (not a midnight timestamp) · F47 the hour heatmap keeps every clock column.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.task_graph import TaskGraphShape
from app.ui import palette
from app.ui.charts import _DAY_TIP_FMT, _TASK_DAG_TEMPLATE, _task_dag_markup, _usd_fmt

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "charts.py").read_text(
    encoding="utf-8")


# ---- F39: magnitude-aware dollar format --------------------------------------

def test_usd_fmt_compacts_large_and_keeps_small_exact():
    assert _usd_fmt(1_240_000) == "$,.3~s"    # "$1.24M" ticks
    assert _usd_fmt(10_000) == "$,.3~s"
    assert _usd_fmt(9_999) == "$,.0f"          # small charts keep exact dollars
    assert _usd_fmt(0) == "$,.0f"
    assert _usd_fmt(None) == "$,.0f"           # garbage degrades, never raises
    assert _usd_fmt("nope") == "$,.0f"


def test_usd_axes_are_magnitude_aware_at_the_big_sites():
    # the flat "$,.0f" axis must be gone from the large-magnitude sites; the
    # deliberate sub-dollar stacked rule keeps its cents branch.
    for needle in ('_usd_fmt(data["USD"].max())', "_usd_fmt(dmax)",
                   "_usd_fmt(_max_stack)",
                   '_usd_fmt(data[["Start", "End"]].max().max())',
                   '_usd_fmt(credit_data["USD"].max())',
                   '_usd_fmt(data["IMPACT_USD_30D"].max())',
                   '_usd_fmt(folded["Value"].max())'):
        assert needle in _SRC, needle


# ---- F40: day-grain tooltips -------------------------------------------------

def test_day_tooltips_are_formatted_not_midnight_timestamps():
    assert _DAY_TIP_FMT == "%b %d, %Y"
    # the named sites carry the format; no bare Day tooltip remains in the
    # spend/stacked family (string-shorthand "Day:T" sites are W2's F41/C39).
    assert 'alt.Tooltip("Day:T"),' not in _SRC
    assert _SRC.count("format=_DAY_TIP_FMT") >= 4


# ---- F47: hour heatmap full clock --------------------------------------------

def test_hour_heatmap_pins_all_24_columns():
    idx = _SRC.index('title="hour of day"')
    assert "domain=list(range(24))" in _SRC[idx:idx + 200]


# ---- F37: critical-path edges ------------------------------------------------

def _dag_inputs():
    df = pd.DataFrame([
        {"TASK_FQN": "DB.S.A", "CRITICAL_PATH": "True", "RUN_STATE": "succeeded"},
        {"TASK_FQN": "DB.S.B", "CRITICAL_PATH": "True", "RUN_STATE": "succeeded"},
        {"TASK_FQN": "DB.S.C", "CRITICAL_PATH": "False", "RUN_STATE": "succeeded"},
    ])
    shape = TaskGraphShape(
        edges=(("DB.S.A", "DB.S.B"), ("DB.S.A", "DB.S.C")),
        levels=(("DB.S.A", 0), ("DB.S.B", 1), ("DB.S.C", 1)),
        missing_predecessors=(), duplicate_nodes=(), cyclic_nodes=(),
    )
    return df, shape


def test_critical_edges_get_the_accent_class():
    df, shape = _dag_inputs()
    markup = _task_dag_markup(df, shape, height=500)
    # A->B joins two critical nodes -> accent route; A->C stays a plain edge.
    assert markup.count('class="edge edge-critical"') == 1
    assert markup.count('class="edge"') == 1
    assert "arrowCritical" in markup                     # accent arrowhead defined


def test_no_critical_nodes_means_no_critical_edges():
    df, shape = _dag_inputs()
    df["CRITICAL_PATH"] = "False"
    markup = _task_dag_markup(df, shape, height=500)
    assert "edge-critical" not in markup.split("<style>", 1)[1].split("</style>", 1)[0] \
        or 'class="edge edge-critical"' not in markup


# ---- F38: DAG legend ---------------------------------------------------------

def test_dag_legend_decodes_every_node_color():
    assert 'class="dag-legend"' in _TASK_DAG_TEMPLATE
    for label in ("failed", "critical path", "healthy", "suspended"):
        assert label in _TASK_DAG_TEMPLATE, label
    assert ".edge-critical" in _TASK_DAG_TEMPLATE
    # hues come FROM the palette at build time (drift-proof; the template itself
    # carries placeholders so the literal-hex guards stay meaningful).
    for ph in ("__C_BAD__", "__C_ACCENT__", "__C_OK__", "__C_MUTED__"):
        assert ph in _TASK_DAG_TEMPLATE, ph
    df, shape = _dag_inputs()
    markup = _task_dag_markup(df, shape, height=500)
    for hue in (palette.BAD, palette.ACCENT, palette.OK, palette.MUTED):
        assert hue in markup, hue
    assert "__C_" not in markup                          # every placeholder resolved
