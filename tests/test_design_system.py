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


def test_no_emoji_page_icons_remain():
    """CoCo high item: emoji page icons replaced by SVG."""
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    for emoji in ("☀️", "📊", "🎛️", "💰", "🔧", "🚨", "🔐", "⚙️"):
        assert emoji not in main, f"emoji {emoji} still in nav"
    assert "_persistent_status_bar" in main       # status bar on every page
    assert "last_refreshed_note" in main           # global freshness indicator


def test_cost_consolidated_to_four_sections():
    from pathlib import Path
    cost = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert '"Spend & Attribution", "Contract & Forecast", "Chargeback & AI"' in cost
    # all eight original sub-tabs still called (nothing lost in the merge)
    for fn in ("_spend_tab", "_attribution_tab", "_contract_tab", "_chargeback_tab",
               "_cortex_storage_tab", "_ai_users_tab", "_optimization_tab", "_savings_tab"):
        assert fn + "(" in cost, fn


def test_pages_carry_svg_icons():
    from pathlib import Path
    pdir = Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"
    for pg, ico in [("overview", "overview"), ("cost", "cost"), ("alerts", "alerts"),
                    ("brief", "brief"), ("admin", "admin")]:
        src = (pdir / f"{pg}.py").read_text(encoding="utf-8")
        assert f'icon_name="{ico}"' in src, pg


def test_daily_count_bars_uses_time_axis():
    """The DDL-changes chart must plot Day as time, not epoch-millis labels."""
    import inspect

    from app.ui import charts
    src = inspect.getsource(charts.daily_count_bars)
    assert '"Day:T"' in src and "pd.to_datetime" in src


def test_role_based_user_scope_everywhere():
    """No user-grained builder should still scope by TRXS_ name prefix."""
    from app.data import cortex_sql, insights_sql, security_sql
    for sql in (cortex_sql.cortex_code_user_rollup(30, "ALFA"),
                security_sql.failed_login_reasons(7, "Trexis"),
                insights_sql.dormant_users(90, "ALFA")):
        assert "COMPANY_FOR_USER" in sql


def test_stale_elements_hidden_for_crisp_section_switch():
    from app import theme
    assert '[data-stale="true"]' in theme._CSS   # no bleed between lazy sections

def test_triage_scope_chips_and_reset():
    """v4.39: the active scope reads as chips, the strip glows when any
    non-default filter is live, and one click resets — scoped numbers must
    never pass as account-wide."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    theme = (root / "app" / "theme.py").read_text(encoding="utf-8")
    for cls in (".ow-scope-chips{", ".ow-chip{", ".ow-chip-accent{",
                ".ow-chip-warn{", ".ow-chip-dot{"):
        assert cls in theme, cls
    assert ':has(.ow-scope-active)' in theme               # the filtered glow
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "_scope_chips" in main and "_reset_scope" in main
    assert '_html.escape(str(value))' in main              # user text escaped
    assert 'kind="warn"' in main                           # contains-filters read hotter
    assert 'st.button("Reset", key="flt_reset", on_click=_reset_scope' in main
    assert "Account-wide" in main                          # honest default chip

