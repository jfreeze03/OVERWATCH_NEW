"""UI/UX master list — Wave 2 chart-unit cluster (F41 + F48) on daily_metric_line.

* F41 — the line's tooltip and peak caption now carry the metric's unit. A $ line
  showed "742389.5" (no $, no separators) and a % line "93.1" (no %). A `unit`
  argument spells the tooltip (a formatted string column), the peak caption, and
  the y-axis; omitting it keeps the legacy bare number.

* F48 — the optional reference rule is now labeled ON the chart (a text mark),
  instead of a bare dashed vertical explained only by a caption underneath.

The specs are compiled headlessly (chart.to_dict()) so we assert real encoding,
not just source text.
"""

from __future__ import annotations

import json

import pandas as pd

from app.ui import charts


def _render(monkeypatch, **kwargs):
    """Call daily_metric_line, capturing the compiled Vega spec + any captions."""
    captured = {}
    caps: list[str] = []
    monkeypatch.setattr(charts.st, "altair_chart",
                        lambda chart, **_: captured.__setitem__("spec", chart.to_dict()))
    monkeypatch.setattr(charts.st, "caption", lambda t, **_: caps.append(str(t)))
    df = pd.DataFrame({
        "DAY": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03"]),
        "VALUE": [742389.5, 10.0, 500000.0],
    })
    charts.daily_metric_line(df, "DAY", "VALUE", "metric", **kwargs)
    return json.dumps(captured.get("spec", {})), caps


def test_f41_unit_formatter_spells_each_unit():
    f = charts._fmt_metric_value
    assert f(742389.5, "usd") == "$742,390"        # $ + separators, 0 dp
    assert f(93.14, "pct") == "93.1%"              # % suffix, 1 dp
    assert f(12.34, "sec") == "12.3s"
    assert f(1234, "credits") == "1,234 cr"
    assert f(1500, "count") == "1,500"
    # unknown unit -> legacy adaptive precision (unchanged for unit-less callers)
    assert f(742389.5, "") == "742,390"
    assert f(9.9, "") == "9.9"
    # negative dollars: the sign LEADS the unit prefix ("-$1,234", not "$-1,234")
    assert f(-1234, "usd") == "-$1,234"
    assert f(-2.5, "pct") == "-2.5%"
    # non-numeric survives as its string; missing/degenerate values read as em-dash
    assert f("n/a", "usd") == "n/a"
    assert f(float("nan"), "usd") == "—"
    assert f(float("inf"), "usd") == "—"
    assert f(float("-inf"), "count") == "—"
    assert f(None, "pct") == "None"      # None isn't float()-able -> str(None)


def test_f41_usd_tooltip_and_axis_and_peak_carry_the_dollar(monkeypatch):
    spec, caps = _render(monkeypatch, unit="usd")
    # tooltip reads a formatted _Label string field, not the bare Q value
    assert '"_Label"' in spec
    # the y-axis is dollar-formatted
    assert '"$,.0f"' in spec
    # the peak caption names the peak WITH its unit ($742,390 is the max row)
    assert any("$742,390" in c for c in caps), caps


def test_f41_percent_tooltip_has_no_dollar_axis_but_peak_has_percent(monkeypatch):
    spec, caps = _render(monkeypatch, unit="pct")
    assert '"_Label"' in spec
    assert '"$,.0f"' not in spec                    # pct keeps a bare axis
    assert any(c.endswith("%.") or "%" in c for c in caps), caps


def test_f41_no_unit_keeps_the_legacy_bare_value_tooltip(monkeypatch):
    spec, _ = _render(monkeypatch)                  # no unit
    # the generic path must NOT introduce a _Label field (byte-compatible tooltip)
    assert '"_Label"' not in spec
    assert '"Value"' in spec


def test_f48_rule_label_renders_a_text_mark_on_the_chart(monkeypatch):
    spec, _ = _render(monkeypatch, unit="sec",
                      rule_date=pd.Timestamp("2026-08-02"), rule_label="registered change")
    # the label text is present as a mark on the layered chart
    assert "registered change" in spec
    assert '"text"' in spec          # a text mark layer exists


def test_f48_rule_without_label_still_renders_just_the_rule(monkeypatch):
    spec, _ = _render(monkeypatch, unit="sec", rule_date=pd.Timestamp("2026-08-02"))
    # a rule (strokeDash) is drawn, but no stray label text is invented
    assert "registered change" not in spec


def test_f48_nat_rule_date_renders_no_rule_and_does_not_crash(monkeypatch):
    # operations passes a change-date that can be NaT; NaT is not None, so without
    # the pd.notna guard it would draw a rule + label at an invalid x-position.
    spec, _ = _render(monkeypatch, unit="sec",
                      rule_date=pd.NaT, rule_label="registered change")
    assert "registered change" not in spec    # no label at a NaT position
    # a plain non-empty line still rendered (the metric line itself)
    assert '"_Label"' in spec
