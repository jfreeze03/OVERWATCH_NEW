"""UI/UX master list — Wave 2 batch 4: alerts triage momentum (C44 + F50 + F57 + F52).

Locks: C44 a RESOLVE/SNOOZE queues the NEXT open event by identity (riding the
deep-link arming machinery, named in the receipt) · F50 the re-check verdict
persists per event and, when clear, one click prefills RESOLVE + ACTIONED + the
measured evidence (applied at the fragment top, before widgets mount) · F57 the
drawer's four supporting reads submit as one async batch with a named spinner ·
F52 a snooze shows its wake time at pick time and a soonest-first countdown in
the snoozed tray.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(
    encoding="utf-8")


# ---- C44: queue advance ------------------------------------------------------

def test_resolve_queues_the_next_event_by_identity():
    idx = _SRC.index("queue the next open event (identity, not")
    block = _SRC[idx:idx + 1000]
    assert 'st.session_state["_ow_alert_next_up"] = str(_nrow["EVENT_ID"])' in block
    assert "int(sel) + 1 < len(edf)" in block
    # the last event in the queue clears the marker instead of pointing nowhere
    assert 'st.session_state.pop("_ow_alert_next_up", None)' in block


def test_next_up_rides_the_deeplink_machinery_and_names_itself():
    # the queued event funnels through requested_event with its own signature —
    # the nonce folds the ARRIVAL EPOCH in (review fix: the applied-gate only
    # re-arms on signature CHANGE, so re-queuing the same id must read as new)
    assert 'event_signature = f"alert-next:{_next_up}:{_sel_nonce}"' in _SRC
    assert "if not requested_event and _next_up:" in _SRC
    # the receipt names what's next, so momentum is visible
    assert '_nxt_label = (f" — next: [{_nrow[\'SEVERITY\']}] "' in _SRC


# ---- review fixes (14 confirmed findings, deduped) ---------------------------

def test_write_consumes_the_spent_drill_identity():
    # HIGH: navigation_context() is read without consume and nothing else clears
    # it — a lingering event_id would shadow the queued next-up on every later
    # run (dead advance, lying receipt). RESOLVE/SNOOZE strips it; ACK keeps it.
    idx = _SRC.index('if action in ("RESOLVE", "SNOOZE"):')
    block = _SRC[idx:idx + 1800]
    assert 'if isinstance(_nav, dict) and _nav.get("event_id"):' in block
    assert 'k: v for k, v in _nav.items() if k != "event_id"' in block


def test_queue_misses_are_final_never_zombie_drawers():
    # a real deep-link superseding the queue, a feed miss, or bulk mode each
    # POP the queue instead of leaving it to fire arbitrarily later
    assert _SRC.count('st.session_state.pop("_ow_alert_next_up", None)') >= 4
    assert "_is_next and _bulk_mode" in _SRC
    # leaving the page expires the queue (main.py leg)
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8")
    assert 'if page != "Alerts" and st.session_state.get("_ow_alert_next_up"):' in main


def test_one_click_resolve_is_freshness_gated():
    # a persisted CLEAR is audit evidence — 30-minute gate, and the verdict
    # dies with the RESOLVE/SNOOZE write (snooze->wake must not resurface it)
    assert '"at_dt": account_now()' in _SRC
    assert "timedelta(minutes=30)" in _SRC
    assert "re-check again before resolving" in _SRC
    assert 'st.session_state.pop(f"_ow_recheck_{event_id[:8]}", None)' in _SRC


def test_null_recheck_never_fabricates_a_clear_zero():
    # NULL CURRENT_VALUE (idle warehouse, zero-row window) routes to the error
    # branch — never "Condition clear: 0.00" + a one-click that audits it
    assert 'default=float("nan")' in _SRC
    assert "if math.isnan(current_v):" in _SRC
    assert "condition not evaluable" in _SRC
    assert "if math.isnan(_rcv):" in _SRC          # render-side stale-entry guard


def test_multi_day_wake_copy_is_unambiguous():
    # a 1-week snooze lands on the SAME weekday — the caption carries the date
    # past a day out, and WAKES_IN rolls hours into days
    assert '"%a %H:%M" if float(snooze_hours) <= 24 else "%a %b %d, %H:%M"' in _SRC
    assert "if _secs >= 86400:" in _SRC
    assert 'return f"{_d}d {_h}h" if _h else f"{_d}d"' in _SRC


# ---- F50: persisted re-check + one-click resolve -----------------------------

def test_recheck_verdict_persists_per_event():
    assert '_rc_key = f"_ow_recheck_{event_id[:8]}"' in _SRC
    assert "st.session_state[_rc_key] = {" in _SRC
    assert '"at": account_now().strftime("%H:%M")' in _SRC   # staleness is visible


def test_one_click_resolve_prefills_before_widgets_mount():
    # the button stashes the prefill and reruns; the fragment applies it at the
    # top, BEFORE any widget mounts (setting an instantiated widget's key raises)
    idx = _SRC.index('st.session_state["_ow_alert_prefill"] = {')
    block = _SRC[idx:idx + 500]
    assert '"RESOLVE"' in block and '"ACTIONED"' in block
    assert "Re-check clear at" in block                      # the evidence note
    apply_at = _SRC.index('st.session_state.pop("_ow_alert_prefill", None)')
    first_widget = _SRC.index("st.toggle(")                  # first fragment widget
    assert apply_at < first_widget


# ---- F57: batched drawer reads -----------------------------------------------

def test_drawer_supporting_reads_are_one_batch():
    idx = _SRC.index('_dr = run_batch([')
    block = _SRC[idx:idx + 900]
    for key in ('"rules"', '"deliv"', '"hist"', '"res"'):
        assert key in block, key
    # named progress on a cold open; per-key run() fallbacks keep degrade honest
    assert 'st.spinner("Assembling event context…")' in _SRC
    assert '_dr.get("rules") or run(' in _SRC
    assert '_dr.get("deliv") or run(' in _SRC
    assert '_dr.get("hist") or run(' in _SRC
    assert '_dr.get("res") or run(' in _SRC


# ---- F52: snooze is a visible timer ------------------------------------------

def test_snooze_shows_wake_time_and_tray_countdown():
    assert "wakes ~{_wake.strftime(" in _SRC                 # at pick time
    assert '"WAKES_IN"' in _SRC                              # tray countdown col
    assert '_sdf.sort_values("SNOOZED_UNTIL")' in _SRC       # soonest wake first
