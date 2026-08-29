"""UI/UX master list — Wave 2 stacked-total cluster (F44) on monthly_stacked_usd.

The monthly "boss chart" stacks spend by warehouse. Its primary number — the
month's TOTAL spend — was only recoverable by hovering each segment and summing
by eye. F44 labels the stack total above each bar (compact SI, $.3s), so the
headline reads at a glance. The partial (in-flight) month's label dims with its
bar so a running total isn't mistaken for a finished one.

The spec is compiled headlessly (chart.to_dict()) so we assert the real layered
encoding, not source text. Daily stacked charts are deliberately excluded — 28+
bars would collide; they already lead with a takeaway caption.
"""

from __future__ import annotations

import pandas as pd

from app.ui import charts


def _spec(monkeypatch, **kwargs):
    captured = {}
    monkeypatch.setattr(charts.st, "altair_chart",
                        lambda chart, **_: captured.__setitem__("d", chart.to_dict()))
    monkeypatch.setattr(charts.st, "caption", lambda *a, **k: None)
    df = pd.DataFrame({
        "MONTH": ["2026-07", "2026-07", "2026-08", "2026-08"],
        "WH": ["A", "B", "A", "B"],
        "USD": [100_000.0, 50_000.0, 200_000.0, 300_000.0],
    })
    charts.monthly_stacked_usd(df, "MONTH", "WH", "USD", **kwargs)
    return captured["d"]


def _text_layer(spec):
    for layer in spec.get("layer", []):
        mark = layer.get("mark")
        mtype = mark.get("type") if isinstance(mark, dict) else mark
        if mtype == "text":
            return layer
    return None


def _layer_rows(spec, layer):
    """Altair hoists a layer's inline data to a top-level `datasets` dict and the
    layer references it by name — resolve either shape."""
    data = layer.get("data", {})
    if "values" in data:
        return data["values"]
    return spec.get("datasets", {}).get(data.get("name"), [])


def test_f44_boss_chart_layers_a_stack_total_label(monkeypatch):
    spec = _spec(monkeypatch, partial_month="2026-08")
    assert "layer" in spec and len(spec["layer"]) == 2   # bars + labels
    text = _text_layer(spec)
    assert text is not None, "no text (total-label) layer on the boss chart"
    # the house _usd_fmt (F39): compact SI at >=$10k (max total here is $500k), so
    # ~12 monthly labels don't collide AND d3's milli-suffix can't reach a total.
    assert text["encoding"]["text"].get("format") == charts._usd_fmt(500_000.0)
    assert text["encoding"]["text"]["format"] == "$,.3~s"


def test_f44_labels_use_the_per_month_TOTAL_not_per_segment(monkeypatch):
    spec = _spec(monkeypatch, partial_month="2026-08")
    text = _text_layer(spec)
    rows = _layer_rows(spec, text)
    by_month = {r["MONTH"]: r["USD"] for r in rows}
    # one row per month, each the SUM across warehouses (100k+50k, 200k+300k)
    assert by_month == {"2026-07": 150_000.0, "2026-08": 500_000.0}


def test_f44_bars_have_y_headroom_so_the_top_total_label_isnt_clipped(monkeypatch):
    # bug-hunt r2: without domainMax headroom, the tallest month's total label
    # (dy=-6 above the bar) clips off the top when that total lands on a round axis
    # tick. The bars' y-scale is padded ~1.12x the max stacked total.
    spec = _spec(monkeypatch, partial_month="2026-08")
    def _mtype(layer):
        m = layer.get("mark")
        return m.get("type") if isinstance(m, dict) else m
    bars = next(lyr for lyr in spec["layer"] if _mtype(lyr) == "bar")
    dmax = bars["encoding"]["y"]["scale"]["domainMax"]
    # max month total here is 500k (200k+300k); headroom must clear it
    assert dmax > 500_000.0 and dmax == 500_000.0 * 1.12


def test_f44_partial_month_label_dims_with_its_bar(monkeypatch):
    spec = _spec(monkeypatch, partial_month="2026-08")
    text = _text_layer(spec)
    rows = _layer_rows(spec, text)
    partial = {r["MONTH"]: r["_PARTIAL"] for r in rows}
    assert partial["2026-08"] is True and partial["2026-07"] is False
    # the label opacity is driven by the same _PARTIAL flag as the bars
    assert "condition" in text["encoding"]["opacity"]
    assert "_PARTIAL" in text["encoding"]["opacity"]["condition"]["test"]
