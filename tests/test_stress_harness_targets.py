"""The opt-in stress harness cannot rot silently (R1-284).

tests/test_stress.py only runs under OW_STRESS=1 (`make stress`), which CI never sets. Its autouse fixture
monkeypatched `app.main.execute_statement` / `execute_statement_async` with raising=True after app.main
stopped binding either name, so EVERY test in it errored at setup, and `_render_page` called
`at.session_state.get(...)`, which AppTest's proxy does not have. Nobody noticed. This always-on check
reads the harness statically: every unconditional `monkeypatch.setattr(<module>, "<name>", ...)` in its
fixture must name an attribute that exists today, and no AppTest session_state `.get(` may come back.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = (_ROOT / "tests" / "test_stress.py").read_text(encoding="utf-8")


def _fixture() -> ast.FunctionDef:
    tree = ast.parse(_SRC)
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_stub_runtime")


def _aliases(fn: ast.FunctionDef) -> dict[str, str]:
    """name -> dotted module for the fixture's local imports (`import a.b as x`, `from a import b`)."""
    out: dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _unconditional_setattrs(fn: ast.FunctionDef) -> list[tuple[str, str]]:
    """(module alias, attribute) for each setattr NOT nested in an `if` / loop (those are hasattr-guarded)."""
    found = []
    for stmt in fn.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "setattr"
                    and isinstance(call.args[0], ast.Name) and isinstance(call.args[1], ast.Constant)):
                found.append((call.args[0].id, call.args[1].value))
    return found


def test_every_unconditional_stub_target_exists():
    fn = _fixture()
    aliases = _aliases(fn)
    targets = _unconditional_setattrs(fn)
    assert ("query_mod", "execute_statement") in targets and ("query_mod", "execute_statement_async") in targets
    missing = []
    for alias, attr in targets:
        module = importlib.import_module(aliases[alias])
        if not hasattr(module, attr):
            missing.append(f"{aliases[alias]}.{attr}")
    assert not missing, f"test_stress's fixture patches names that no longer exist (raising=True errors): {missing}"


def test_harness_never_calls_get_on_the_apptest_session_state():
    assert "at.session_state.get(" not in _SRC
