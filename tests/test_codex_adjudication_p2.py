"""Codex UI/UX review — adjudicated P2 + the owner's resource-monitor keep/kill decision.

- The owner chose to REMOVE the resource-monitor coverage panel that had returned post the
  standing "Resource monitors are GONE" decision (it was read-only visibility, but the owner
  wanted zero RM surface). Panel + call site + SQL builder + wave2 helper + tests removed;
  auto-suspend / quiet-hours analysis stays.
- Admin's 15 bold-Markdown pseudo-headings were migrated to the `section_header` primitive
  (icon + a11y heading + consistent stripe) the rest of the app uses.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_resource_monitor_surface_is_fully_removed():
    # no panel, no SQL builder, no coverage helper anywhere in the app
    for rel in ("app/ui/pages/operations.py", "app/data/ops_sql.py", "app/logic/wave2.py"):
        src = _read(rel)
        assert "_monitor_coverage_panel" not in src, rel
        assert "show_resource_monitors_sql" not in src, rel
        assert "monitor_coverage" not in src, rel
        assert "SHOW RESOURCE MONITORS" not in src, rel
    # the sibling wave-2 logic the panel did NOT own survives
    w = _read("app/logic/wave2.py")
    assert "def token_economics(" in w and "def fleet_cache_hit_pct(" in w


def test_admin_uses_the_section_header_primitive_not_markdown_pseudo_headings():
    src = _read("app/ui/pages/admin.py")
    # no standalone bold-Markdown pseudo-heading remains
    assert not re.search(r'^\s*st\.markdown\("\*\*[^*]*\*\*"\)$', src, re.M)
    # the page now imports + uses the primitive (it used zero section_header before)
    assert "section_header" in src
    assert src.count("section_header(") >= 15
