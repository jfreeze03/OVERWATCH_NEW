"""PR C review r2: a Verify click never executes a statement nobody saw.

``_savings_tab`` is rendered directly (no nav), so these AppTests also run on the streamlit 1.52.2 floor, where a
changed option list resets the item selectbox (its identity includes the options). Two stale-click paths:
the ledger gains a row between the render and the click, and an item switch coalesces with the click.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from streamlit.testing.v1 import AppTest
from test_pages_shaped import (  # noqa: F401  (harness stubs, as the shaped tests use)
    _shaped_run,
    _stub_shaped,
)
from test_prc_c1_shaped import _amount_input, _estimated_schedule_row, _measurement, _ok

A = "abcdef12-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
N = "cccccccc-0000-4000-8000-000000000003"
_VERIFIED = "SET STATE = 'VERIFIED'"


def _row(item: str, desc: str, target: str, created: str) -> pd.Series:
    r = _estimated_schedule_row()
    r["ITEM_ID"] = item
    r["DESCRIPTION"] = desc
    r["TARGET_OBJECT"] = target
    r["CREATED_AT"] = pd.Timestamp(created)
    return r


def _meas(booked: date, before: float, after: float) -> pd.DataFrame:
    return pd.DataFrame([{"BASIS": "WAREHOUSE", "BOOKED_DAY": booked, "BEFORE_DAYS": 14,
                          "MAX_AFTER_DAYS": 30, "BEFORE_CREDITS": before, "AFTER_CREDITS": after,
                          "BEFORE_QUERIES": 14000.0, "AFTER_QUERIES": 30000.0,
                          "LOADED_THROUGH": date(2026, 9, 27)}])


def _tab() -> None:
    from app.ui.pages.cost_parts import optimize as opt
    opt._savings_tab(3.68, {})


def _harness(monkeypatch, state: dict, written: list[str]):
    import app.ui.pages.cost_parts.optimize as opt

    def _run(*args, **kwargs):
        key = str(kwargs.get("key") or "")
        sql = str(args[0]) if args else ""
        if key == "savings_ledger":
            return _ok(state["ledger"])
        if key.startswith("ledger_measure_"):
            if "WH_B" in sql:
                return _ok(_meas(date(2026, 6, 1), 2800.0, 1500.0))
            if "WH_N" in sql:
                return _ok(_meas(date(2026, 8, 20), 700.0, 600.0))
            return _ok(_measurement())
        return _ok(pd.DataFrame())

    def _record(sql, **_k):
        written.append(str(sql))
        return True, "ok"

    monkeypatch.setattr(opt, "run", _run)
    monkeypatch.setattr(opt, "_is_operator", lambda: True)
    monkeypatch.setattr(opt, "write_gate_open", lambda *_a, **_k: True)
    monkeypatch.setattr(opt, "execute_statement", _record)
    at = AppTest.from_function(_tab, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _shown(at) -> list[str]:
    return [str(c.value) for c in at.code if _VERIFIED in str(c.value)]


def test_a_new_ledger_row_between_render_and_click_never_writes_an_unseen_statement(monkeypatch):
    state = {"ledger": pd.concat([_row(A, "Suspend schedule on WH_A", "WH_A", "2026-08-01 09:00"),
                                  _row(B, "Suspend schedule on WH_B", "WH_B", "2026-06-01 09:00")],
                                 ignore_index=True)}
    written: list[str] = []
    at = _harness(monkeypatch, state, written)
    sb = at.selectbox(key="ledger_verify_pick")
    sb.set_value(next(o for o in sb.options if "bbbbbbbb" in o))
    at.run()
    assert not at.exception, at.exception
    painted = _shown(at)
    assert painted and f"ITEM_ID = '{B}'" in painted[0]
    # another operator books a new ESTIMATED item before this operator clicks Verify (on 1.52.2 the changed
    # option list resets the pick to it)
    state["ledger"] = pd.concat([_row(N, "Suspend schedule on WH_N", "WH_N", "2026-08-20 09:00"), state["ledger"]],
                                ignore_index=True)
    at.button(key="ledger_verify_exec").click()
    at.run()
    assert not at.exception, at.exception
    updates = [w for w in written if _VERIFIED in w]
    # either nothing is written (the click was stale), or exactly the painted statement -- never B's amount on N
    assert all(w == painted[0] for w in updates), updates
    assert not any(f"ITEM_ID = '{N}'" in w for w in updates)


def test_an_item_switch_coalesced_with_the_click_writes_nothing(monkeypatch):
    state = {"ledger": pd.concat([_row(A, "Suspend schedule on WH_A", "WH_A", "2026-08-01 09:00"),
                                  _row(B, "Suspend schedule on WH_B", "WH_B", "2026-06-01 09:00")],
                                 ignore_index=True)}
    written: list[str] = []
    at = _harness(monkeypatch, state, written)
    _amount_input(at).set_value(1234.0)
    at.run()
    assert not at.exception, at.exception
    # the operator picks B and clicks Verify before the page settles: one rerun carries both
    sb = at.selectbox(key="ledger_verify_pick")
    sb.set_value(next(o for o in sb.options if "bbbbbbbb" in o))
    at.button(key="ledger_verify_exec").click()
    at.run()
    assert not at.exception, at.exception
    assert not [w for w in written if _VERIFIED in w], written
    # B takes its own measured prefill, never A's typed 1234
    assert _amount_input(at).value != 1234.0
    assert any("nothing was written" in str(w.value) for w in at.warning)


def test_a_typed_amount_clicked_before_the_page_re_renders_writes_on_the_first_click(monkeypatch):
    """Review r3: the browser commits a number_input on blur, which the Verify click's mousedown triggers, so the
    typed amount and the click arrive in ONE rerun. Same item: the click writes the typed amount at once (a
    full-statement comparison used to refuse it with a false 'the list changed' warning)."""
    state = {"ledger": pd.concat([_row(A, "Suspend schedule on WH_A", "WH_A", "2026-08-01 09:00"),
                                  _row(B, "Suspend schedule on WH_B", "WH_B", "2026-06-01 09:00")],
                                 ignore_index=True)}
    written: list[str] = []
    at = _harness(monkeypatch, state, written)
    _amount_input(at).set_value(555.0)
    at.button(key="ledger_verify_exec").click()
    at.run()
    assert not at.exception, at.exception
    updates = [w for w in written if _VERIFIED in w]
    assert len(updates) == 1, written
    assert "VERIFIED_USD = 555" in updates[0] and f"ITEM_ID = '{A}'" in updates[0]
    assert not any("nothing was written" in str(w.value) for w in at.warning)
