"""UI/UX master list — Wave 1 visual/severity/a11y batch (W1a).

Locks: F13 neutral resting KPI stripe · C22 no hover on non-interactive cards ·
C28 24px help target · F21 right-anchored/clamped tooltip · F14 shared
focus-visible grammar · C4 connection-bound brand dot · F22 wordmark solid
fallback · F19 section severity reaches icon+badge · F16 one severity hue map ·
F29 decorative sparklines aria-hidden.
"""

from __future__ import annotations

from pathlib import Path

from app.theme import _CSS
from app.ui import palette, status_colors
from app.ui.components import spark_svg

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_f13_resting_kpi_stripe_is_neutral_severity_keeps_hue():
    # the DEFAULT stripe must not be the accent gradient (it looked semantic on
    # every card); severity modifiers still tint it.
    stripe = _CSS.split('div[data-testid="stMetric"]::before', 1)[1].split("}", 1)[0]
    assert "var(--ow-ink-mute)" in stripe
    assert "ow-accent" not in stripe
    assert '.ow-sev-bad div[data-testid="stMetric"]::before' in _CSS  # severity still tints


def test_f13_one_kpi_value_size_on_both_surfaces():
    assert "font-size:1.55rem; font-weight:720" in _CSS   # stMetric == metric_card_html
    assert "font-size:1.62rem" not in _CSS


def test_c22_no_hover_elevation_on_noninteractive_cards():
    assert 'div[data-testid="stMetric"]:hover' not in _CSS
    assert ".ow-card:hover" not in _CSS
    # interactive surfaces keep hover
    assert ".stButton > button:hover" in _CSS


def test_c28_help_target_is_24px():
    help_rule = _CSS.split(".ow-help {", 1)[1].split("}", 1)[0]
    assert "width:24px" in help_rule and "height:24px" in help_rule
    assert "margin:-4px 0" in help_rule   # larger target must not grow the title row


def test_f21_tooltip_right_anchored_and_viewport_clamped():
    tip = _CSS.split(".ow-help[data-help]::after", 1)[1].split("}", 1)[0]
    assert "right:-4px" in tip and "left:auto" in tip
    assert "max-width:min(300px, 74vw)" in tip


def test_f14_focus_visible_grammar_covers_the_controls():
    assert ".stButton > button:focus-visible" in _CSS
    assert 'div[data-baseweb="select"]:focus-within' in _CSS
    assert 'div[role="radiogroup"] label:has(input:focus-visible)' in _CSS
    assert ".stTextInput input:focus-visible" in _CSS


def test_c4_brand_dot_binds_to_connection():
    assert ".ow-brand-dot--off" in _CSS                     # the disconnected variant
    off = _CSS.split(".ow-brand-dot--off", 1)[1].split("}", 1)[0]
    assert "animation:none" in off
    # main.py binds the class to the live connection
    assert '"ow-brand-dot" if connected else "ow-brand-dot ow-brand-dot--off"' in _MAIN


def test_f22_wordmark_is_solid_ink():
    # v4.461 P3: the gradient text-clip (an AI-startup flourish) was retired for the
    # operator aesthetic — the wordmark is now unconditionally solid ink, and the
    # background-clip:text / text-fill-color machinery is gone from the CSS entirely.
    word = _CSS.split(".ow-brand-word", 1)[1].split("}", 1)[0]
    assert "color:var(--ow-ink)" in word                    # solid ink, renders everywhere
    assert "text-fill-color" not in _CSS                    # no gradient clip anywhere
    assert "background-clip:text" not in _CSS


def test_f19_section_severity_reaches_icon_and_badge():
    for sev in ("ok", "warn", "bad", "info"):
        assert f".ow-section--{sev} .ow-section__icon" in _CSS, sev
        assert f".ow-section--{sev} .ow-section__badge" in _CSS, sev


def test_f16_tables_and_charts_share_severity_hues():
    # one hue per severity name: the chart map must agree with the table map on
    # every shared severity key (fg hue vs cell fg differ; compare via semantics).
    # INFO is the one that diverged (grey table cell, blue chart series).
    assert palette.SEVERITY_HUES["INFO"] == palette.LOW
    # and the table maps LOW and INFO to the same (muted) pair
    assert status_colors.STATUS_COLOR_MAP["INFO"] == status_colors.STATUS_COLOR_MAP["LOW"]


def test_f29_sparklines_are_decorative_for_assistive_tech():
    svg = spark_svg([1.0, 2.0, 3.0])
    assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg


# ---- F1: nav label == page H1 ------------------------------------------------

def test_nav_labels_match_page_titles():
    # the sidebar label is the "you are here" anchor — the landing H1 must read
    # identically (Ask's H1 stays "Ask OVERWATCH": its nav GROUP carries that name).
    root = Path(__file__).resolve().parents[1]
    brief = (root / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    security = (root / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert 'page_header("Brief",' in brief
    assert 'page_header("Morning brief"' not in brief
    assert 'page_header("Security",' in security
    assert 'page_header("Security & Governance"' not in security
