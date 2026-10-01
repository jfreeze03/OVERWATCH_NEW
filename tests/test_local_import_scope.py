"""R2-057: a function-local import binds its name for the WHOLE function, not just its branch.

control_room.render() did `from datetime import timedelta` inside the Pulse section's
`if _activity_ready:` branch. Python then treats `timedelta` as local to all of render(), so the
Timeline & movers drill (a different section; that branch never ran) raised UnboundLocalError on
every row selection, even though the module imports timedelta at the top. The fix removed the
redundant local import.

Behaviour lock: tests/test_control_room_timeline_drill.py (the drill renders on a row selection).
This file is the ratchet, and runs on the floor leg too: no function under app/ loads a name that one of its own NESTED (branch-local) imports
  binds, outside the block of an import of that name that dominates the load. A top-level import in
  the function body covers the rest of the function; anything else must be hoisted to module level
  (or to the top of the function). This is narrower than "no local imports" -- those are fine and
  common -- and it is the exact shape that broke.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _imports_with_blocks(fn: ast.FunctionDef | ast.AsyncFunctionDef):
    """(import node, the statement list holding it, is it in the function's top-level body) for
    every import in fn's OWN scope (nested defs/classes/lambdas are their own scopes)."""
    def visit(stmts, top):
        for s in stmts:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                yield s, stmts, top
            if isinstance(s, _SCOPES):
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(s, field, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    yield from visit(sub, False)
            for h in getattr(s, "handlers", None) or []:
                yield from visit(h.body, False)
            for c in getattr(s, "cases", None) or []:
                yield from visit(c.body, False)
    yield from visit(fn.body, True)


def _binds(scope: ast.AST, name: str) -> bool:
    """Whether a nested scope binds ``name`` itself (then its loads are not the outer local's)."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = scope.args
        if any(p.arg == name for p in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                                        *([a.vararg] if a.vararg else []),
                                        *([a.kwarg] if a.kwarg else [])]):
            return True
    for n in ast.walk(scope):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return True
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                (al.asname or al.name).split(".")[0] == name for al in n.names):
            return True
    return False


def _loads(fn: ast.AST, name: str) -> list[int]:
    """Line numbers where fn's local ``name`` is read: its own scope plus nested scopes that do not
    rebind the name (closures read the enclosing local)."""
    out: list[int] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, _SCOPES) and _binds(n, name):
            continue
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
            out.append(n.lineno)
        stack.extend(ast.iter_child_nodes(n))
    return sorted(set(out))


def undominated_local_import_loads(src: str, label: str = "<src>") -> list[str]:
    """Every load of a name bound by a function-local import that no import of that name dominates
    (an import covers its own block from its line on; a top-level import covers the rest of the
    function). Each hit is an UnboundLocalError waiting for the branch that skips the import."""
    tree = ast.parse(src)
    hits: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cover: dict[str, list[tuple[int, int]]] = {}
        for imp, block, top in _imports_with_blocks(fn):
            end = (fn.end_lineno if top else max(getattr(s, "end_lineno", s.lineno) for s in block)) or imp.lineno
            for al in imp.names:
                cover.setdefault((al.asname or al.name).split(".")[0], []).append((imp.lineno, end))
        for name, spans in cover.items():
            hits.extend(f"{label}:{line} {fn.name}() reads {name!r}, bound only by the local "
                        f"import(s) at {[lo for lo, _ in spans]} that do not reach it"
                        for line in _loads(fn, name) if not any(lo <= line <= hi for lo, hi in spans))
    return hits


def test_no_function_reads_a_name_its_branch_local_import_does_not_reach():
    hits: list[str] = []
    for path in sorted((_ROOT / "app").rglob("*.py")):
        hits += undominated_local_import_loads(path.read_text(encoding="utf-8"),
                                               str(path.relative_to(_ROOT)))
    assert not hits, ("a branch-local import makes its name local to the WHOLE function, so a read "
                      "outside that branch raises UnboundLocalError (R2-057). Hoist the import to "
                      "module level (or the top of the function):\n" + "\n".join(hits))


def test_the_ratchet_has_teeth():
    # the exact R2-057 shape: module import + a branch-local re-import + a read in another branch
    bad = (
        "from datetime import timedelta\n"
        "def render(section, ready):\n"
        "    if section == 'Pulse':\n"
        "        if ready:\n"
        "            from datetime import timedelta\n"
        "            x = timedelta(days=1)\n"
        "    elif section == 'Timeline':\n"
        "        y = timedelta(minutes=30)\n"
    )
    hits = undominated_local_import_loads(bad)
    assert len(hits) == 1 and ":8 render() reads 'timedelta'" in hits[0], hits
    # a closure reading the branch-local name from outside the branch is the same bug
    closure = (
        "def f(ok):\n"
        "    if ok:\n"
        "        import json\n"
        "    def g():\n"
        "        return json.dumps(1)\n"
        "    return g\n"
    )
    assert len(undominated_local_import_loads(closure)) == 1
    # the safe shapes stay quiet: a use inside the import's block, a top-of-function import, the
    # same name re-imported in each branch that reads it, a nested scope with its own import
    ok = (
        "def a(flag):\n"
        "    if flag:\n"
        "        import json\n"
        "        return json.dumps(1)\n"
        "    return None\n"
        "def b(flag):\n"
        "    import json\n"
        "    if flag:\n"
        "        return json.dumps(1)\n"
        "    return json.dumps(2)\n"
        "def c(flag):\n"
        "    if flag:\n"
        "        import json\n"
        "        return json.dumps(1)\n"
        "    else:\n"
        "        import json\n"
        "        return json.dumps(2)\n"
        "def d(flag):\n"
        "    if flag:\n"
        "        import json\n"
        "        json.dumps(1)\n"
        "    def inner():\n"
        "        import json\n"
        "        return json.dumps(3)\n"
        "    return inner\n"
    )
    assert undominated_local_import_loads(ok) == []

