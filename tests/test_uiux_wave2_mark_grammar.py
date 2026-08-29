"""UI/UX master list — Wave 2 mark-grammar (C38).

Charts encode data PROVENANCE in their marks: measured-and-complete is solid,
measured-but-provisional (the newest metering day, an in-flight month — the window
closed but the data hasn't) dims to one shared PROVISIONAL_OPACITY, and a guess is
never drawn as an observation (the forecast is a KPI, not a chart band). C38 names
that convention (`PROVISIONAL_OPACITY` + `_provisional_opacity`) so every chart's
"partial, not a drop" reads identically and future charts reuse it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.ui import charts

_ROOT = Path(__file__).resolve().parents[1]


def test_c38_provisional_opacity_constant_and_helper():
    assert charts.PROVISIONAL_OPACITY == 0.45
    enc = charts._provisional_opacity("PROVISIONAL")   # alt.condition returns a plain dict
    # a flagged (provisional) row dims to the constant; complete rows stay solid
    assert enc["condition"]["test"] == "datum.PROVISIONAL"
    assert enc["condition"]["value"] == charts.PROVISIONAL_OPACITY
    assert enc["value"] == 1.0
    # the field is parameterized (spend_trend uses PROVISIONAL, the boss chart _PARTIAL)
    assert charts._provisional_opacity("_PARTIAL")["condition"]["test"] == "datum._PARTIAL"


def _spec(fn, *args, **kwargs):
    captured = {}
    _orig_chart = charts.st.altair_chart
    _orig_cap = charts.st.caption
    charts.st.altair_chart = lambda ch, **k: captured.__setitem__("d", ch.to_dict())
    charts.st.caption = lambda *a, **k: None
    try:
        fn(*args, **kwargs)
    finally:
        charts.st.altair_chart = _orig_chart
        charts.st.caption = _orig_cap
    return json.dumps(captured.get("d", {}))


def test_c38_spend_trend_dims_the_newest_day_via_the_helper():
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03"]),
                       "USD": [100.0, 200.0, 150.0]})
    spec = _spec(charts.spend_trend, df)
    assert '"datum.PROVISIONAL"' in spec and '"value": 0.45' in spec


def test_c38_boss_chart_dims_the_partial_month_via_the_helper():
    df = pd.DataFrame({"MONTH": ["2026-07", "2026-08"], "WH": ["A", "A"],
                       "USD": [100_000.0, 50_000.0]})
    spec = _spec(charts.monthly_stacked_usd, df, "MONTH", "WH", "USD", partial_month="2026-08")
    assert '"datum._PARTIAL"' in spec and '"value": 0.45' in spec


def test_c38_no_stray_inline_provisional_dimming_remains():
    # every provisional-dim site must go through the shared helper, so the magic
    # 0.45 lives in exactly one place.
    src = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
    assert 'alt.condition("datum.PROVISIONAL", alt.value(0.45)' not in src
    assert 'alt.condition("datum._PARTIAL", alt.value(0.45)' not in src
    # the module docstring no longer claims a forecast BAND (the forecast is a KPI)
    assert "forecast band" not in src.split('"""', 2)[1]
