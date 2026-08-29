"""UI/UX master list — C47: shared master-detail (Action Center + Decision Studio).

Locks: components.master_detail owns the two-column split (ranked work LEFT,
selected-item detail RIGHT) and the positional-selection → stable-id → sticky
persistence dance, while each caller keeps its own table flavor
(list_render_fn) and editor body (detail_render_fn). Selection binds by
IDENTITY (id_col), not a re-sortable positional index; a fresh preselect
(deep-link) wins once then a click wins; nothing selected shows the empty-detail
hint (never row 0's editor). The columns restack on narrow viewports via the
shared st-key-ow_md_* CSS.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_primitive_binds_by_identity_and_persists_stickily():
    comp = _src("app/ui/components.py")
    body = comp.split("def master_detail(", 1)[1].split("\ndef ", 1)[0]
    assert "st.columns(ratio" in body                       # the two-pane split
    assert 'st.container(key=f"ow_md_{key}")' in body       # keyed for the restack CSS
    # selection persists by the row's stable id, not the positional index
    assert 'persist = f"_ow_md_sel_{key}"' in body
    assert "str(display.iloc[int(sel)][id_col])" in body
    assert "display[display[id_col].astype(str) == selected_id]" in body
    # review fix: the rec29 seen-guard resolves position->id only on a genuine
    # NEW click, so a sticky index re-emitted after a re-sort can't rebind the
    # detail to the wrong row (the HIGH the review caught)
    assert "sel != st.session_state.get(seen_sel)" in body
    # a deep-link preselect wins and clears the sticky selection so it can't clobber
    assert "if preselect_id:" in body
    assert "st.session_state.pop(table_key, None)" in body
    # nothing selected -> the empty hint, never row 0's editor
    assert "if row is not None:" in body and "st.caption(empty_detail_msg)" in body


def test_action_center_deeplink_is_one_shot():
    # review fix: the deep-link action_id is consumed on arrival so a later
    # manual click sticks and a repeat nav to the SAME id re-focuses it
    ac = _src("app/ui/workbench.py").split("def render_action_center", 1)[1].split("\ndef ", 1)[0]
    assert 'k: v for k, v in _nav.items() if k != "action_id"' in ac


def test_action_center_is_master_detail():
    wb = _src("app/ui/workbench.py")
    ac = wb.split("def render_action_center", 1)[1].split("\ndef ", 1)[0]
    assert 'master_detail(' in ac and 'id_col="ACTION_ID"' in ac
    assert "list_render_fn=_ac_list" in ac
    assert "detail_render_fn=lambda r: _render_action_detail(r, extended=extended)" in ac
    # deep-link action_id preselects, validated against the live frame
    assert '_ctx_id = str(navigation_context().get("action_id") or "").strip()' in ac
    assert 'set(display["ACTION_ID"].astype(str))' in ac
    # the old orphaned selection helper is gone
    assert "_apply_action_context" not in wb


def test_decision_studio_experiments_is_master_detail():
    ds = _src("app/ui/decision_studio.py")
    exp = ds.split("def _experiments", 1)[1].split("\ndef ", 1)[0]
    assert 'master_detail(' in exp and 'id_col="EXPERIMENT_ID"' in exp
    assert "detail_render_fn=_render_experiment_detail" in exp
    # the detail body was extracted to a function taking the bound row; the C48
    # save latch still keys off EXPERIMENT_ID
    detail = ds.split("def _render_experiment_detail(", 1)[1].split("\ndef ", 1)[0]
    assert 'experiment_id = str(row["EXPERIMENT_ID"])' in detail
    assert 'write_gate_open(f"experiment_save_{experiment_id}")' in detail
    assert "frame.iloc[int(selected)]" not in detail        # no positional re-derive


def test_narrow_viewport_restacks_the_columns():
    theme = _src("app/theme.py")
    assert '@media (max-width:1180px)' in theme
    assert '[class*="st-key-ow_md_"] [data-testid="stColumn"]' in theme
    assert "flex:1 1 100%" in theme


def test_docs_carry_the_master_detail_contract():
    assert "Master-detail layout (C42/C47)" in _src("ARCHITECTURE.md")
