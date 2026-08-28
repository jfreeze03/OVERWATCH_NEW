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
    # r4: overflow REBALANCES evenly across rows (5 -> 3+2) and fills each row so no
    # card is orphaned at quarter width; the final row is st.columns(len(chunk)).
    assert "divmod(len(items), rows)" in body
    assert "st.columns(len(chunk))" in body


def test_alerts_kpis_carry_severity():
    src = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert '"severity": "bad" if crit_n else "ok"' in src
    assert '"severity": "warn" if high_n else "ok"' in src
    # rec42: bulk execute is a confirm_gate(...) call now; it still carries the
    # primary-action hierarchy (type="primary" flows through as a button kwarg).
    # F51: the bulk gate mounts under the selection nonce so an executed
    # selection can't linger and re-arm it against a shifted feed.
    assert 'key=f"alert_bulk_exec_{_sel_nonce}"' in src and 'type="primary"' in src


def test_contains_filters_use_a_count_badged_compact_popover():
    assert '_more_label = f"More · {_adv_n}" if _adv_n else "More"' in _MAIN
    assert "with st.popover(_more_label" in _MAIN
    assert "_adv_label" not in _MAIN and "expanded=_adv_on" not in _MAIN
    # v4.157.0: the "Views & display" popover was removed (owner: unused); saved
    # views / density / timezone still hydrate at startup via _apply_default_landing.
    assert 'st.popover("Views & display")' not in _MAIN


def test_scope_rides_the_status_bar():
    # rec10: the "Scope" cell restated the filter toolbar right above it (neither
    # is sticky in 1.45), so it was dropped — the bar now carries only signal not
    # already on screen (open criticals, telemetry age, MTD).
    assert '"k": "Scope"' not in _MAIN
    assert '"k": "Open criticals"' in _MAIN


def test_compact_density_mode_exists():
    assert "_COMPACT_CSS" in _THEME
    # v4.157.0: the density TOGGLE moved out with the Views popover, but the
    # pref still hydrates at startup (_apply_default_landing) and drives the CSS.
    assert '_ow_density' in _THEME and "_ow_density" in _MAIN


def test_dashboard_surfaces_hold_still():
    assert "translateY" not in _THEME                              # calm hover (r4 #11)
    assert "--ow-r:8px" in _THEME                                  # tightened radii (r4 #12)
    assert "letter-spacing:0; color:var(--ow-ink)" in _THEME       # zero heading tracking
    # the kicker's uppercase tracking is a deliberate label style — keep it
    assert "letter-spacing:0.06em" in _THEME
