"""Design-system contracts: theme tokens, icons, cards, sparklines."""

import html as _html

from app.theme import chip, inject_theme  # noqa: F401 (import-safe)
from app.ui import icons
from app.ui.components import (
    metric_card_html,
    spark_svg,
    status_bar,  # noqa: F401
)


def test_theme_has_token_layer_and_card_system():
    from app import theme

    css = theme._TOKENS + theme._CSS
    for token in ("--ow-accent", "--ow-bad", "--ow-warn", "--ow-ok", "--ow-shadow",
                  "--ow-font", "--ow-r", "--ow-ease"):
        assert token in css, token
    for cls in (".ow-card", ".ow-card--bad", ".ow-section--warn", ".ow-statusbar",
                ".ow-stat", "div[data-testid=\"stMetric\"]"):
        assert cls in css, cls
    assert "@media (max-width:640px)" in css        # responsive
    assert "prefers-reduced-motion" in css          # accessible


def test_icons_are_svg_currentcolor_no_emoji():
    for name in ("brief", "cost", "alerts", "security", "up", "down"):
        svg = icons.icon(name)
        assert svg.startswith("<svg") and "currentColor" in svg
    # every page maps to a real icon, and none of them is an emoji glyph
    for page in ("Brief", "Cost & Contract", "Alerts", "Admin"):
        assert icons.page_icon(page).startswith("<svg")
    assert icons.icon("nonexistent-name").startswith("<svg")   # safe fallback


def test_spark_svg_normalizes_and_guards():
    s = spark_svg([1, 5, 2, 8, 4])
    assert "polyline" in s and "polygon" in s      # line + area fill
    assert spark_svg([1]) == "" and spark_svg([]) == ""


def test_metric_card_escapes_and_severity_and_trend():
    card = metric_card_html({"label": "MTD <spend>", "value": "$1,234",
                             "delta": "-3%", "delta_color": "inverse",
                             "severity": "bad", "spark": [1, 2, 3],
                             "help": 'tip "x"'})
    assert "ow-card--bad" in card
    assert _html.escape("MTD <spend>") in card and "<spend>" not in card
    assert "polyline" in card                       # sparkline embedded
    # inverse + negative delta = good = green
    assert "#34d399" in card
    assert 'title=' in card                         # help became a tooltip
