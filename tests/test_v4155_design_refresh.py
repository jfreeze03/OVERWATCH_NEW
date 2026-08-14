"""Locks for the v4.155 design refresh (owner review 2026-08-14: "I do not like
the color scheme and some of the sections need to be displayed better").

Three shipped changes, each pinned here:
  1. Recolor — graphite chrome + indigo accent + true-red BAD; INFO (sky) is no
     longer the same hue as the accent, so informational vs interactive reads.
  2. Status cells are ALWAYS the dark-tuned pairs (the browser-theme detection
     that rendered pastel light pairs on the pinned-dark chrome is gone — its
     own lock lives in tests/history_locks/test_ux_foundation.py).
  3. Section display — Admin and Alerts panel titles use the design-system
     section_header (stripe/icon/badge/anchor), not flat bold markdown; the two
     longest walls (Alerts→History, Admin→Performance) open with a section_toc;
     section headers carry top breathing room so panels read as sections.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.ui import palette

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- 1. recolor -------------------------------------------------------------

def test_accent_is_no_longer_the_info_hue():
    """The old scheme used sky for BOTH the accent and INFO, so interactive
    chrome and informational status were indistinguishable."""
    assert palette.ACCENT != palette.INFO
    assert palette.ACCENT.lower() == "#818cf8"          # indigo, not stock sky
    assert palette.INFO.lower() == "#38bdf8"            # sky stays semantic


def test_bad_is_a_red_not_a_pink():
    assert palette.BAD.lower() == "#ef5350"             # rose #fb7185 retired
    theme = _src("app/theme.py")
    assert "#fb7185" not in theme and "rgba(251,113,133" not in theme


def test_chrome_is_graphite_not_navy():
    assert palette.BG.lower() == "#0c0d12"
    for old in ("#0a0f1c", "#0f1729", "#131d33"):       # the navy trio is gone
        assert old not in _src("app/theme.py")
        assert old not in _src(".streamlit/config.toml")


def test_dag_iframe_chrome_is_palette_sourced():
    """The task-DAG iframe carried its own stale chrome literals; now the
    template holds placeholders filled from palette at render time."""
    ch = _src("app/ui/charts.py")
    for ph in ("__BG__", "__RAISED__", "__INK__", "__INK_SOFT__",
               "__INK_MUTE__", "__ACCENT__", "__SURFACE_GLASS__"):
        assert ph in ch, ph
        assert f'"{ph}", palette.' in ch or f'"{ph}", _rgba' in ch or ph == "__SURFACE_GLASS__"
    assert '.replace("__SURFACE_GLASS__", _rgba(palette.SURFACE, 0.94))' in ch
    # no hardcoded chrome hex left in the template CSS block
    tmpl = ch.split("_TASK_DAG_TEMPLATE", 1)[1].split("</style>", 1)[0]
    assert not re.search(r"#(0a0f1c|131d33|e8eef7|aab6c8|8593a8|38bdf8)", tmpl)


# --- 3. section display -----------------------------------------------------

def test_admin_and_alerts_use_section_headers_not_flat_bold():
    """Every static top-level panel title on Admin/Alerts is a section_header.
    The only st.markdown bold titles left are DYNAMIC drill labels (f-strings)
    or inline labels nested in a per-event detail expander."""
    adm = _src("app/ui/pages/admin.py")
    al = _src("app/ui/pages/alerts.py")
    assert adm.count("section_header(") >= 16
    assert al.count("section_header(") >= 10
    # static flat titles: zero on Admin; Alerts keeps exactly the two
    # per-event inline labels (Loop status / Savings booked).
    assert len(re.findall(r'st\.markdown\("\*\*', adm)) == 0
    assert len(re.findall(r'st\.markdown\("\*\*', al)) == 2
    assert "st.subheader(" not in _src("app/ui/pages/control_room.py")


def test_long_walls_open_with_a_toc():
    assert 'section_toc([("Events by day", "al-events-day")' in _src("app/ui/pages/alerts.py")
    assert 'section_toc([("SLO scorecard", "adm-perf-slo")' in _src("app/ui/pages/admin.py")


def test_section_headers_carry_breathing_room():
    theme = _src("app/theme.py")
    assert "margin:18px 0 8px" in theme                 # comfortable density
    assert "margin:10px 0 4px" in theme                 # compact keeps hierarchy
