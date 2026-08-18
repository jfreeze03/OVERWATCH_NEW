"""v4.151.0: Decision Studio promoted to its own Analyze page (rec8) +
Entity 360 catalog-seeded entity picker (rec12)."""

from __future__ import annotations

from pathlib import Path

from app.config import NAV_GROUPS, PAGES_BY_PROFILE
from app.logic.navigate import PAGE_SECTION_KEYS, PAGE_SECTION_LABELS

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- rec8: Decision Studio is a first-class page ---------------------------------
def test_decision_studio_is_a_top_level_analyze_page():
    assert "Decision Studio" in NAV_GROUPS["Analyze"]
    for prof in ("ANALYST", "MANAGER", "DBA"):
        assert "Decision Studio" in PAGES_BY_PROFILE[prof]
    # EXECUTIVE never had Control Room (where it lived), so it doesn't get the page.
    assert "Decision Studio" not in PAGES_BY_PROFILE["EXECUTIVE"]
    assert PAGE_SECTION_KEYS["Decision Studio"] == "decision_section"
    assert PAGE_SECTION_LABELS["Decision Studio"] == [
        "ROI", "Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"]


def test_decision_studio_registered_in_main_with_a_page_shell():
    main = _src("app/main.py")
    assert '"Decision Studio": decision_studio.render' in main
    page = _src("app/ui/pages/decision_studio.py")
    assert "@safe_page" in page and "def render()" in page
    assert 'key="decision_section"' in page
    # The six section bodies still live in the ui module and are dispatched here.
    for view in ("Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"):
        assert f'"{view}"' in page
    # Cross-jumps into Entity 360 still target Control Room (Entity 360 stays there).
    assert '"Control Room", "Entity 360"' in _src("app/ui/decision_studio.py")


def test_legacy_saved_view_remaps_to_the_new_page():
    # A stale default-landing / saved view {Control Room, Decision Studio} must land on
    # the new page, not silently fall back to Control Room's first section.
    remap = _src("app/core/state.py").split(
        "def consume_pending_navigation", 1)[1].split("\ndef ", 1)[0]
    assert '"Control Room"' in remap and '"Decision Studio"' in remap
    assert '"page": "Decision Studio"' in remap and '"section": "Portfolio"' in remap


def test_control_room_no_longer_carries_decision_studio():
    cr = _src("app/ui/pages/control_room.py")
    # No quoted section label, dispatch branch, or import remains (an explanatory
    # comment may still name it in prose).
    assert '"Decision Studio"' not in cr
    assert 'elif section == "Decision Studio"' not in cr
    assert "render_decision_studio" not in cr
    assert '"Entity 360"' in cr          # Entity 360 stays in Control Room


# --- rec12: Entity 360 catalog picker with free-text fallback --------------------
def test_entity_360_has_catalog_picker_with_free_text_fallback():
    wb = _src("app/ui/workbench.py")
    e360 = wb.split("def render_entity_360", 1)[1].split("\ndef ", 1)[0]
    assert "entity_catalog(entity_type=kind" in e360   # catalog seeded per kind
    assert "Pick a catalogued" in e360
    assert 'st.text_input("Entity key"' in e360        # free-text escape hatch kept
    # Single source of truth: the picker POPULATES the text box via on_change (no
    # separate `picked or typed` that could shadow a drilled key), and the drill seed
    # clears the pick so a stale earlier pick can't win.
    assert "on_change=_apply_pick" in e360
    assert 'st.session_state["entity_360_key"] = _p' in e360
    seed = wb.split("def _seed_entity_context", 1)[1].split("\ndef ", 1)[0]
    assert 'st.session_state[f"entity_360_pick_{kind}"] = ""' in seed
