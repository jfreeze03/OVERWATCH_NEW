"""v4.153.0 — Entity 360 Watchlist infinite-rerun regression locks.

Owner-reported ("definitive bug"): clicking a watchlist row made the app rerun
forever, forcing a disconnect. Two ingredients, both pinned here:

1. render_watchlist fed a RAW selectable_table selection straight into
   request_navigation. st.dataframe selections are sticky across reruns, and a
   same-page jump that carries a section + context is deliberately NOT a no-op
   (test_bug_round5 pins that), so the navigation re-fired every rerun.
2. The Entity 360 nested sub-tab stayed on "Watchlist", so the very component
   that re-fired the navigation was re-rendered by each rerun it caused —
   closing the loop.

The fix mirrors the codebase's own seen-guard (selectable_nav_table /
decision_rows): navigate only on a CHANGED selection, and switch the nested
sub-tab to "Entity" via a pending flag the dispatch consumes pre-render (the
lazy_sections seeding pattern), so the click lands on the drilled entity and
render_watchlist leaves the render path entirely.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_watchlist_navigates_only_on_changed_selection():
    wb = _src("app/ui/workbench.py")
    # The seen-guard: a sticky selection must not re-fire navigation.
    assert 'selected != st.session_state.get("_ow_watchlist_seen")' in wb
    assert 'st.session_state["_ow_watchlist_seen"] = selected' in wb
    # The raw unguarded shape must not return.
    assert "if selected is not None:\n        row = frame.iloc" not in wb
    # Re-arm on an unselected render (skeptic MED): the table unmounts after the
    # drill, so a stale seen index would swallow the first click on whatever row
    # now occupies it. An unselected mount must clear the guard so every fresh
    # click lands — and it cannot loop, because a None selection never fires.
    assert 'st.session_state.pop("_ow_watchlist_seen", None)' in wb
    rearm = wb.index('st.session_state.pop("_ow_watchlist_seen", None)')
    guard = wb.index('selected != st.session_state.get("_ow_watchlist_seen")')
    assert rearm < guard, "re-arm must run before the seen-guard"


def test_watchlist_click_switches_to_entity_subtab():
    wb = _src("app/ui/workbench.py")
    cr = _src("app/ui/pages/control_room.py")
    # The click asks for the Entity sub-tab (pending flag, not a direct widget
    # write — render_watchlist runs AFTER nested_sections instantiated the
    # widget this run, so a direct write would be a StreamlitAPIException).
    assert 'st.session_state["_ow_entity_view_pending"] = "Entity"' in wb
    # The dispatch consumes it BEFORE nested_sections renders (pop = one-shot).
    assert 'st.session_state.pop("_ow_entity_view_pending", None)' in cr
    consume = cr.index('st.session_state.pop("_ow_entity_view_pending", None)')
    render = cr.index('nested_sections(["Entity", "Watchlist"], key="cr_entity_view")')
    assert consume < render, "pending sub-tab flag must be consumed pre-render"
