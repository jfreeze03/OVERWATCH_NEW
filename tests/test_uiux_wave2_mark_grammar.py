"""UI/UX master list — Wave 2 mark-grammar (C38).

Charts encode data PROVENANCE in their marks: measured-and-complete is solid,
measured-but-provisional (the newest metering day, an in-flight month — the window
closed but the data hasn't) dims to one shared PROVISIONAL_OPACITY, and a MODELED /
projected series is drawn as its own distinct mark (a hollow, dashed-outline bar),
never like an observation and never confused with the provisional dimming. C38 names
these conventions (`PROVISIONAL_OPACITY` + `_provisional_opacity`, `MODELED_FILL_OPACITY`
+ `_MODELED_DASH`) so every chart reads identically and future charts reuse them.
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


def test_c38_modeled_mark_is_distinct_from_measured_and_provisional():
    # the modeled mark rides channels a provisional bar never uses, and its dim is a
    # DIFFERENT value than 0.45 so the two provenance states can never be confused.
    assert charts.MODELED_FILL_OPACITY == 0.20
    assert charts.MODELED_FILL_OPACITY != charts.PROVISIONAL_OPACITY
    assert charts._MODELED_DASH == [5, 3]
    df = pd.DataFrame({"DB": ["A", "B", "C"], "USD": [1200.0, 800.0, 300.0]})
    # projection: hollow flat fill + dashed accent outline, NOT the 0.45 opacity dim
    modeled = _spec(charts.bar_usd, df, "DB", "USD", title="Projected growth $/mo", modeled=True)
    md = json.loads(modeled)["layer"][0]["mark"]
    assert md["strokeDash"] == [5, 3] and md["stroke"] and md["fill"]
    assert md["fillOpacity"] == charts.MODELED_FILL_OPACITY
    assert "color" not in md                         # no solid gradient fill
    assert md.get("opacity") != charts.PROVISIONAL_OPACITY
    assert '"title": "Projected $/mo"' in modeled    # hover confirms it's a projection
    # measured (default) is UNCHANGED: solid gradient, no stroke/dash/fillOpacity
    measured = _spec(charts.bar_usd, df, "DB", "USD", title="$ by cost arm")
    mm = json.loads(measured)["layer"][0]["mark"]
    assert "color" in mm and "strokeDash" not in mm and "fillOpacity" not in mm


def test_c38_projected_storage_growth_call_site_is_modeled():
    # the ONE charted projection (Cost▸Optimize storage growth movers, GROWTH_USD_30D)
    # must pass the modeled treatment; measured bar_usd callers stay solid.
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    assert "charts.bar_usd(growing" in opt              # the projected call exists
    block = opt.split("charts.bar_usd(growing", 1)[1][:200]
    assert '"GROWTH_USD_30D"' in block and "modeled=True" in block
    # the measured object-cost bars on the same page are NOT modeled
    meas = opt.split("charts.bar_usd(_adf", 1)[1][:200]
    assert "modeled=True" not in meas
