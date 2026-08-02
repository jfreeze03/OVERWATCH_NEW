"""Bug round 5 regression locks (app-only findings).

Three of the five findings were regressions from the recent design waves; these pin the
fixes so they cannot silently return. The two migration-proc findings are tracked
separately (they need an owner migration, not an app edit)."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rank 1 (HIGH) — Overview "Top actions" infinite rerun for EXECUTIVE.
# request_navigation must clamp an off-profile target to the viewer's profile and
# NO-OP a jump that resolves to the current page (no section/filters) — otherwise a
# sticky st.dataframe selection re-fires it every rerun and the page spins forever.
# ---------------------------------------------------------------------------
class _FakeSt:
    def __init__(self, page: str = "Overview"):
        self.session_state: dict = {"_ow_page": page}
        self.reran = 0

    def rerun(self) -> None:
        self.reran += 1


def test_request_navigation_noops_offprofile_selfjump(monkeypatch):
    from app.core import state

    fake = _FakeSt("Overview")
    monkeypatch.setattr(state, "st", fake)
    import app.core.session as session
    monkeypatch.setattr(session, "current_role", lambda: "ALFA_PDMWMGMT")  # -> EXECUTIVE

    # Control Room is NOT in the EXECUTIVE profile -> clamps to Overview == current page.
    state.request_navigation("Control Room")
    assert "_ow_nav_pending" not in fake.session_state   # nothing queued
    assert fake.reran == 0                                # and no rerun -> no loop


def test_request_navigation_still_jumps_cross_page(monkeypatch):
    from app.core import state

    fake = _FakeSt("Overview")
    monkeypatch.setattr(state, "st", fake)
    import app.core.session as session
    monkeypatch.setattr(session, "current_role", lambda: "ALFA_PDMWMGMT")  # -> EXECUTIVE

    # Cost & Contract IS in the EXECUTIVE profile -> a real jump still queues + reruns.
    state.request_navigation("Cost & Contract")
    assert fake.session_state["_ow_nav_pending"]["page"] == "Cost & Contract"
    assert fake.reran == 1

    # a same-page jump that carries a section is NOT a no-op (section drill still fires).
    fake.session_state["_ow_page"] = "Cost & Contract"
    fake.session_state.pop("_ow_nav_pending", None)
    state.request_navigation("Cost & Contract", "Contract")
    assert fake.session_state["_ow_nav_pending"]["section"] == "Contract"


def test_overview_top_actions_acts_only_on_new_selection():
    ov = _src("app/ui/pages/overview.py")
    comp = _src("app/ui/components.py")
    # rec29: the new-selection guard was extracted into selectable_nav_table.
    # Overview uses it; the component fires on_select only on a CHANGED selection.
    assert "selectable_nav_table(" in ov and 'key="ov_actions_sel"' in ov
    assert "sel != st.session_state.get(seen_key)" in comp


# ---------------------------------------------------------------------------
# Rank 4 (LOW) — allocation caption pool must match the path that actually served.
# ---------------------------------------------------------------------------
def test_spend_alloc_caption_pool_reads_served_source():
    src = _src("app/ui/pages/cost_parts/spend.py")
    assert "_dim_res = {dim: _fetch_alloc(dim)" in src          # dims pre-fetched (cached)
    assert "_intro_pool = _alloc_pool(_served.source)" in src   # pool off the served path
    # the old guess (full window whenever no schema filter) is gone
    assert '_alloc_pool("QUERY_HISTORY") if schema_contains else window_usd' not in src


# ---------------------------------------------------------------------------
# Rank 5 (LOW) — wide-table identity column keeps BOTH its pin and its pretty label.
# (Behavioural label check lives in test_design_next.test_rec13_prettify_header.)
# ---------------------------------------------------------------------------
def test_prettify_pin_share_one_column():
    comp = _src("app/ui/components.py")
    assert "st.column_config.Column(_label, pinned=True, help=_help)" in comp  # rec32 added header help
    assert "def _auto_pin" not in comp   # the separate pin pass (which the prettifier defeated) is gone


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
