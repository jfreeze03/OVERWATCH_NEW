"""Bug-hunt round 26: adversarial find→refute-verify sweep over the fresh UI/UX
refactor (v4.461–v4.477) + V125. 1 confirmed defect; the other 5 candidates did
NOT survive refute-verify (salience / color-prominence nits — a folded card kept
its value + help + the per-row verdict in the table, losing only a background
color, which the round explicitly excluded as a style preference).

DEEP-LINK REGRESSION (r26 #1, confirmed): v4.475 deferred the Cost ▸ Attribution
panel — which OWNS the by-warehouse "full spend movers" table — behind the
`cost_attribution_load` toggle (off by default, for a lighter first paint). But the
Control Room "Full spend movers → Cost & Contract" button deep-links to that section
expecting the table visible; after the split it landed on the Spend view with the
table hidden and no cue. Fix: the CR button seeds the toggle OPEN before navigating,
so the deep-link lands directly on the promised table while ordinary entries into
Spend & Attribution keep the toggle off (the perf default).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_cr_full_movers_deeplink_opens_the_deferred_attribution_toggle():
    cr = _read("app/ui/pages/control_room.py")
    seg = cr.split('key="cr_movers_cost"', 1)[1].split("result_caption", 1)[0]
    # the deep-link still targets Spend & Attribution
    assert 'request_navigation("Cost & Contract", "Spend & Attribution")' in seg
    # and it seeds the deferred movers-table toggle OPEN, BEFORE navigating
    assert 'st.session_state["cost_attribution_load"] = True' in seg
    assert seg.index("cost_attribution_load") < seg.index("request_navigation("), \
        "the toggle must be seeded before the navigation call"


def test_movers_table_is_in_the_deferred_attribution_tab_gated_by_that_toggle():
    # grounds the contract the deep-link depends on: the by-warehouse exact-usage
    # movers table lives in _attribution_tab (deferred), not the eager _spend_tab,
    # and that panel is gated by cost_attribution_load.
    spend = _read("app/ui/pages/cost_parts/spend.py")
    attr = spend.split("def _attribution_tab", 1)[1].split("\ndef ", 1)[0]
    assert "**By warehouse (exact usage)**" in attr
    spend_tab = spend.split("def _spend_tab", 1)[1].split("def _attribution", 1)[0]
    assert "**By warehouse (exact usage)**" not in spend_tab
    assert 'key="cost_attribution_load"' in _read("app/ui/pages/cost.py")
