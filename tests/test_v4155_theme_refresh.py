"""Locks for the v4.155 owner re-theme + section-display pass (2026-08-14).

Owner: "I do not like the color scheme and some of the sections need to be
displayed better." What shipped, pinned here so it survives:

1. Chrome is warm graphite, not the navy void — the old #0a0f1c/#0f1729/#131d33
   family is retired from every chrome surface (theme, charts, config.toml).
2. The brand ACCENT is its own (iris) hue, no longer conflated with the INFO
   severity sky — "informational" and "interactive" stopped sharing one blue.
3. Severity semantics are UNCHANGED (ok/warn/bad/high/info) — traffic-light
   meaning is adjudicated (A1/A2, rec50); only the chrome moved.
4. Primary-button dark ink keeps >= 4.5:1 on the accent it sits on (computed,
   not asserted-by-hex, so a future accent change re-proves it).
5. Section headers are real dividers: larger title, top-margin rhythm, hairline
   underline; neutral sections dropped the gray wash (severity tints stay).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.ui import palette

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _wcag_ratio(fg: str, bg: str) -> float:
    def _lin(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    def _lum(hexs: str) -> float:
        r, g, b = (int(hexs[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    la, lb = _lum(fg), _lum(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# The retired navy chrome. #38bdf8 is NOT here — it lives on as the INFO
# severity hue; what died is its double duty as the brand accent.
_RETIRED_CHROME = ("#0a0f1c", "#0f1729", "#131d33")


def test_navy_chrome_is_retired_everywhere():
    for rel in ("app/theme.py", "app/ui/palette.py", "app/ui/charts.py",
                ".streamlit/config.toml"):
        text = _src(rel).lower()
        for hexv in _RETIRED_CHROME:
            assert hexv not in text, f"{rel} still carries retired chrome {hexv}"


def test_accent_is_not_the_info_severity():
    """Pre-v4.155 ACCENT == INFO (#38bdf8), which painted every interactive
    affordance in the informational-severity blue. They must stay distinct."""
    assert palette.ACCENT.lower() != palette.INFO.lower()
    assert palette.ACCENT2.lower() != palette.INFO.lower()


def test_severity_hues_unchanged_by_the_retheme():
    """The owner complaint was the CHROME, not the traffic lights — severity
    meaning (A1/A2, rec50) must not drift under a re-theme."""
    assert palette.OK == "#34d399"
    assert palette.WARN == "#fbbf24"
    assert palette.BAD == "#fb7185"
    assert palette.INFO == "#38bdf8"
    assert palette.HIGH == "#fb923c"


def test_primary_button_ink_contrast_is_computed_not_hoped():
    """The forced dark ink (test_live_round6 pins the hex) must actually clear
    WCAG AA on BOTH ends of the accent gradient it sits on."""
    theme = _src("app/theme.py")
    m = re.search(r"color:(#[0-9a-fA-F]{6}) !important; border:none", theme)
    assert m, "primary-button forced ink not found"
    ink = m.group(1)
    assert _wcag_ratio(ink, palette.ACCENT) >= 4.5
    assert _wcag_ratio(ink, palette.ACCENT2) >= 4.5


def test_section_headers_are_dividers_with_rhythm():
    theme = _src("app/theme.py")
    rule = theme.split(".ow-section {", 1)[1].split("}", 1)[0]
    assert "margin:20px 0 10px" in rule                 # rhythm between panels
    assert "border-bottom:1px solid var(--ow-hairline)" in rule  # underline
    assert "rgba(171,168,182,0.06)" not in rule         # neutral wash dropped
    assert "background:" not in rule                    # ...entirely
    # severity sections keep their tint — color still equals signal (law 8)
    for sev in ("ok", "warn", "bad", "info"):
        assert f".ow-section--{sev} {{ border-left-color:var(--ow-{sev}); background:" in theme
    assert "font-size:1.14rem" in theme.split(".ow-section__title", 1)[1].split("}", 1)[0]


def test_chip_definition_is_consolidated():
    """Two historical .ow-chip blocks silently overrode each other; v4.155
    merged them. Exactly one definition, keeping all three state variants."""
    from app import theme
    css = theme._TOKENS + theme._CSS
    assert css.count(".ow-chip{") == 1
    for variant in (".ow-chip-ok", ".ow-chip-bad", ".ow-chip-warn"):
        assert variant in css, variant


def test_dag_viewer_follows_the_new_chrome():
    charts = _src("app/ui/charts.py")
    assert "background: #131215" in charts              # host = page bg
    assert "outline: 2px solid #8f8aff" in charts       # focus = accent
    assert 'stroke="#0a0f1c"' not in charts             # point strokes read palette.BG
    assert "stroke=palette.BG" in charts
