"""Holistic review #16 (v4.609.0) on the rendered page: which SP_INCIDENT_DECLARE overload Control Room CALLs.

V170 adds a 5-arg overload that writes the declaring DBA to DECLARED_BY / LINKED_BY; the kept 4-arg one credits the
app owner, and history is never rewritten. Control Room used to pick the overload from the startup gate's 4 h
metadata-tier SCHEMA_VERSION stash, so a declare inside that window after the apply still CALLed the 4-arg proc.
It now asks schema_gate.has_migration_fresh (a live-tier re-read only when the stash lacks V170) for the SQL preview,
the click and the two V170 captions. AppTest over the shaped harness (both CI legs); the gate's own unit locks are in
tests/test_schema_gate.py.

  prefix 169        stash 1..169, fresh read 1..169  -> 4-arg CALL, the pre-V170 caption
  stale stash       stash 1..169, fresh read 1..172  -> 5-arg CALL with the viewer, the post-V170 caption
  steady state      stash 1..tip                    -> 5-arg CALL, and no fresh read at all
"""

from __future__ import annotations

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from tests._source import migration_tip

_PICK = "PIPE_TASK_FAILURES|ALFA|OBJECT|ALFA_EDW.CORE.T_LOAD"
_FOUR = ("CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE('Task failures on T_LOAD', 'HIGH', 'ALFA', "
         f"'{_PICK}')")
_FIVE = _FOUR[:-1] + ", 'JDOE')"
_PRE = "show the app owner until the V170 schema update is applied"
_POST = "Manual declares made before the V170 schema update took effect"
_TASK = "Task-failure proposals reach HIGH only with a matching task change"


def _versions(tip: int) -> QueryResult:
    return QueryResult(df=pd.DataFrame({"VERSION": list(range(1, tip + 1))}), ok=True, source="SCHEMA_VERSION")


def _props() -> pd.DataFrame:
    return pd.DataFrame({
        "PROPOSAL_KEY": [_PICK], "SUGGESTED_TITLE": ["Task failures on T_LOAD"], "SEVERITY": ["HIGH"],
        "COMPANY": ["ALFA"], "ENTITY_KIND": ["OBJECT"], "ENTITY_NAME": ["ALFA_EDW.CORE.T_LOAD"],
        "CONFIDENCE": ["MEDIUM"], "ALERTS": [2], "MATCHED_WH_CHANGES": [0], "MATCHED_OBJECT_CHANGES": [0],
        "MATCHED_TASK_FAILURES": [3], "FIRST_TS": [pd.Timestamp("2026-09-30")],
        "LAST_TS": [pd.Timestamp("2026-10-01")], "EVIDENCE": ["Alert-family correlation only."],
    })


def _page(monkeypatch, *, stash_tip: int, fresh_tip: int):
    """Render Control Room > Incidents & triage as an operator. The startup gate hands ``stash_tip`` to the schema
    gate; any read schema_gate issues itself (the fresh one) answers ``fresh_tip``. Returns (app, gate reads, CALLs)."""
    import app.main as main_mod
    from app.core import identity
    from app.core import query as query_mod
    from app.ui import schema_gate
    from app.ui.pages import control_room

    def _floor_gate():
        schema_gate.remember(_versions(stash_tip))
        return None

    gate_reads: list[dict] = []

    def _gate_run(sql, **kw):
        gate_reads.append({"sql": sql, **kw})
        return _versions(fresh_tip)

    def _run(*args, **kw):
        if str(kw.get("key") or "").startswith("inc_famopen_"):          # no open incident for this family
            return QueryResult(df=pd.DataFrame({"ALREADY_OPEN": [False], "OPEN_INCIDENT_ID": [None]}), ok=True)
        return _shaped_run(*args, **kw)

    def _batch(specs, **kw):
        out = _shaped_batch(specs, **kw)
        if "props" in out:
            out["props"] = QueryResult(df=_props(), ok=True, source="INCIDENT_PROPOSALS")
        return out

    calls: list[str] = []

    def _execute_action(sql, fallbacks, **_kw):
        calls.append(sql)
        return True, "OK: declared 0b9c-0042 with 2 member(s) linked"

    monkeypatch.setattr(main_mod, "_schema_floor_breach", _floor_gate)
    monkeypatch.setattr(schema_gate, "run", _gate_run)
    monkeypatch.setattr(control_room, "run", _run)
    monkeypatch.setattr(control_room, "run_batch", _batch)
    monkeypatch.setattr(identity, "identity_sql", lambda: "'JDOE'")
    monkeypatch.setattr(query_mod, "execute_action", _execute_action)

    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Control Room")
    at.session_state["cr_section"] = "Incidents & triage"
    at.session_state["_ow_current_role"] = "SNOW_SYSADMINS"
    at.run()
    assert not at.exception, at.exception
    return at, gate_reads, calls


def _preview(at) -> str:
    codes = [str(c.value) for c in at.code if "SP_INCIDENT_DECLARE" in str(c.value)]
    assert len(codes) == 1, codes
    return codes[0]


def _captions(at) -> str:
    return " ".join(str(c.value) for c in at.caption)


def _declare(at) -> None:
    at.text_input(key=f"inc_prop_exec_{_PICK}_confirm").set_value("DECLARE")
    at.run()
    at.button(key=f"inc_prop_exec_{_PICK}_btn").click()
    at.run()
    assert not at.exception, at.exception


def test_prefix_169_calls_the_4_arg_overload(monkeypatch):
    at, gate_reads, calls = _page(monkeypatch, stash_tip=169, fresh_tip=169)
    assert _preview(at) == _FOUR + ";"
    caps = _captions(at)
    assert _PRE in caps and _POST not in caps and _TASK not in caps
    live = [r for r in gate_reads if r.get("tier") == "live"]
    assert live and all(r["key"] == "schema_gate_fresh" for r in live)
    _declare(at)
    assert calls == [_FOUR + ";"]


def test_a_stale_stash_after_the_apply_calls_the_5_arg_overload(monkeypatch):
    """The reviewer's run: the 4 h metadata entry was cached before the apply, so the stash still says 169."""
    at, gate_reads, calls = _page(monkeypatch, stash_tip=169, fresh_tip=172)
    assert _preview(at) == _FIVE + ";"
    caps = _captions(at)
    assert _POST in caps and _PRE not in caps and _TASK in caps
    assert [r for r in gate_reads if r.get("tier") == "live"]
    _declare(at)
    assert calls == [_FIVE + ";"]


def test_the_steady_state_needs_no_fresh_read(monkeypatch):
    at, gate_reads, calls = _page(monkeypatch, stash_tip=migration_tip(), fresh_tip=1)
    assert _preview(at) == _FIVE + ";"
    assert [r for r in gate_reads if r.get("tier") == "live"] == []
    _declare(at)
    assert calls == [_FIVE + ";"]
