"""rec4: the Jump-to command palette + the deep-link resolver enumerate a page's
sections from navigate.PAGE_SECTION_LABELS. These tests keep that central map in
lock-step with each page's own lazy_sections() call, so renaming a section in one
place fails CI until the other matches (the house drift-guard pattern).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.logic.navigate import PAGE_SECTION_KEYS, PAGE_SECTION_LABELS

_ROOT = Path(__file__).resolve().parents[1]

_KEY_TO_FILE = {
    "cr_section": "app/ui/pages/control_room.py",
    "cost_section": "app/ui/pages/cost.py",
    "ops_section": "app/ui/pages/operations.py",
    "sec_section": "app/ui/pages/security.py",
    "alerts_section": "app/ui/pages/alerts.py",
    "adm_section": "app/ui/pages/admin.py",
}


def _lazy_sections_labels(src: str, key: str):
    """Labels passed to the lazy_sections(...) call whose key= matches, or the
    sentinel 'MAP' when the call passes PAGE_SECTION_LABELS[...] directly (then
    it cannot drift from the map)."""
    m = re.search(r"lazy_sections\((.*?)key\s*=\s*[\"']" + re.escape(key) + r"[\"']",
                  src, re.S)
    if not m:
        return None
    args = m.group(1)
    if "PAGE_SECTION_LABELS[" in args:
        return "MAP"
    return re.findall(r"[\"']([^\"']+)[\"']", args)


def test_section_key_and_label_maps_cover_the_same_pages():
    assert set(PAGE_SECTION_KEYS) == set(PAGE_SECTION_LABELS)


def test_page_lazy_sections_match_central_labels():
    for page, key in PAGE_SECTION_KEYS.items():
        src = (_ROOT / _KEY_TO_FILE[key]).read_text(encoding="utf-8")
        got = _lazy_sections_labels(src, key)
        assert got is not None, f"{page}: no lazy_sections(..., key={key!r}) found"
        if got == "MAP":
            continue
        assert got == PAGE_SECTION_LABELS[page], (
            f"{page}: lazy_sections labels {got} != central map "
            f"{PAGE_SECTION_LABELS[page]} — update navigate.PAGE_SECTION_LABELS "
            "and the page together")
