"""F51 (UI/UX master list) — the alert drawer must bind by EVENT IDENTITY.

The open-events selection is a sticky POSITIONAL st.dataframe index. When the live
feed shrinks or reorders under it (a resolve here, the V091 auto-resolver, a filter
change), the same index silently lands on a DIFFERENT event — and before this fix
the drawer opened (and could act on) the wrong one. The adversarial review of the
first cut confirmed 13 follow-on defects; this locks the full design:

  * pure staleness decision (_stale_rebind) + identity binding, on the DRAWER and
    the storm-rollup selection alike;
  * every selection/write widget mounts under a GENERATION NONCE — popping session
    keys is not a reset (the frontend re-sends values by element id);
  * receipt + feed-shift notice render BEFORE the guard, so resolving the LAST
    event still shows its receipt and nothing strands to resurface stale;
  * the deep-link identity fallback is armed per arrival and DISARMED by a write's
    nonce bump (it used to re-fire every paint, reopening the acted drawer), and a
    deep-link-derived selection bypasses the positional-staleness guard;
  * a guard trip parks its notice and RERUNS so the fresh table mounts in the same
    interaction; an orphaned bind never outlives its selection;
  * ACK keeps the drawer open (the event stays in the feed) — only RESOLVE/SNOOZE
    reset the selection; every write reruns the full page (notify()'s contract:
    the re-read feed is the durable receipt).
"""

from __future__ import annotations

from pathlib import Path

from app.ui.pages.alerts import _stale_rebind

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(
    encoding="utf-8")


# ---- pure staleness decision -------------------------------------------------

def test_same_index_different_event_is_stale():
    assert _stale_rebind(3, "EVENT_B", (3, "EVENT_A")) is True


def test_same_index_same_event_is_not_stale():
    assert _stale_rebind(3, "EVENT_A", (3, "EVENT_A")) is False


def test_changed_index_is_a_genuine_click_not_stale():
    assert _stale_rebind(5, "EVENT_B", (3, "EVENT_A")) is False


def test_no_or_garbage_binding_is_not_stale():
    assert _stale_rebind(3, "EVENT_A", None) is False
    assert _stale_rebind(3, "EVENT_A", ()) is False
    assert _stale_rebind(3, "EVENT_A", ("x",)) is False
    assert _stale_rebind(3, "EVENT_A", ("not-int", "EVENT_A")) is False
    assert _stale_rebind(3, "EVENT_A", {"idx": 3}) is False


# ---- nonce-keyed widgets (reset-by-remount) ----------------------------------

def test_every_selection_and_write_widget_is_nonce_keyed():
    for needle in (
        'key=f"alert_events_sel_{_sel_nonce}_',
        'key=f"alert_rollup_sel_{_sel_nonce}"',
        'key=f"alert_action_{event_id[:8]}_{_sel_nonce}"',
        'key=f"alert_note_{event_id[:8]}_{_sel_nonce}"',
        'key=f"alert_kind_{event_id[:8]}_{_sel_nonce}"',
        'key=f"alert_snooze_{event_id[:8]}_{_sel_nonce}"',
        'key=f"alert_exec_{event_id[:8]}_{_sel_nonce}"',
        'key=f"alert_bulk_note_{_sel_nonce}"',
        'key=f"alert_bulk_exec_{_sel_nonce}"',
    ):
        assert needle in _SRC, needle
    # the old fixed keys must not linger anywhere
    # C43 replaced the bulk multiselect with in-table multi-row selection.
    for stale in ('key="alert_events_sel"', 'key="alert_rollup_sel"',
                  'key="alert_bulk_pick"', 'st.multiselect("Events"'):
        assert stale not in _SRC, stale
    # pops of widget keys are NOT a reset — none may remain
    assert 'st.session_state.pop(f"alert_' not in _SRC
    assert 'st.session_state.pop("alert_bulk' not in _SRC


# ---- identity binding on both selections -------------------------------------

def test_drawer_and_rollup_bind_by_identity():
    assert "def _stale_rebind(" in _SRC
    assert "_stale_rebind(\n                    int(sel), _ev_here" in _SRC \
        or "_stale_rebind(int(sel), _ev_here" in _SRC.replace("\n", " ").replace("  ", " ") \
        or "int(sel), _ev_here" in _SRC
    assert '"_ow_alert_drawer_bind"' in _SRC
    assert '"_ow_alert_rollup_bind"' in _SRC


def test_guard_trip_parks_notice_and_reruns():
    # the stale branch must NOT render inline and continue — it parks the notice
    # and reruns so the fresh unselected table mounts in the same interaction.
    idx = _SRC.index("can never be a stale POSITIONAL")
    block = _SRC[idx:idx + 900]
    assert '"_ow_alert_stale_note"' in block
    assert "st.rerun()" in block


def test_orphaned_bind_never_outlives_its_selection():
    # no-selection branch and the rollup early-return both drop the drawer bind
    assert _SRC.count('st.session_state.pop("_ow_alert_drawer_bind", None)') >= 3


# ---- receipt & notice render before the guard --------------------------------

def test_receipt_and_notice_render_before_the_guard():
    pop_receipt = _SRC.index('st.session_state.pop("_ow_alert_receipt", "")')
    pop_note = _SRC.index('st.session_state.pop("_ow_alert_stale_note", "")')
    guard_at = _SRC.index("if guard(events,")
    assert pop_receipt < guard_at and pop_note < guard_at
    assert _SRC.count('st.session_state.pop("_ow_alert_receipt", "")') == 1


# ---- deep-link fallback arming -----------------------------------------------

def test_deeplink_fallback_is_armed_per_arrival_and_nonce_gated():
    assert '"_ow_alert_deeplink_armed"' in _SRC
    # a NEW arrival re-arms; the armed tuple carries the nonce so a write disarms
    idx = _SRC.index('st.session_state["_ow_alert_context_applied"] = event_signature')
    assert 'pop("_ow_alert_deeplink_armed"' in _SRC[idx:idx + 300]
    assert "_armed == (event_signature, _sel_nonce)" in _SRC
    # identity-derived selection bypasses the positional guard
    assert "_from_deeplink" in _SRC
    assert "not _from_deeplink and _stale_rebind" in _SRC


# ---- post-write behavior -----------------------------------------------------

def test_only_resolve_and_snooze_reset_the_selection():
    # ACK keeps the event in the feed at its index — the drawer stays open.
    idx = _SRC.index("ok, msg = execute_action(call, stmts, page=_PAGE)")
    block = _SRC[idx:idx + 1800]
    assert 'if action in ("RESOLVE", "SNOOZE"):' in block
    assert '"_ow_alert_sel_nonce"] = _sel_nonce + 1' in block
    assert '"_ow_alert_receipt"' in block
    assert "st.rerun()" in block


def test_bulk_write_bumps_nonce_and_reruns():
    idx = _SRC.index("ok_u, msg_u = execute_action(call, [upd, aud], page=_PAGE)")
    block = _SRC[idx:idx + 1200]
    assert '"_ow_alert_sel_nonce"] = _sel_nonce + 1' in block
    assert '"_ow_alert_receipt"' in block
    assert "st.rerun()" in block
