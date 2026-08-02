"""Locks for the NEXT scannability wave (v4.104) — Codex visual-review NEXT + added.

A3 color the Δ columns · A2 colorblind-safe severity (redundant shape) · rec11 lag
note once per page · rec1 de-duped in-header scope · rec5 no per-page kicker · rec13
human table headers · rec6 Control Room heading consistency · A5 one dollar-axis
spelling · rec15 a cost-driver takeaway.  (rec7 sidebar radio-dot deferred — DOM-fragile.)
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# A3 — movement/delta columns are sign-colored
def test_a3_delta_detection_and_color():
    from app.ui.status_colors import delta_css, is_delta_column
    assert is_delta_column("DELTA_USD") and is_delta_column("D_CALLS") and is_delta_column("Δ %")
    assert not is_delta_column("CHANGE_SEEN_AT")   # a timestamp is not a movement
    assert not is_delta_column("WAREHOUSE_NAME")
    up, down = delta_css(40000), delta_css(-40000)
    assert "color:" in up and "color:" in down and up != down     # +$ and -$ differ by hue, not just a sign
    assert delta_css(0) == "" and delta_css("n/a") == ""


def test_a3_wired_into_table_renderer():
    c = _src("app/ui/components.py")
    assert "if is_delta_column(col):" in c and "delta_css(v)" in c


# A2 — the event-timeline dots carry a redundant shape per severity
def test_a2_event_timeline_redundant_shape():
    ch = _src("app/ui/charts.py")
    block = ch.split("def event_timeline", 1)[1].split("\ndef ", 1)[0]
    assert 'alt.Shape("SEVERITY:N"' in block and "mark_point" in block
    assert "triangle-up" in block and "diamond" in block   # distinct shapes, not all circles


# rec11 — the lag note is once per page, not on every panel caption
def test_rec11_lag_note_once_per_page():
    c = _src("app/ui/components.py")
    rc = c.split("def result_caption", 1)[1].split("\ndef ", 1)[0]
    assert "ACCOUNT_USAGE_LAG_NOTE" not in rc                 # dropped from the per-panel caption
    ph = c.split("def page_header", 1)[1].split("\ndef ", 1)[0]
    assert "st.caption(ACCOUNT_USAGE_LAG_NOTE)" in ph         # shown once in the header


# rec1 + rec5 — de-duped in-header scope, no per-page kicker
def test_rec1_rec5_header_decluttered():
    c = _src("app/ui/components.py")
    ph = c.split("def page_header", 1)[1].split("\ndef ", 1)[0]
    assert 'class="ow-kicker">OVERWATCH' not in ph            # rec5: kicker div gone
    assert "subtitle if (chips or not scope_note)" in ph      # rec1: scope not doubled when chips render


# rec13 — human-readable table headers (display only)
def test_rec13_prettify_header():
    from app.ui.components import _prettify_header
    assert _prettify_header("WAREHOUSE_NAME") == "Warehouse Name"
    assert _prettify_header("CREDITS_USD") == "Credits USD"    # USD token preserved
    assert _prettify_header("USD") == "USD"                    # short all-caps left alone
    assert _prettify_header("Already Human") == "Already Human"
    comp = _src("app/ui/components.py")
    assert "st.column_config.Column(_label, help=_help)" in comp          # relabel-only path (+ rec32 help)
    # r5-bug: the pin and the pretty label go into the SAME Column so a wide table's
    # first column keeps both (the old code let the prettifier defeat _auto_pin).
    assert "st.column_config.Column(_label, pinned=True, help=_help)" in comp
    assert "def _auto_pin" not in comp                                    # inlined away


# rec6 — Control Room top-level sections use section_header
def test_rec6_control_room_headings():
    cr = _src("app/ui/pages/control_room.py")
    for title in ("Triage queue", "Incidents", "Telemetry freshness", "Spend movers (window vs prior)"):
        assert f'section_header("{title}")' in cr
    assert cr.count("st.subheader(") <= 1                     # only the one sub-panel remains


# A5 — one dollar-axis spelling
def test_a5_axis_title_consistent():
    ch = _src("app/ui/charts.py")
    assert 'title="USD", stack="zero"' not in ch              # the bare "USD" boss-chart axis is gone
    assert 'title="Spend (USD)"' in ch


# rec15 — the cost-drivers panel leads with its conclusion
def test_rec15_cost_driver_takeaway():
    ov = _src("app/ui/pages/overview.py")
    assert "Top driver:" in ov
    # C5: the denominator was described as "tracked drivers", which readers took
    # for total spend. The panel only covers warehouse compute — serverless and
    # AI/Cortex bill on meters it never reads — so the share must name its pie.
    # V069 (2026-07-31): serverless & AI moved to their own COST_DRIVER_SVC panel, so
    # the warehouse denominator is named "% of warehouse" and the trailing clause now says
    # they are SHOWN separately below (a distinct rendered panel), not merely that they
    # "bill separately". The C5 intent — name the pie as warehouse-only — is unchanged.
    assert "% of warehouse " in ov and "serverless & AI shown separately below" in ov
    assert "% of tracked drivers" not in ov
