"""Router consistency: navigate.py may only point at pages/sections that
actually exist in the UI source, and every seeded alert rule must resolve.

Source-scraping style (like the teardown-coverage test): the pages' own
lazy_sections calls are the ground truth, so section consolidations can
never silently strand alert deep-links again (the 'Spend' -> 'Spend &
Attribution' class of break).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config import PAGES_BY_PROFILE
from app.logic.navigate import (
    _FAMILY_DEFAULTS,
    _RULE_TARGETS,
    FIX_TARGETS,
    INLINE_FIX_RULES,
    PAGE_SECTION_KEYS,
    fix_target,
    investigation_target,
)

_ROOT = Path(__file__).resolve().parents[1]
_PAGES_DIR = _ROOT / "app" / "ui" / "pages"
_PAGE_FILES = {
    "Overview": "overview.py", "Control Room": "control_room.py",
    "Cost & Contract": "cost.py", "Operations": "operations.py",
    "Decision Studio": "decision_studio.py",
    "Alerts": "alerts.py", "Security": "security.py",
    "Admin": "admin.py", "Brief": "brief.py",
}


def _lazy_sections_of(page_file: str) -> tuple[list[str], str]:
    """(labels, key) from the page's lazy_sections(...) call; ([], '') if none."""
    src = (_PAGES_DIR / page_file).read_text(encoding="utf-8")
    m = re.search(r"lazy_sections\(\s*(\[.*?\])\s*,\s*key=(\"[^\"]+\"|'[^']+')",
                  src, re.DOTALL)
    if not m:
        return [], ""
    labels = ast.literal_eval(m.group(1))
    key = ast.literal_eval(m.group(2))
    return [str(x) for x in labels], str(key)


def _seeded_rule_ids() -> set[str]:
    ids: set[str] = set()
    for sql in (_ROOT / "snowflake" / "migrations").glob("*.sql"):
        ids.update(re.findall(r"'((?:COST|PERF|PIPE|SEC|OPS)_[A-Z_0-9]+)'",
                              sql.read_text(encoding="utf-8")))
    return ids


def test_page_section_keys_match_source():
    for page, key in PAGE_SECTION_KEYS.items():
        labels, actual_key = _lazy_sections_of(_PAGE_FILES[page])
        assert labels, f"{page}: no lazy_sections found"
        assert actual_key == key, f"{page}: navigate key {key!r} != source {actual_key!r}"


def test_static_targets_point_at_real_sections():
    for rid, (page, section) in {**_RULE_TARGETS, **FIX_TARGETS,
                                 **dict(_FAMILY_DEFAULTS)}.items():
        labels, _ = _lazy_sections_of(_PAGE_FILES[page])
        assert section in labels, f"{rid}: {page!r} has no section {section!r} (has {labels})"


def test_every_seeded_rule_resolves_to_a_real_target():
    seeded = _seeded_rule_ids()
    assert len(seeded) >= 20, f"rule scrape looks broken: {sorted(seeded)}"
    for rid in sorted(seeded):
        tgt = investigation_target(rid, "warehouse WH_TEST_X on DBX.SCH. something")
        page = tgt["page"]
        assert page in _PAGE_FILES, f"{rid}: unknown page {page!r}"
        if tgt["section"]:
            labels, _ = _lazy_sections_of(_PAGE_FILES[page])
            assert tgt["section"] in labels, f"{rid}: {page} lacks {tgt['section']!r}"
        fx = fix_target(rid, "")
        if fx:
            labels, _ = _lazy_sections_of(_PAGE_FILES[fx["page"]])
            assert fx["section"] in labels, f"{rid} fix: {fx['page']} lacks {fx['section']!r}"


def test_fix_and_inline_rules_are_seeded():
    seeded = _seeded_rule_ids()
    for rid in list(FIX_TARGETS) + list(INLINE_FIX_RULES) + list(_RULE_TARGETS):
        assert rid in seeded, f"{rid} routed but never seeded in any migration"


def test_jump_box_targets_exist():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    for page, section in re.findall(r'request_navigation\("([^"]+)",\s*"([^"]+)"', src):
        labels, _ = _lazy_sections_of(_PAGE_FILES[page])
        assert section in labels, f"jump box: {page!r} lacks section {section!r}"


# ---------------------------------------------------------------------------
# UI22: the universal entity drill (entity_nav_table) points at a real target
# and its row handler navigates to Entity 360 for the picked row.
# ---------------------------------------------------------------------------

def test_entity_nav_table_targets_real_section_and_is_adopted():
    comp_src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert 'request_navigation("Control Room", "Entity 360"' in comp_src
    labels, _ = _lazy_sections_of("control_room.py")
    assert "Entity 360" in labels           # the drill lands on a real section
    ops_src = (_PAGES_DIR / "operations.py").read_text(encoding="utf-8")
    assert ops_src.count("entity_nav_table(") >= 2 and 'entity_type="WAREHOUSE"' in ops_src


def test_entity_nav_table_opens_selected_entity(monkeypatch):
    import pandas as pd

    import app.ui.components as components
    captured: dict = {}
    monkeypatch.setattr(components, "selectable_nav_table",
                        lambda frame, key, on_select, **k: on_select(1))
    monkeypatch.setattr("app.core.state.request_navigation",
                        lambda page, section="", filters=None, context=None:
                        captured.update(page=page, section=section, context=context))
    df = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A", "WH_B"], "PEAK": [1, 2]})
    components.entity_nav_table(df, key="t", key_col="WAREHOUSE_NAME", entity_type="WAREHOUSE")
    assert captured["page"] == "Control Room" and captured["section"] == "Entity 360"
    assert captured["context"] == {"entity_type": "WAREHOUSE", "entity_key": "WH_B"}


def test_entity_nav_table_per_row_type_col_wins(monkeypatch):
    import pandas as pd

    import app.ui.components as components
    captured: dict = {}
    monkeypatch.setattr(components, "selectable_nav_table",
                        lambda frame, key, on_select, **k: on_select(0))
    monkeypatch.setattr("app.core.state.request_navigation",
                        lambda page, section="", filters=None, context=None: captured.update(context=context))
    df = pd.DataFrame({"ENTITY_TYPE": ["USER"], "ENTITY_KEY": ["JOE"]})
    components.entity_nav_table(df, key="t2", key_col="ENTITY_KEY",
                               type_col="ENTITY_TYPE", entity_type="FALLBACK")
    assert captured["context"] == {"entity_type": "USER", "entity_key": "JOE"}


def test_entity_nav_table_degrades_without_key_col(monkeypatch):
    import pandas as pd

    import app.ui.components as components
    calls = {"styled": 0, "nav": 0}
    monkeypatch.setattr(components, "styled_table",
                        lambda frame, **k: calls.__setitem__("styled", calls["styled"] + 1))
    monkeypatch.setattr(components, "selectable_nav_table",
                        lambda *a, **k: calls.__setitem__("nav", calls["nav"] + 1))
    components.entity_nav_table(pd.DataFrame({"X": [1]}), key="t3",
                               key_col="MISSING", entity_type="WAREHOUSE")
    assert calls["styled"] == 1 and calls["nav"] == 0


def test_dba_pages_cover_all_routed_pages():
    routable = {p for p, _ in {**_RULE_TARGETS, **FIX_TARGETS}.values()}
    routable |= {p for _, (p, _s) in _FAMILY_DEFAULTS}
    missing = routable - set(PAGES_BY_PROFILE["DBA"])
    assert not missing, f"routed pages not in DBA nav: {missing}"
