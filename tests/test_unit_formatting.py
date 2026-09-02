"""The single canonical unit -> formatter — root-cause guard for the recurring
cross-surface number-format drift class (a KPI tile hand-formatting "0.03 TB"
beside a table's humanized "30.7 GB": FC-1 / CSF-1 / CD-1).

format_unit() is the one place a raw scalar becomes a display string. These tests
make the metric_registry `unit` field load-bearing (every unit has a renderer),
prove the card + chart surfaces route through it, and forbid the specific raw
byte-literal card value that caused the class.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.logic import metric_registry as mr
from app.logic.formulas import format_credits, format_unit, format_usd, humanize_bytes, humanize_gb
from app.ui import charts

_UI = Path(__file__).resolve().parents[1] / "app" / "ui"


# --- every registry unit has a canonical renderer (makes the registry load-bearing) ----
def test_every_registry_unit_has_a_canonical_renderer():
    for u in mr.UNITS:
        out = format_unit(1234.5, u)
        assert out and out != "—", f"registry unit {u!r} has no format_unit renderer"


# --- format_unit matches the surface conventions it is meant to unify -------------------
def test_format_unit_matches_surface_conventions():
    # USD == format_usd (the same helper the KPI cards and _render_table USD columns use)
    assert format_unit(1_234_567, "usd") == format_usd(1_234_567)
    assert format_unit(950, "usd") == format_usd(950)
    # credits == format_credits
    assert format_unit(1500, "credits") == format_credits(1500)
    # bytes / gb / tb humanize like the table's _byte_unit_for_column path
    assert format_unit(3 * 1024 ** 3, "bytes") == humanize_bytes(3 * 1024 ** 3)
    assert format_unit(0.03, "gb") == humanize_gb(0.03)                    # "30.7 MB"
    assert format_unit(0.03, "tb") == humanize_bytes(0.03 * 1024 ** 4)     # "30.7 GB"
    # the sub-unit magnitude that used to read "0.03" now reads its real size
    assert format_unit(0.03, "tb") != "0.03" and "GB" in format_unit(0.03, "tb")
    # percent carries its unit
    assert format_unit(93.1, "percent") == "93.1%"
    # count / days are INTEGERS like the table's _COUNT_SUFFIXES — a small count must read
    # "5", never "5.0" (the <100 adaptive-fallback bug the count branch fixes)
    assert format_unit(5, "count") == "5"
    assert format_unit(42, "n") == "42"
    assert format_unit(0, "count") == "0"
    assert format_unit(7, "days") == "7"
    # aliases: registry + chart + table spellings all resolve
    assert format_unit(5, "USD") == format_unit(5, "$") == format_unit(5, "usd")
    assert format_unit(50, "pct") == format_unit(50, "percent")


# --- degenerate input renders an em-dash, never a misleading zero-magnitude ------------
def test_format_unit_degenerate_is_em_dash():
    assert format_unit(None, "usd") == "—"
    assert format_unit(float("nan"), "tb") == "—"
    assert format_unit(float("inf"), "bytes") == "—"


# --- the KPI card API routes a (value, unit) through the canonical formatter -----------
def test_card_unit_renders_through_canonical_formatter():
    from app.ui.components import metric_card_html
    out = metric_card_html({"label": "Growth", "value": 0.03, "unit": "tb"})
    assert "30.7 GB" in out and "0.03" not in out
    # a pre-formatted string value (no unit) is rendered verbatim, unchanged
    assert "$1.20M" in metric_card_html({"label": "X", "value": "$1.20M"})
    # exception_summary honors unit= too
    from app.ui import components

    captured: list[str] = []
    _real = components.st
    try:
        class _Fake:
            def markdown(self, v, **_):
                captured.append(str(v))
        components.st = _Fake()
        components.exception_summary([{"label": "Spill", "value": 0.03, "unit": "gb"}], "clean")
    finally:
        components.st = _real
    assert "30.7 MB" in captured[-1]


# --- N7: the chart formatter no longer drops a registry-spelled unit to a bare number --
def test_charts_formatter_no_longer_drops_registry_units():
    assert charts._fmt_metric_value(93.1, "percent") == "93.1%"
    assert charts._fmt_metric_value(3 * 1024 ** 3, "bytes") == humanize_bytes(3 * 1024 ** 3)
    # the existing lowercase fast-path vocabulary is unchanged
    assert charts._fmt_metric_value(742389, "usd") == "$742,389"
    assert charts._fmt_metric_value(93.1, "pct") == "93.1%"
    # case-insensitive: an uppercase registry spelling hits the same path
    assert charts._fmt_metric_value(742389, "USD") == charts._fmt_metric_value(742389, "usd")


# --- the class-ending guard: no KPI card value may hand-build a raw byte-unit literal --
def test_no_kpi_card_value_carries_a_raw_byte_unit_literal():
    pat = re.compile(r'"value":\s*f"[^"]*\}[^"]*\s(?:TB|GB|MB|KB)"')
    hits = []
    for p in _UI.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p.relative_to(_UI)}:{i}")
    assert not hits, (
        "A KPI card 'value' hand-builds a raw byte-unit string (reads '0.03 TB' beside a "
        "table's '30.7 GB'). Pass a raw value + unit='gb'|'tb'|'bytes' instead: " + "; ".join(hits))
