"""UI/UX master list — Wave 2 alerts-triage batch (F49 + C43).

Locks: F49 the DECIDE bar (investigate/fix + ack/resolve/snooze + execute) leads
the drawer, with the evidence panels demoted into a Supporting-evidence group ·
C43 bulk acknowledge/resolve runs off in-table multi-row selection (one row =
drawer, several = bulk panel with a severity summary), the duplicate multiselect
lookup deleted, and selectable_table gains a clamped multi-row mode.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.ui.components import selectable_table

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(
    encoding="utf-8")
_COMP = (Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py").read_text(
    encoding="utf-8")


# ---- F49: decide bar leads the drawer ----------------------------------------

def test_decide_bar_renders_before_the_evidence_panels():
    # the action radio + execute must come BEFORE the playbook/history/AI panels
    action = _SRC.index('action = st.radio("Action", ["ACK", "RESOLVE", "SNOOZE"]')
    execute = _SRC.index('"Execute with audit row"')
    playbook = _SRC.index('"Playbook — what to do first"')
    history = _SRC.index("This rule recently")
    ai = _SRC.index("Assemble the evidence")
    assert action < playbook and execute < playbook
    assert playbook < history < ai                    # evidence order preserved below


def test_evidence_is_grouped_and_playbook_demoted():
    assert '**Supporting evidence**' in _SRC
    # the playbook no longer auto-expands above the decision
    assert 'st.expander("Playbook — what to do first", expanded=False)' in _SRC
    assert 'st.expander("Playbook — what to do first", expanded=True)' not in _SRC
    # the group heading sits after the execute controls
    assert _SRC.index('"Execute with audit row"') < _SRC.index('**Supporting evidence**')


def test_nav_targets_are_hoisted_with_the_decide_bar():
    # target/fix/wh_inline are computed before the buttons that use them
    assert _SRC.index("target = investigation_target(") < _SRC.index('st.button("Investigate →"')


# ---- C43: in-table multi-row bulk selection ----------------------------------

def test_feed_selection_is_mode_gated_multi_row():
    # review fix: bulk is an explicit OPERATOR-ONLY mode (single-click drawer stays
    # the default gesture), the table key carries the mode so state can't leak
    # across flips, and the single-mode int return is never truthiness-mangled.
    assert "_bulk_mode = bool(is_operator and st.toggle(" in _SRC
    assert "multi=_bulk_mode)" in _SRC
    assert "key=f\"alert_events_sel_{_sel_nonce}_{'m' if _bulk_mode else 's'}\"" in _SRC
    assert "sel = int(_raw_sel) if _raw_sel is not None else None" in _SRC


def test_bulk_panel_is_selection_driven_with_a_severity_summary():
    assert "if is_operator and _bulk_rows:" in _SRC
    assert "_bdf = edf.iloc[_bulk_rows]" in _SRC
    assert '_bdf["SEVERITY"].astype(str).str.upper().value_counts()' in _SRC
    assert '_bulk_ids = _bdf["EVENT_ID"].astype(str).tolist()' in _SRC
    # the duplicate bulk-pick multiselect is gone (the un-snooze picker stays)
    assert 'st.multiselect("Events"' not in _SRC


def test_deeplink_fallback_defers_to_a_bulk_selection():
    assert "if sel is None and not _bulk_rows and requested_event:" in _SRC


def test_bulk_set_binds_by_identity_and_disarms_on_feed_shift():
    # review fix: the bulk SET gets the F51 treatment — same positions but
    # different EVENT_IDs across a full rerun disarms the typed confirm gate.
    assert '"_ow_alert_bulk_bind"' in _SRC
    idx = _SRC.index("the bulk SET binds by identity")
    block = _SRC[idx:idx + 1100]
    assert 'tuple(_bbind[1]) != _ids_here' in block
    assert "st.rerun()" in block
    # and the bind never outlives a write or the rollup early-return
    assert _SRC.count('st.session_state.pop("_ow_alert_bulk_bind", None)') >= 4


def test_recheck_copy_points_at_the_decide_bar_above():
    assert "resolve as ACTIONED in the" in _SRC and "decide bar above" in _SRC
    assert "resolve as ACTIONED below" not in _SRC


def test_selectable_table_multi_mode_contract():
    # the shared helper grew a clamped multi-row mode without breaking callers
    sig = inspect.signature(selectable_table)
    assert "multi" in sig.parameters and sig.parameters["multi"].default is False
    assert 'selection_mode="multi-row" if multi else "single-row"' in _COMP
    assert "_valid if multi else (_valid[0] if _valid else None)" in _COMP
