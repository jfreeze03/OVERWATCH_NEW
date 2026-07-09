"""Locks for the v4.9.1 visual pass (Codex round 4, Streamlit-reality-checked).

Pins: the gradient actually fades, KPI rows wrap at four, alerts KPIs carry
severity, the contains-filters collapse (and auto-open when active), compact
density exists, hover motion is gone, and the budget line labels itself.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_THEME = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
_CHARTS = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
_MAIN = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_spend_trend_bars_carry_the_gradient():
    # v4.11: the area wash is gone (owner, twice) — bars + 7d average now.
    # The vertical accent gradient must still actually fade (r4 #16 spirit).
    seg = _CHARTS.split("def spend_trend", 1)[1].split("def bar_usd", 1)[0]
    assert seg.count("offset=0.0") == 1 and "offset=1.0" in seg
    assert "mark_bar" in seg and "mark_area" not in seg


def test_budget_rule_is_labeled_without_hover():
    seg = _CHARTS.split("def spend_trend", 1)[1].split("def bar_usd", 1)[0]
    assert "mark_text" in seg and "budget $" in seg


def test_kpi_rows_wrap_at_four():
    src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = src.split("def kpi_row", 1)[1].split("\ndef ", 1)[0]
    assert "min(columns or 4, 4" in body
    assert "range(0, len(items), width)" in body                   # overflow wraps


def test_alerts_kpis_carry_severity():
    src = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert '"severity": "bad" if crit_n else "ok"' in src
    assert '"severity": "warn" if high_n else "ok"' in src
    assert 'key="alert_bulk_exec", type="primary"' in src          # action hierarchy


def test_contains_filters_collapse_but_never_hide_active_ones():
    assert "More filters — warehouse / user / schema contains" in _MAIN
    assert "expanded=_adv_on" in _MAIN                             # auto-open when active
    assert 'st.popover("Views")' in _MAIN and "💾" not in _MAIN     # emoji retired


def test_scope_rides_the_status_bar():
    # 1.45 has no sticky positioning; orientation-while-scrolling comes from
    # the persistent status bar instead.
    assert '"k": "Scope"' in _MAIN


def test_compact_density_mode_exists():
    assert "_COMPACT_CSS" in _THEME
    assert '_ow_density' in _THEME and "_ow_density" in _MAIN      # toggle wired


def test_dashboard_surfaces_hold_still():
    assert "translateY" not in _THEME                              # calm hover (r4 #11)
    assert "--ow-r:8px" in _THEME                                  # tightened radii (r4 #12)
    assert "letter-spacing:0; color:var(--ow-ink)" in _THEME       # zero heading tracking
    # the kicker's uppercase tracking is a deliberate label style — keep it
    assert "letter-spacing:0.06em" in _THEME
