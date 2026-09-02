"""UI/UX master list — Wave 2 batch 5: C48 app-wide in-flight action state.

Two-part seam: (1) the query-layer write fns paint "Executing…" while the
round-trip runs — every write in the app gets the in-flight state at the one
seam they all cross; (2) every write CLICK BLOCK carries the duplicate-click
latch — write_gate_open(key) as the gate's last condition, stamp_write(key)
after the block's last write (a click during a slow write queues a second
rerun on which the same button fires again; un-latched, that double-books
SAVINGS_LEDGER rows, duplicates ACTION_QUEUE items, or declares a second
incident under a fresh uuid).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# every file with operator-write click blocks -> expected latched-block count
LATCHED_FILES = {
    "app/ui/pages/cost_parts/optimize.py": 5,
    "app/ui/pages/operations.py": 5,
    "app/ui/pages/alerts.py": 6,
    "app/ui/workbench.py": 6,
    "app/ui/pages/cost_parts/ai_chargeback.py": 3,
    "app/ui/security_center.py": 2,
    "app/ui/pages/control_room.py": 3,   # +1 v4.375: bulk 'resolve open incidents' reset panel
    "app/ui/decision_studio.py": 2,
    "app/ui/pages/cost.py": 1,
    "app/ui/pages/admin.py": 1,
}


def test_the_latch_pair_exists_and_is_honest():
    comp = _src("app/ui/components.py")
    gate = comp.split("def write_gate_open(", 1)[1].split("\ndef ", 1)[0]
    stamp = comp.split("def stamp_write(", 1)[1].split("\ndef ", 1)[0]
    # monotonic-clocked grace window, stamped at write END (the queued click is
    # processed only after the first run finishes, so the window need not cover
    # the write duration itself)
    assert "time.monotonic()" in gate and "time.monotonic()" in stamp
    assert "_WRITE_GRACE_S" in gate
    # a swallowed duplicate explains itself AND names the recovery — never a
    # silent nothing, never an error banner
    assert "click again to repeat it" in gate
    assert "st.toast(" in gate
    assert "st.error" not in gate
    # re-verify fix (HIGH): opening the gate ARMS the latch BEFORE the write —
    # a duplicate click on a non-fragment page PREEMPTS the running script
    # (RerunException at the next yield point, after the write committed but
    # before any end-of-block stamp), so an arm-at-end latch never arms in
    # exactly the scenario it exists for. stamp_write settles: refresh on
    # success, DELETE on failure (retry stays live).
    assert "arm BEFORE the write" in gate
    assert gate.index("latch[key] = (now, _run_seq())") > gate.index("return False")
    assert "latch.pop(key, None)" in stamp


def test_latch_is_run_seq_aware_per_key_and_success_only():
    # review fixes: (1) wall-clock alone loses to the write's own global cache
    # bump (the queued duplicate lands on the NEXT run however long it takes —
    # session runs are serial), so the run-sequence check is the load-bearing
    # swallow; (2) the latch is a per-key dict, so an interleaved write on
    # another block can't evict a pending swallow; (3) a FAILED write never
    # stamps — the operator's natural retry re-executes instead of reading
    # "just ran".
    comp = _src("app/ui/components.py")
    gate = comp.split("def write_gate_open(", 1)[1].split("\ndef ", 1)[0]
    stamp = comp.split("def stamp_write(", 1)[1].split("\ndef ", 1)[0]
    assert "_run_seq() <= int(entry[1]) + 1" in gate
    assert "_WRITE_BACKSTOP_S" in gate                     # stale-latch bound
    assert "dict(latch) if isinstance(latch, dict) else {}" in gate
    assert "def stamp_write(key: str, ok: bool = True)" in comp
    assert "if not ok:\n        latch.pop(key, None)" in stamp
    # the counter bumps once per FULL script run (fragments don't advance it)
    main = _src("app/main.py")
    assert 'st.session_state["_ow_run_seq"]' in main


def test_latch_keys_scope_by_action_and_target():
    # review fixes: fixed keys swallowed genuinely distinct actions —
    # ack->investigate->resolve on one alert, cancelling the NEXT runaway
    # query, budget saves for two departments, and the watch/unwatch undo
    # (where the direction lives in the WIDGET key too, so the label flip
    # drops a queued phantom click at the element-identity level).
    al = _src("app/ui/pages/alerts.py")
    assert al.count('f"alert_exec_{event_id[:8]}_{action}"') == 2   # gate + stamp
    # fragment surfaces freeze the run seq, so their keys carry the target:
    # un-snooze scopes by selection, the AI save by content (a regenerated
    # hypothesis is a new save; only a byte-identical re-save is swallowed)
    assert al.count("_unsz_key") >= 3                               # def + gate + stamp
    assert 'f"ai_save_{event_id[:8]}:{hash(answer) & 0xFFFFFF}"' in al
    ops = _src("app/ui/pages/operations.py")
    assert ops.count('f"emg_rq_{qid[:8]}"') == 2
    # the emergency surfaces get SHORT backstops (idempotent levers/cancels,
    # back-to-back actions under pressure) and the lever key scopes by
    # lever+target — a bare "emg" inside the fragment/dialog locked out every
    # subsequent emergency action for the whole backstop after one success
    assert 'f"emg:{action}:{stmt[:64]}"' in ops
    assert ops.count("backstop=15.0") == 2
    wb = _src("app/ui/workbench.py")
    assert '_dir = "rm" if is_watched else "add"' in wb
    assert 'f"entity_watch_toggle_{_dir}"' in wb                    # widget key
    assert wb.count('f"entity_watch_{_dir}:{kind}:{key}"') == 2     # latch key
    cb = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert cb.count('f"bud_save:{pick_dept}"') == 2
    assert cb.count('f"cb_map_exec:{name}"') == 2
    cost = _src("app/ui/pages/cost.py")
    assert cost.count('f"unmap_apply:{pick}:{company_choice}"') == 2


def test_every_write_click_block_is_latched():
    for rel, blocks in LATCHED_FILES.items():
        src = _src(rel)
        gates = len(re.findall(r"write_gate_open\(", src))
        stamps = len(re.findall(r"stamp_write\(", src))
        assert gates == blocks, f"{rel}: {gates} gates != {blocks} expected"
        assert stamps == blocks, f"{rel}: {stamps} stamps != {blocks} expected"


def test_query_layer_writes_paint_the_inflight_state():
    q = _src("app/core/query.py")
    stmt = q.split("def execute_statement(sql: str", 1)[1].split("\ndef ", 1)[0]
    act = q.split("def execute_action(", 1)[1].split("\ndef ", 1)[0]
    cancel = q.split("def execute_cancel_query(", 1)[1].split("\ndef ", 1)[0]
    assert 'st.spinner("Executing write…")' in stmt
    assert 'st.spinner("Executing action…")' in act
    assert 'st.spinner("Cancelling query…")' in cancel


def test_high_risk_ledger_blocks_stamp_after_the_conditional_booking():
    # the three optimize.py blocks book conditional SAVINGS_LEDGER rows — the
    # stamp must come AFTER the booking (all writes covered), before notify
    src = _src("app/ui/pages/cost_parts/optimize.py")
    for key, est in (("sizing", "est_sz"), ("waste", "est_w"), ("remed", "est_monthly")):
        idx = src.index(f'write_gate_open("{key}")')
        block = src[idx:idx + 2600]
        assert f'stamp_write("{key}", ok)' in block, key
        assert block.index(f"if ok and {est} > 0:") < block.index(f'stamp_write("{key}"'), key


def test_stamps_precede_reruns_everywhere():
    # st.rerun() raises — a stamp after it never executes, so in every latched
    # block that reruns, the stamp must come first
    for rel in LATCHED_FILES:
        src = _src(rel)
        for m in re.finditer(r"stamp_write\(([^)]*)\)", src):
            # from each stamp, the nearest FOLLOWING rerun (if any) must not be
            # between the block's last write and the stamp — approximated by:
            # no st.rerun() in the 300 chars BEFORE the stamp within the block
            before = src[max(0, m.start() - 300):m.start()]
            assert "st.rerun()" not in before, f"{rel}: rerun before stamp near {m.group(0)}"


def test_declare_incident_loop_stops_at_first_failure():
    # site-audit fix: INCIDENT_MEMBERS must never run after a failed INCIDENTS
    # insert (half-applied declare)
    # the declare exec keys are now scoped per proposal (_exec_key = inc_prop_exec_{_pick})
    src = _src("app/ui/pages/control_room.py")
    idx = src.index("write_gate_open(_exec_key)")
    block = src[idx:idx + 2400]
    # INC-1: a family already open pre-empts the declare (honest no-op, no phantom "declared")
    assert "ALREADY_OPEN" in block
    # within the declare loop, break precedes the loop's stamp_write, so a failed
    # INCIDENTS insert never runs INCIDENT_MEMBERS
    loop = block.split("for _stmt in _dec:", 1)[1]
    assert "break" in loop
    assert loop.index("break") < loop.index("stamp_write(_exec_key")


def test_ai_hypothesis_save_swallow_is_calm():
    # the ai_panel helper calls on_save directly (no click gate in alerts.py),
    # so the latch guards the callback entry — the swallow returns (True, "")
    # (the gate's own toast explains it) and ai_panel skips notify on an empty
    # message, so a swallow never paints a success/error banner of its own
    src = _src("app/ui/pages/alerts.py")
    idx = src.index("if not write_gate_open(_ai_key)")
    block = src[idx:idx + 300]
    assert 'return True, ""' in block
    panel = _src("app/ui/ai_panel.py")
    assert "if msg_s:" in panel
