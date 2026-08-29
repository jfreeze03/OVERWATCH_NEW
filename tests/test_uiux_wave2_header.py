"""UI/UX master list — Wave 2 header cluster (F9 + F6).

Locks: F9 a "Group ▸ Page ▸ Section" breadcrumb kicker above the page title
(nav group from NAV_GROUPS, section from the persisted ?section= deep link,
degrading to Group ▸ Page) so a deep-link landing says where it is above the
fold · F6 the ACCOUNT_USAGE lag caption prints ONLY on the metering surfaces
that actually read lagging data, so it stops being noise where it doesn't apply.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.ui import components

_COMP = (Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py").read_text(
    encoding="utf-8")


def test_f9_breadcrumb_group_page_section(monkeypatch):
    # review fix: the section comes from the page's remembered session key, not
    # the lagging ?section= param. Fresh visit (key unset) -> the FIRST section
    # (what lazy_sections lands on), so the crumb matches the pills below.
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state={}))
    crumb = components._page_breadcrumb("Operations")
    assert crumb.startswith("Analyze ▸ Operations ▸ ")     # defaults to the first section
    assert components._page_breadcrumb("Security").startswith("Govern ▸ Security ▸ ")
    # a remembered section renders exactly
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state={"ops_section": "Warehouses"}))
    assert components._page_breadcrumb("Operations") == "Analyze ▸ Operations ▸ Warehouses"
    # a stale/invalid stored section falls back to the first (never a wrong label)
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state={"ops_section": "NotARealSection"}))
    assert components._page_breadcrumb("Operations").startswith("Analyze ▸ Operations ▸ ")
    assert "NotARealSection" not in components._page_breadcrumb("Operations")
    # a sectionless page in no nav group still degrades gracefully (no crash)
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state={}))
    assert "Unknown Page" in components._page_breadcrumb("Unknown Page")


def test_page_header_renders_the_breadcrumb_before_the_title():
    body = _COMP.split("def page_header(", 1)[1].split("\ndef ", 1)[0]
    assert "_crumb = _page_breadcrumb(title)" in body
    assert 'class="ow-breadcrumb"' in body
    assert body.index("_crumb = _page_breadcrumb") < body.index("if icon_name:")
    assert ".ow-breadcrumb" in (Path(__file__).resolve().parents[1] / "app" / "theme.py").read_text(
        encoding="utf-8")


def test_f6_lag_caption_is_gated_to_metering_surfaces():
    # review fix: Control Room (live QUERY_HISTORY/TASK_HISTORY fallback) and
    # Brief (FACT_METERING_DAILY headline) surface lagging data prominently too.
    _expected = {
        "Cost & Contract", "Operations", "Security", "Overview", "Control Room", "Brief"}
    assert _expected == components._LAGGING_SURFACES
    body = _COMP.split("def page_header(", 1)[1].split("\ndef ", 1)[0]
    # the note is no longer unconditional — it's gated to the lagging surfaces
    assert "if title in _LAGGING_SURFACES:" in body
    assert body.index("if title in _LAGGING_SURFACES:") < body.index("st.caption(ACCOUNT_USAGE_LAG_NOTE)")
    # pure app-table pages stay excluded (the note would be noise there)
    for p in ("Admin", "Ask", "Decision Studio"):
        assert p not in components._LAGGING_SURFACES
