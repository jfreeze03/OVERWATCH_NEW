"""rec50: palette.py is the single source of truth for OVERWATCH's colors.

These tests fail CI if any of the three color surfaces drift apart:
  1. app/ui/palette.py  (imported by main.py, components.py, charts.py, status_colors.py)
  2. app/theme.py       (the --ow-* CSS design tokens)
  3. .streamlit/config.toml  (native-widget chrome)

Before rec50 these were hand-aligned and silently diverged (config.toml carried
#0b1220/#111a2c/#e2e8f0 vs the tokens). Now a divergence is a red build.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from app.ui import palette

_ROOT = Path(__file__).resolve().parents[1]


def _theme_tokens() -> dict[str, str]:
    """Parse the --ow-<name>:#hex custom properties out of app/theme.py."""
    css = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
    return {name.lower(): hexv.lower()
            for name, hexv in re.findall(r"--ow-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css)}


def test_palette_matches_theme_tokens():
    """Every palette constant that has a --ow-* token must equal it."""
    tok = _theme_tokens()
    # (palette attr, theme token name)
    pairs = [
        ("OK", "ok"), ("WARN", "warn"), ("BAD", "bad"), ("INFO", "info"),
        ("ACCENT", "accent"), ("ACCENT2", "accent2"),
        ("BG", "bg"), ("SURFACE", "surface"), ("RAISED", "raised"),
        ("INK", "ink"), ("INK_SOFT", "ink-soft"), ("INK_MUTE", "ink-mute"),
    ]
    for attr, token in pairs:
        assert token in tok, f"--ow-{token} not found in theme.py"
        assert getattr(palette, attr).lower() == tok[token], (
            f"palette.{attr}={getattr(palette, attr)} != --ow-{token}={tok[token]} "
            "— align palette.py and theme.py")


# Consumer files must REFERENCE palette for the pure severity hues, not re-hardcode
# them (rec50). These four have no other legitimate use in the app, so any literal
# copy in a consumer is drift the palette-vs-theme test alone would miss (the review
# found several such residuals). Accent/slate hues are excluded — they legitimately
# recur in gradients (charts heatmap ramp) and light-theme lookup keys.
_SEVERITY_HUES = ("#34d399", "#fbbf24", "#ef5350", "#fb923c")
_CONSUMERS = ("app/main.py", "app/ui/components.py", "app/ui/charts.py",
              "app/ui/status_colors.py")


def test_consumers_reference_palette_not_literal_severity_hues():
    offenders = []
    for rel in _CONSUMERS:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        offenders.extend(f"{rel} still hardcodes {hue}"
                         for hue in _SEVERITY_HUES if hue in text)
    assert not offenders, (
        "severity hues must come from app/ui/palette.py, not literals: "
        + "; ".join(offenders))


def test_config_toml_matches_palette():
    """The native-widget theme in config.toml must mirror the palette chrome."""
    cfg = tomllib.loads((_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    theme = cfg.get("theme", {})
    expect = {
        "primaryColor": palette.ACCENT,
        "backgroundColor": palette.BG,
        "secondaryBackgroundColor": palette.SURFACE,
        "textColor": palette.INK,
    }
    for key, want in expect.items():
        assert str(theme.get(key, "")).lower() == want.lower(), (
            f"config.toml [theme].{key}={theme.get(key)} != palette {want} "
            "— align config.toml to palette.py")
