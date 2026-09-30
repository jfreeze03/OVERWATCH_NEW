"""The shared schema gate (Next-Fifty wave 4, spec section 2.1): app/ui/schema_gate.py.

Every wave-4 read of a new column and every caption that claims a new behaviour asks
``has_migration(n, page)`` first, so a deploy that lands before the owner's apply keeps the pre-apply
behaviour. These tests pin the contract the later slices build on:
- unreadable / empty / junk -> not applied (the safe side);
- the read is the startup floor gate's SQL at the startup gate's tier, so it is the same run() cache entry;
- one SCHEMA_VERSION answer per full script run, normally the startup gate's own (no second statement,
  so tests/test_usage_sim.py first-paint budgets do not move);
- the page harnesses stub the module and model the gate's hand-off.
"""

from __future__ import annotations

import ast
from types import SimpleNamespace

import pandas as pd
import pytest

import app.main as m
from app.core.result import QueryResult
from app.data import mart_sql
from app.ui import schema_gate
from tests._source import ROOT, migration_tip, read

_READS = {"run", "run_batch", "run_batch_mixed", "run_mart_first", "execute_statement"}


def _versions(*values) -> QueryResult:
    return QueryResult(df=pd.DataFrame({"VERSION": list(values)}), ok=True)


def _state(monkeypatch, state: dict) -> dict:
    monkeypatch.setattr(schema_gate, "st", SimpleNamespace(session_state=state))
    return state


def _recorder(monkeypatch, module, res: QueryResult) -> list[dict]:
    calls: list[dict] = []

    def _run(sql, **kw):
        calls.append({"sql": sql, **kw})
        return res

    monkeypatch.setattr(module, "run", _run)
    return calls


# --- unreadable -> not applied --------------------------------------------------------------------
@pytest.mark.parametrize("res", [
    QueryResult(ok=False, error="SQL compilation error: Object does not exist"),
    QueryResult(ok=True),                                                    # no frame at all
    QueryResult(df=pd.DataFrame({"VERSION": []}), ok=True),                  # table empty
    QueryResult(df=pd.DataFrame({"OTHER": [154]}), ok=True),                 # no VERSION column
    _versions("x", None, float("nan"), 154.5, 0, -154),                      # junk only
])
def test_unreadable_empty_or_junk_is_not_applied(monkeypatch, res):
    _state(monkeypatch, {})
    _recorder(monkeypatch, schema_gate, res)
    assert schema_gate.applied_versions("Brief") == set()
    assert schema_gate.has_migration(154, "Brief") is False


def test_applied_versions_parse_like_deploy_health(monkeypatch):
    _state(monkeypatch, {})
    _recorder(monkeypatch, schema_gate, _versions(152, 153.0, "154", " 155 ", "junk", None))
    assert schema_gate.applied_versions("Brief") == {152, 153, 154, 155}
    assert schema_gate.has_migration(154, "Brief") is True
    assert schema_gate.has_migration(156, "Brief") is False


# --- the same cache entry as the startup gate -----------------------------------------------------
def test_the_read_is_the_startup_gates_sql_and_tier(monkeypatch):
    """run() caches by (tier, row-capped SQL, scope); page and key are telemetry-only. Same SQL, same tier
    and the same (default) row cap means the gate's fallback read lands on the startup gate's entry."""
    _state(monkeypatch, {})
    shell = _recorder(monkeypatch, m, _versions(1, 2))
    gate = _recorder(monkeypatch, schema_gate, _versions(1, 2))
    m._schema_floor_breach()
    schema_gate.has_migration(2, "Control Room")
    assert len(shell) == 1 and len(gate) == 1
    assert shell[0]["sql"] == gate[0]["sql"] == mart_sql.schema_version()
    assert shell[0]["tier"] == gate[0]["tier"] == schema_gate.TIER == "metadata"
    assert shell[0].get("max_rows") == gate[0].get("max_rows") is None       # both on the default cap
    assert gate[0]["page"] == "Control Room"


# --- one answer per full script run ---------------------------------------------------------------
def test_the_startup_gate_read_answers_the_whole_run(monkeypatch):
    state = _state(monkeypatch, {"_ow_run_seq": 7})
    _recorder(monkeypatch, m, _versions(*range(1, 155)))
    gate = _recorder(monkeypatch, schema_gate, _versions(*range(1, 166)))
    assert m._schema_floor_breach() is None                                  # at/above the floor
    assert state[schema_gate.STASH_KEY] == (7, frozenset(range(1, 155)))
    assert schema_gate.has_migration(154, "Brief") is True
    assert schema_gate.has_migration(162, "Brief") is False
    assert schema_gate.has_migration(165, "Overview") is False
    assert gate == []                                                        # no statement of its own
    state["_ow_run_seq"] = 8                                                 # the next full run
    assert schema_gate.has_migration(165, "Admin") is True                   # Admin: no floor gate ran
    assert schema_gate.has_migration(162, "Admin") is True
    assert len(gate) == 1                                                    # one read, then stashed


def test_a_failed_read_is_asked_once_per_run(monkeypatch):
    """st.cache_data never caches a failure, so without the stash every has_migration call in a run
    would re-issue (and log) a failing statement (the admin._read_fresh_applied r2 lesson)."""
    state = _state(monkeypatch, {"_ow_run_seq": 3})
    gate = _recorder(monkeypatch, schema_gate, QueryResult(ok=False, error="warehouse suspended"))
    assert schema_gate.has_migration(162, "Brief") is False
    assert schema_gate.has_migration(163, "Brief") is False
    assert len(gate) == 1
    state["_ow_run_seq"] = 4
    schema_gate.has_migration(162, "Brief")
    assert len(gate) == 2


def test_the_startup_gate_failing_still_answers_not_applied_without_a_retry(monkeypatch):
    _state(monkeypatch, {"_ow_run_seq": 5})
    _recorder(monkeypatch, m, QueryResult(ok=False, error="boom"))
    gate = _recorder(monkeypatch, schema_gate, _versions(165))
    assert m._schema_floor_breach() is None                                  # the floor gate fails open
    assert schema_gate.has_migration(165, "Brief") is False                  # ... and the run says not applied
    assert gate == []


@pytest.mark.parametrize("stash", [(6, frozenset({165})), "junk", (7,), (7, {165}), None])
def test_a_stale_or_malformed_stash_is_ignored(monkeypatch, stash):
    _state(monkeypatch, {"_ow_run_seq": 7, schema_gate.STASH_KEY: stash})
    gate = _recorder(monkeypatch, schema_gate, _versions(1))
    assert schema_gate.has_migration(165, "Brief") is False
    assert len(gate) == 1


def test_without_a_run_sequence_nothing_is_stashed(monkeypatch):
    """Unit-test stubs and bare mode have no _ow_run_seq: every call reads, nothing is written."""
    state = _state(monkeypatch, {})
    gate = _recorder(monkeypatch, schema_gate, _versions(154))
    assert schema_gate.remember(_versions(1, 2)) == frozenset({1, 2})
    assert schema_gate.has_migration(154, "Brief") is True
    assert schema_gate.has_migration(154, "Brief") is True
    assert len(gate) == 2 and state == {}


def test_the_startup_gate_hands_off_before_it_can_return():
    body = read("app/main.py").split("def _schema_floor_breach()", 1)[1].split("\ndef ", 1)[0]
    handoff = body.index("schema_gate.remember(res)")
    assert body.index("res = run(mart_sql.schema_version()") < handoff < body.index("return")
    assert "from app.ui import schema_gate" in read("app/main.py")


def test_control_room_v154_caption_rides_the_shared_gate():
    cr = read("app/ui/pages/control_room.py")
    body = cr.split("def _v154_applied()", 1)[1].split("\ndef ", 1)[0]
    assert "return has_migration(154, _PAGE)" in body
    assert "run(" not in body                                                # no private SCHEMA_VERSION read
    assert "cr_incident_loop_migver" not in cr


# --- the page harnesses stub it ---------------------------------------------------------------------
def _module_level_readers() -> set[str]:
    """app/ui modules that import a read entry point from app.core.query at module level."""
    out = set()
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if (isinstance(node, ast.ImportFrom) and node.module == "app.core.query"
                    and {a.asname or a.name for a in node.names} & _READS):
                out.add(".".join(path.relative_to(ROOT).with_suffix("").parts))
    return out


def _fixture_modules(rel: str, fixture: str) -> set[str]:
    """Full names of the modules a harness fixture loops its read stubs over (``for module in (...)``)."""
    fn = next(n for n in ast.parse(read(rel)).body if isinstance(n, ast.FunctionDef) and n.name == fixture)
    names: dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
        elif isinstance(node, ast.Import):
            names.update({a.asname or a.name: a.name for a in node.names})
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple)
                and getattr(n.target, "id", "") == "module")
    return {names[e.id] for e in loop.iter.elts if isinstance(e, ast.Name)}


def test_every_ui_module_with_a_module_level_read_is_stubbed_by_the_page_harnesses():
    """A module the harnesses do not patch reads for real under AppTest: the shaped render loses its
    populated branches and tests/usage_sim.py undercounts. attention.py set the precedent; schema_gate.py
    joins it, and so must the next app/ui module that imports run at module level."""
    readers = _module_level_readers()
    assert "app.ui.schema_gate" in readers and "app.ui.attention" in readers
    import usage_sim
    _main, modules = usage_sim._patched_modules()
    assert readers - {mod.__name__ for mod in modules} == set()
    assert readers - _fixture_modules("tests/test_pages_shaped.py", "_stub_shaped") == set()
    smoke = _fixture_modules("tests/test_pages_apptest.py", "_stub_runtime")
    assert {"app.ui.schema_gate", "app.ui.attention"} <= smoke


def test_the_harnesses_model_the_gate_hand_off_at_the_repo_tip(monkeypatch):
    """Both page harnesses bypass the floor check but keep the hand-off, so has_migration() is not a
    counted read in the simulator (production answers it from the gate's own read) and the shaped render
    takes the post-apply branches."""
    import test_pages_shaped
    assert "_schema_floor_breach\", _floor_gate_at_repo_tip)" in read("tests/test_pages_shaped.py")
    assert "_schema_floor_breach\", _floor_gate_at_repo_tip)" in read("tests/usage_sim.py")
    state = _state(monkeypatch, {"_ow_run_seq": 1})
    gate = _recorder(monkeypatch, schema_gate, _versions(1))
    assert test_pages_shaped._floor_gate_at_repo_tip() is None
    assert state[schema_gate.STASH_KEY] == (1, frozenset(range(1, migration_tip() + 1)))
    assert schema_gate.has_migration(migration_tip(), "Brief") is True
    assert gate == []
