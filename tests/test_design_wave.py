"""Locks for the DO-FIRST scannability wave (v4.103) — the high-ROI items from the
Codex visual/design review assessment (docs/reviews/DESIGN_REVIEW_2026-07-31.md).

rec14 self-identifying exports · rec2 Brief reorder · rec18 label floor ·
A1 unified traffic-light palette · rec10 clickable action surface · rec4 hoist
Top actions · rec16 readable boss chart + movers · rec20 export trend + print CSS.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# rec14 — self-identifying CSV exports
def test_rec14_export_filename_is_page_scoped():
    from app.ui.components import _slugify
    assert _slugify("Cost & Contract") == "cost-contract"
    assert _slugify("") == "table" and _slugify(None) == "table"
    comp = _src("app/ui/components.py")
    assert "_export_filename(seq, slug)" in comp          # both download buttons use it
    assert "overwatch_table_{seq}.csv" not in comp        # the generic name is gone
    assert "def styled_table(df, *, height" in comp and "slug: str | None = None" in comp


# rec2 — Brief: numbers/fires/asks before the AI narrative
def test_rec2_brief_narrative_below_asks_and_collapsed():
    b = _src("app/ui/pages/brief.py")
    assert b.index("**Asks**") < b.index("AI morning narrative")   # narrative moved down
    assert 'expanded=False' in b.split("AI morning narrative", 1)[1][:80]


# rec18 — micro-label floor raised off ~10.6-11.5px
def test_rec18_label_sizes_raised():
    t = _src("app/theme.py")
    assert ".ow-stat__k { font-size:0.72rem" in t
    assert ".ow-card__title { font-size:0.76rem" in t
    assert 'stMetricLabel"] p { font-size:0.76rem' in t


# A1 — one traffic-light palette across surfaces
def test_a1_sidebar_strip_matches_token_palette():
    m = _src("app/main.py")
    strip = m.split("_STRIP_COLORS = {", 1)[1].split("}", 1)[0]
    assert '"OK": "#34d399"' in strip and '"BAD": "#fb7185"' in strip and '"WARN": "#fbbf24"' in strip
    assert "#22c55e" not in strip and "#ef4444" not in strip and "#f59e0b" not in strip


def test_a1_charts_read_shared_severity_palette():
    ch = _src("app/ui/charts.py")
    # the events-by-day scale + the budget line read SEV_COLORS, not divergent hexes
    assert "SEV_COLORS[\"CRITICAL\"], SEV_COLORS[\"HIGH\"]" in ch
    assert 'color=SEV_COLORS["CRITICAL"]' in ch
    assert "#ef4444" not in ch and "#f97316" not in ch and "#f87171" not in ch


# rec16 — readable boss chart + a movers table
def test_rec16_boss_chart_defaults_and_movers():
    from app.ui.charts import monthly_stacked_usd
    # default top_n dropped 8 -> 5 (<= 6 colors)
    assert monthly_stacked_usd.__defaults__[-1] == 5
    ov = _src("app/ui/pages/overview.py")
    assert "Top movers —" in ov and 'slug="warehouse-movers"' in ov
    assert '"DELTA_USD"' in ov and '"DELTA_PCT"' in ov


# rec4 + rec10 — Top actions above the charts, and clickable
def test_rec4_top_actions_above_boss_chart():
    ov = _src("app/ui/pages/overview.py")
    assert ov.index('section_header("Top actions")') < ov.index('section_header("Monthly spend by warehouse")')


def test_rec10_top_actions_are_clickable():
    ov = _src("app/ui/pages/overview.py")
    assert 'selectable_table(' in ov and 'key="ov_actions_sel"' in ov
    assert 'request_navigation("Control Room")' in ov


# rec20 — the exec export carries a trend sparkline + a print stylesheet
def test_rec20_export_trend_and_print_css():
    from app.logic.formulas import exec_summary_html
    kw = {"company": "ALFA", "days": 30, "generated": "now", "window_spend": "$1",
          "mtd_line": "$5", "forecast_line": "$4", "alerts_line": "0",
          "score_line": "100/100 (Healthy)", "drivers": [], "actions": []}
    with_trend = exec_summary_html(**kw, spend_series=[10.0, 12.0, 9.0, 15.0, 11.0])
    assert "<polyline" in with_trend and "@page" in with_trend and "Spend trend" in with_trend
    # no series -> no sparkline, but the print stylesheet is still present
    without = exec_summary_html(**kw, spend_series=None)
    assert "<polyline" not in without and "@page" in without
    assert without.startswith("<!DOCTYPE html>") and "<script" not in without.lower()
