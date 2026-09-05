"""v4.461 UI refactor P0 (design-system flatten) + P1 (Spend primary-metric hierarchy).

Locks the deliberate "remove the AI-dashboard chrome" decisions so a later change
cannot silently re-puff the card grammar (gradient/shadow back onto resting cards)
or regress the Cost ▸ Spend headline to an equal-weight KPI sea. These are
PRESENTATION locks only — no calculation, query, or mart is involved.
"""

from __future__ import annotations

import html as _html
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import theme
from app.ui import components

_ROOT = Path(__file__).resolve().parents[1]
_CSS = theme._TOKENS + theme._CSS


def _rule(selector: str) -> str:
    """The body of the first CSS rule for `selector` (which must end in ' {')."""
    return _CSS.split(selector, 1)[1].split("}", 1)[0]


def test_p0_card_grammar_is_flat():
    # cards/metrics/stats: solid raised surface + hairline, NO decorative gradient
    # and NO resting shadow (elevation is color+border, not chrome).
    for sel in (".ow-card {", 'div[data-testid="stMetric"] {', ".ow-stat {"):
        rule = _rule(sel)
        assert "linear-gradient" not in rule, sel
        assert "box-shadow" not in rule, sel
        assert "var(--ow-raised)" in rule, sel
    assert "box-shadow" not in _rule('[data-testid="stDataFrame"] {')
    # the severity stripe stays — a colored stripe still MEANS severity (F13)
    assert ".ow-card--bad::before" in _CSS


def test_p0_card_floor_lowered_and_single_sourced():
    assert "min-height:72px" in _rule(".ow-card {")
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    # the inline 96px override on metric_card_html is gone — the CSS floor is the
    # single source of the card height.
    assert 'style="min-height:96px"' not in comp


def test_p0_neutral_section_header_has_no_resting_gradient():
    base = _rule(".ow-section {")
    assert "linear-gradient" not in base
    assert "background:transparent" in base
    # severity tints still fill from data-derived alarm_health
    for sev in ("ok", "warn", "bad", "info"):
        assert f".ow-section--{sev}" in _CSS


def test_p0_hero_header_shrunk():
    assert "font-size:1.4rem" in _rule(".ow-page-heading h1 {")
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "icon(icon_name, 20)" in comp   # 26 -> 20


def test_p0_shadow_kept_only_for_floating_layers():
    # shadow is legitimate on genuinely floating layers (the help tooltip) — the
    # token stays defined and used there, just off resting cards.
    assert "box-shadow:var(--ow-shadow2)" in _CSS
    assert "--ow-shadow:" in _CSS   # still defined (design_system test relies on it)


def test_p1_hero_metric_renders_primary_metric_hierarchy(monkeypatch: pytest.MonkeyPatch):
    out: list[str] = []
    monkeypatch.setattr(components, "st",
                        SimpleNamespace(markdown=lambda v, **k: out.append(str(v))))
    components.hero_metric(
        {"label": "Credit spend", "value": "$142,392", "help": 'why "x"',
         "delta": "-12%", "delta_color": "inverse", "severity": "ok"},
        [{"label": "All-in", "value": "$168,204"},
         {"label": "CoCo <b>", "value": "$2,114"}],
    )
    h = out[-1]
    assert "ow-hero" in h and "ow-hero__value" in h
    assert "$142,392" in h and "$168,204" in h                 # hero + companion values
    assert "ow-hero__companions" in h and h.count("ow-hero__c-value") == 2
    assert "ow-hero--ok" in h                                  # severity reaches the value
    assert _html.escape("CoCo <b>") in h                       # companion label escaped
    assert "ow-help" in h                                      # help -> focusable badge
    # a hero with no companions renders just the main block
    out.clear()
    components.hero_metric({"label": "X", "value": "1"})
    assert "ow-hero__companions" not in out[-1]


def test_p1_spend_leads_with_hero_and_gates_capability_panels():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    # the headline is a hero (one dominant value + companions), not a flat kpi_row
    assert "hero_metric(_hero, _companions)" in src
    assert "_companions = []" in src
    # the WLA-1 credit-spend label is preserved verbatim as the hero (round-13 ally)
    assert 'f"Credit spend, {_wlab} (account)"' in src
    # the two attribution-capability meta-panels are audit-gated via the helper
    assert "def _spend_attribution_capability(" in src          # extracted helper exists
    # and it is invoked ONLY under an audit_mode() gate in the default Spend flow
    assert ("    if audit_mode():\n"
            "        _spend_attribution_capability(df, rate, ai_rate, billed_usd, _wlab)") in src
