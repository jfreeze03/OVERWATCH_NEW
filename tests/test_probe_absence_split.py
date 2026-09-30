"""v4.605: a failed probe read renders by its failure KIND.

needs_setup ("not installed / not readable by this app") is ONLY for a true absence -- an absent or
unauthorised object ('absent') or an unavailable function ('unknown_function'), app.core.result.is_setup_absence.
A missing column on an existing view ('missing_column') is schema drift, and a 'timeout' or any 'other'
failure is a failed read: each renders empty_state("unavailable", <panel sentence>, detail=<error>), never
needs_setup and never the clean state. The reported bug: Security > Access's admin network-policy panel
routed a missing column to needs_setup ("the policy-reference view isn't readable") through a lumped
_PROBE_ABSENT tuple. query.run(probe=True) still leaves missing_column unlogged (its expected-absence tuple is
unchanged), so no new unavailable sentence may point at the Admin error log.

Render tests with fakes (the tests/test_policy_coverage.py ``_patch`` / ``_failed(kind)`` pattern), source
locks for sites embedded in large functions, and an AST ratchet over app/ui: a branch that turns a failed
read into needs_setup must split on the kind; the remaining NON-probe sites of that class are allowlisted
(file, variable) pairs whose reads carry no probe=True, so the list can only shrink (follow-up PR).
"""

from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.result import SETUP_ABSENCE_KINDS, QueryResult, is_setup_absence
from tests._source import ROOT, read

_SETUP = ("absent", "unknown_function")
_FAILED = ("missing_column", "timeout", "other")


def _ok(df: pd.DataFrame):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", truncated=False,
                           usable=lambda: not df.empty)


def _failed(kind: str):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           truncated=False, usable=lambda: False)


class _FakeSt:
    def __init__(self, toggles_off: tuple[str, ...] = ()):
        self.calls: list[tuple[str, str]] = []
        self.session_state: dict = {}
        self._off = toggles_off

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def markdown(self, text, *_a, **_k):
        self.calls.append(("markdown", str(text)))

    def warning(self, text, *_a, **_k):
        self.calls.append(("warning", str(text)))

    def info(self, text, *_a, **_k):
        self.calls.append(("info", str(text)))

    def toggle(self, *_a, key: str = "", **_k):
        return key not in self._off

    def divider(self):
        self.calls.append(("divider", ""))

    @contextmanager
    def expander(self, *_a, **_k):
        yield self

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _patch(monkeypatch, module, results: dict, **extra):
    """Patch a page module's st / run / empty_state (and the named extras) with recording fakes."""
    fake = _FakeSt()
    seen: dict = {"runs": [], "empty": [], "detail": [], "kpis": [], "tables": [], "guard": 0}

    def fake_run(_sql, *_a, key: str = "", **_kw):
        seen["runs"].append(key)
        return results[key]

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    def fake_guard(*_a, **_k):
        seen["guard"] += 1
        return False

    monkeypatch.setattr(module, "st", fake)
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "empty_state", fake_empty)
    for name, value in {"section_header": lambda *_a, **_k: None, "result_caption": lambda *_a, **_k: None,
                        "kpi_row": lambda items, *_a, **_k: seen["kpis"].append(items),
                        "styled_table": lambda df, *_a, **_k: seen["tables"].append(df),
                        "entity_nav_table": lambda df, *_a, **_k: seen["tables"].append(df),
                        "guard": fake_guard, **extra}.items():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
    return fake, seen


def _one_unavailable(seen: dict, kind: str) -> str:
    ((state, msg),) = seen["empty"]
    assert state == "unavailable", (kind, seen["empty"])
    assert seen["detail"] == [f"boom ({kind})"]
    assert "error log" not in msg.lower()                  # drift stays unlogged on a probe read
    return msg


# ----------------------------------------------------------------------------- the shared rule ----

def test_setup_absence_kinds_are_the_true_absences():
    assert frozenset({"absent", "unknown_function"}) == SETUP_ABSENCE_KINDS
    m = re.search(r"_expected_absence = probe and kind in \(([^)]*)\)", read("app/core/query.py"))
    assert m, "query.run's expected-absence tuple moved"
    run_unlogged = {s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()}
    assert run_unlogged > SETUP_ABSENCE_KINDS                    # a strict subset: drift is unlogged, not setup
    assert "missing_column" in run_unlogged and "missing_column" not in SETUP_ABSENCE_KINDS
    assert is_setup_absence("absent") and is_setup_absence("ABSENT") and is_setup_absence(" unknown_function ")
    for kind in (None, "", "timeout", "missing_column", "other"):
        assert not is_setup_absence(kind), kind
    assert "missing_column | timeout" in read("app/core/result.py")      # the kinds comment names drift


def test_no_lumped_absence_tuple_in_the_ui():
    assert "_PROBE_ABSENT" not in read("app/ui/pages/security.py")
    with_literal = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "app" / "ui").rglob("*.py")
                          if '"missing_column"' in p.read_text(encoding="utf-8"))
    # ai_chargeback: the token panel's documented optional TOKENS_GRANULAR note. attention: the night read's
    # FALLBACK-read kinds (a missing ETA column retries the base roll-up), never a setup or absence claim.
    assert with_literal == ["app/ui/attention.py", "app/ui/pages/cost_parts/ai_chargeback.py"]
    cb = read("app/ui/pages/cost_parts/ai_chargeback.py")
    assert cb.count('"missing_column"') == 2
    panel = cb.split("def _token_economics_panel", 1)[1].split("\ndef ", 1)[0]
    assert panel.count('"missing_column"') == 2


# -------------------------------------------------------------------- Security > Access (#9) ----

def _sec():
    from app.ui.pages import security as sec
    return sec


@pytest.mark.parametrize("kind", _SETUP)
def test_auth_readiness_absent_is_setup(monkeypatch, kind):
    sec = _sec()
    _, seen = _patch(monkeypatch, sec, {})
    sec._render_auth_readiness(_failed(kind), "ALL", evidence_ok=True)
    assert seen["empty"] == [("needs_setup", "Snowflake's USERS or GRANTS_TO_USERS view, or OVERWATCH's login "
                                             "fact, isn't readable by this app here, so password-deprecation "
                                             "readiness can't be classified.")]
    assert not seen["kpis"] and not seen["tables"] and seen["guard"] == 0


@pytest.mark.parametrize("kind", _FAILED)
def test_auth_readiness_failures_are_unavailable(monkeypatch, kind):
    sec = _sec()
    _, seen = _patch(monkeypatch, sec, {})
    sec._render_auth_readiness(_failed(kind), "ALL", evidence_ok=True)
    assert "read failed" in _one_unavailable(seen, kind)
    assert not seen["kpis"] and not seen["tables"] and seen["guard"] == 0


@pytest.mark.parametrize("kind", _SETUP)
def test_admin_netpol_absent_is_setup(monkeypatch, kind):
    sec = _sec()
    _, seen = _patch(monkeypatch, sec, {"admin_netpol_ALL": _failed(kind)})
    sec._render_admin_network_policy("ALL")
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup" and "POLICY_REFERENCES or GRANTS_TO_USERS view isn't readable" in msg
    assert "SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN USER" in msg
    assert not seen["kpis"] and not seen["tables"] and seen["guard"] == 0


@pytest.mark.parametrize("kind", _FAILED)
def test_admin_netpol_failures_are_unavailable(monkeypatch, kind):
    """The reported bug: a missing column (drift) read as 'the view isn't readable here' (needs_setup)."""
    sec = _sec()
    _, seen = _patch(monkeypatch, sec, {"admin_netpol_ALL": _failed(kind)})
    sec._render_admin_network_policy("ALL")
    msg = _one_unavailable(seen, kind)
    assert "admin network-policy read failed" in msg and "SHOW PARAMETERS LIKE 'NETWORK_POLICY'" in msg
    assert all(state != "needs_setup" for state, _ in seen["empty"])
    assert not seen["kpis"] and not seen["tables"] and seen["guard"] == 0


_TAG_CAPTION = "Object-tag coverage needs ACCOUNT_USAGE.TAG_REFERENCES (tag lineage)"


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_tag_panel_split(monkeypatch, kind):
    sec = _sec()
    fake, seen = _patch(monkeypatch, sec, {"tag_probe": _failed(kind)})
    sec._tag_governance_panel("ALL")
    assert seen["runs"] == ["tag_probe"]                          # the coverage read is never issued
    if kind in _SETUP:
        assert _TAG_CAPTION in fake.text("caption") and seen["empty"] == []
    else:
        assert "TAG_REFERENCES check failed" in _one_unavailable(seen, kind)
        assert _TAG_CAPTION not in fake.text("caption")
    assert fake.calls[-1] == ("divider", "")


# ------------------------------------------------------------------- Security decision queue ----

def _sc():
    from app.ui import security_center as sc
    return sc


_V075 = "becomes available after V075 is applied by the Snowflake owner."


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_change_risk_breakdown_split(monkeypatch, kind):
    sc = _sc()
    _, seen = _patch(monkeypatch, sc, {"sec_change_risk_breakdown": _failed(kind)})
    sc._render_change_risk_diagnostic()
    if kind in _SETUP:
        assert seen["empty"] == [("needs_setup", "The change-risk breakdown " + _V075)]
    else:
        assert "FACT_SECURITY_CHANGE" in _one_unavailable(seen, kind)
    assert not seen["kpis"]


def _coverage_frame():
    return pd.DataFrame([{"DOMAIN": "IDENTITY", "COVERAGE_STATUS": "COMPLETE"}])


@pytest.mark.parametrize("queue,coverage", [
    (_failed("timeout"), None),                 # queue timeout, coverage ok
    (_failed("timeout"), _failed("timeout")),   # both timeout (used to read "apply V075")
    (_failed("absent"), None),                  # queue absent, coverage ok
    (_failed("missing_column"), _failed("absent")),
])
def test_security_overview_failed_queue_is_unavailable(monkeypatch, queue, coverage):
    sc = _sc()
    cov = coverage if coverage is not None else _ok(_coverage_frame())
    _, seen = _patch(monkeypatch, sc, {"sec_exception_queue_ALL": queue, "sec_domain_coverage": cov},
                     domain_posture=lambda *_a, **_k: pytest.fail("scored an unread queue"))
    sc.render_security_overview("ALL")
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "V_SECURITY_EXCEPTION_QUEUE" in msg
    assert seen["detail"] == [queue.error]
    assert not seen["kpis"]


def test_security_overview_both_absent_is_the_v075_setup_state(monkeypatch):
    sc = _sc()
    _, seen = _patch(monkeypatch, sc, {"sec_exception_queue_ALL": _failed("absent"),
                                       "sec_domain_coverage": _failed("unknown_function")})
    sc.render_security_overview("ALL")
    assert seen["empty"] == [("needs_setup", "The security decision queue " + _V075)]
    assert not seen["kpis"]


# ----------------------------------------------------------------------- the other page panels ----

@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_query_insights_split(monkeypatch, kind):
    from app.ui.pages import operations as ops
    fake, seen = _patch(monkeypatch, ops, {"query_insights_7": _failed(kind)})
    ops._query_insights_panel()
    if kind in _SETUP:
        assert "isn't available on this account/edition" in fake.text("caption") and seen["empty"] == []
    else:
        assert "QUERY_INSIGHTS view could not be read" in _one_unavailable(seen, kind)
        assert "isn't available" not in fake.text("caption")
    assert not seen["tables"]


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_account_storage_tiers_split(monkeypatch, kind):
    from app.ui.pages.cost_parts import spend
    fake, seen = _patch(monkeypatch, spend, {"stor_acct_30": _failed("absent"),
                                             "stor_acct_live_30": _failed(kind)})
    spend._account_storage_tiers("ALL", 30, {})
    assert seen["runs"] == ["stor_acct_30", "stor_acct_live_30"]   # mart failed -> the live leg decides
    if kind in _SETUP:
        assert "need migration V046" in fake.text("caption") and seen["empty"] == []
    else:
        assert "Account storage by tier could not be read" in _one_unavailable(seen, kind)
        assert "V046" not in fake.text("caption")
    assert not seen["tables"] and not seen["kpis"]


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_blast_radius_edges_split(monkeypatch, kind):
    from app.ui import workbench as wb
    fake, seen = _patch(monkeypatch, wb, {"object_dep_edges": _failed(kind)})
    wb._object_blast_radius_panel("DB.S.T")
    assert seen["runs"] == ["object_dep_edges"]
    if kind in _SETUP:
        assert "Declared object lineage needs" in fake.text("caption") and seen["empty"] == []
    else:
        assert "OBJECT_DEPENDENCIES) could not be read" in _one_unavailable(seen, kind)
        assert "lineage needs" not in fake.text("caption")


_NOT_VISIBLE = "isn't visible to this role"


@pytest.mark.parametrize("kind", (*_SETUP, *_FAILED, "none", "ok_empty"))
def test_org_truth_panel_split(monkeypatch, kind):
    from app.ui.pages.cost_parts import contract
    bal = None if kind == "none" else (_ok(pd.DataFrame()) if kind == "ok_empty" else _failed(kind))
    fake, seen = _patch(monkeypatch, contract, {}, org_balance_result=lambda _page: bal)
    assert contract._org_truth_panel() is False
    caps = fake.text("caption")
    if kind in (*_SETUP, "none"):
        assert _NOT_VISIBLE in caps and seen["empty"] == []
    elif kind == "ok_empty":                     # a readable, empty view is not "not visible"
        assert "Org balance view returned no usable rows — No balance rows visible." in caps
        assert _NOT_VISIBLE not in caps and seen["empty"] == []
    else:
        assert "REMAINING_BALANCE_DAILY) could not be read" in _one_unavailable(seen, kind)
        assert _NOT_VISIBLE not in caps
    assert seen["runs"] == []                     # the org_items read never follows a failure


def test_embedded_probe_sites_split_on_the_kind():
    spend = read("app/ui/pages/cost_parts/spend.py")
    assert "if not na.ok and is_setup_absence(na.error_kind):" in spend
    assert ('empty_state("unavailable", "Snowflake\'s native ANOMALY_INSIGHTS feed could not be read. The '
            'z-score "\n                        "sweep above still runs.", detail=na.error)') in spend
    assert "elif is_setup_absence(_oc.error_kind):" in read("app/ui/pages/cost_parts/optimize.py")
    assert "elif is_setup_absence(_pc.error_kind):" in read("app/ui/pages/cost_parts/unit_costs.py")
    adm = read("app/ui/pages/admin.py")
    assert "elif fh.ok or is_setup_absence(fh.error_kind):" in adm
    recon = adm.split("elif not ai_recon.ok:", 1)[1]
    assert recon.lstrip().startswith('empty_state("unavailable"') and "detail=ai_recon.error" in recon[:300]
    assert "if not ai_recon.ok and is_setup_absence(ai_recon.error_kind):" in adm


# ------------------------------------------------------------------------ Admin > Setup progress ----

def _setup_rows(monkeypatch, results: dict):
    from app.ui.pages import admin
    tables: list[pd.DataFrame] = []
    empties: list[tuple[str, str]] = []
    warnings: list[str] = []

    def _run(_sql, *, key, **_k):
        return results.get(key, QueryResult(ok=True))

    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", lambda kind, msg, *_a, **_k: empties.append((kind, msg)))
    monkeypatch.setattr(admin, "load_settings", lambda _p: {})
    monkeypatch.setattr(admin, "styled_table", lambda df, **_k: tables.append(df))
    monkeypatch.setattr(admin, "st", SimpleNamespace(warning=warnings.append, caption=lambda *_a: None))
    admin._setup_progress_tab()
    return tables[0].set_index("STEP"), empties, warnings


def test_setup_checklist_failed_read_is_unknown_not_pending(monkeypatch):
    rows, empties, warnings = _setup_rows(monkeypatch, {"setup_schema_version": QueryResult(
        ok=False, error="boom (timeout)", error_kind="timeout")})
    mig = rows.loc["Database migrations"]
    assert mig["STATUS"] == "Unknown"
    assert mig["DETAIL"] == "SCHEMA_VERSION could not be read, so which migrations are applied is unknown."
    assert "Run the missing migrations" not in mig["FIX"] and "not a setup gap" in mig["FIX"]
    assert [k for k, _ in empties] == ["unavailable"]           # one unavailable, never clean
    assert "1 setup item(s) could not be checked" in empties[0][1]
    # the ok-but-empty routes read is a readable table with no route, never "not readable"
    assert rows.loc["Alert routes configured", "DETAIL"] == "0 enabled route(s)"
    assert rows.loc["Marts loading", "DETAIL"] == "SOURCE_FRESHNESS_STATE is empty — have the loader tasks run?"
    # Unknown never counts as pending
    pending = int(rows["STATUS"].isin(("Pending", "Partial")).sum())
    assert warnings == [f"{pending} setup item(s) still pending — see the FIX column."]


def test_setup_checklist_absent_schema_version_is_still_pending(monkeypatch):
    rows, empties, _ = _setup_rows(monkeypatch, {
        "setup_schema_version": QueryResult(ok=False, error="boom (absent)", error_kind="absent"),
        "setup_freshness": QueryResult(ok=False, error="boom (timeout)", error_kind="timeout"),
        "setup_routes": QueryResult(ok=False, error="boom (other)", error_kind="other")})
    mig = rows.loc["Database migrations"]
    assert mig["STATUS"] == "Pending" and "nothing applied yet" in mig["DETAIL"]
    assert rows.loc["Marts loading", "STATUS"] == "Unknown"
    assert "could not be read" in rows.loc["Marts loading", "DETAIL"]
    assert rows.loc["Alert routes configured", "STATUS"] == "Unknown"
    assert rows.loc["Alert routes configured", "DETAIL"] == "ALERT_ROUTES could not be read, so the routes are unknown."
    assert ("unavailable", "2 setup item(s) could not be checked: a read failed, which is not a setup gap. "
                           "Retry.") in empties
    assert all(k != "clean" for k, _ in empties)


# ------------------------------------------------------------------------- the AST ratchet ----

# The NON-probe `if not X.ok -> needs_setup` class, left for a follow-up PR: errors ARE logged (run() without
# probe=True records every failure), but the panel still says "not installed". (file, variable) pairs; every
# pair must still be flagged (fixing one removes it here) and its read must carry no probe=True (a probe read
# of this shape is in scope and must split on the kind now).
_NON_PROBE_ALLOWLIST = frozenset({
    ("app/ui/workbench.py", "base"),
    ("app/ui/workbench.py", "detail"),
    ("app/ui/workbench.py", "record"),
    ("app/ui/workbench.py", "changes"),
    ("app/ui/pages/alerts.py", "prec"),
    ("app/ui/pages/control_room.py", "res"),
    ("app/ui/pages/cost_parts/contract.py", "org_m"),
    ("app/ui/pages/cost_parts/contract.py", "model_m"),
    ("app/ui/pages/cost_parts/contract.py", "res"),
    ("app/ui/pages/cost_parts/compare.py", "pat"),
    ("app/ui/pages/cost_parts/optimize.py", "res"),
    ("app/ui/pages/operations.py", "res"),
    ("app/ui/pages/overview.py", "actions_res"),
    ("app/ui/pages/brief.py", "events"),
    ("app/ui/pages/brief.py", "actions"),
    ("app/ui/pages/admin.py", "res"),
    ("app/ui/pages/admin.py", "fq"),
    ("app/ui/decision_studio.py", "result"),
})


def _read_vars(test: ast.expr, *, negated: bool) -> set[str]:
    """Names X whose `X.ok` / `X.usable()` the test reads -- under a `not` (negated=True) or bare (False)."""
    out: set[str] = set()
    under_not: set[int] = set()
    for n in ast.walk(test):
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            under_not.add(id(n.operand))
    for n in ast.walk(test):
        name = None
        if isinstance(n, ast.Attribute) and n.attr == "ok" and isinstance(n.value, ast.Name):
            name = n.value.id
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "usable"
              and isinstance(n.func.value, ast.Name)):
            name = n.func.value.id
        if name and (id(n) in under_not) == negated:
            out.add(name)
    return out


def _splits_on_kind(test: ast.expr) -> bool:
    return any((isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "is_setup_absence")
               or (isinstance(n, ast.Attribute) and n.attr == "error_kind") for n in ast.walk(test))


def _calls_setup_state(body: list[ast.stmt]) -> bool:
    for stmt in body:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        fn = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
        if fn == "_setup_state":
            return True
        kind = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "kind"), None)
        if fn == "empty_state" and isinstance(kind, ast.Constant) and kind.value == "needs_setup":
            return True
    return False


def _setup_on_failure_sites(tree: ast.Module) -> list[tuple[str, ast.If]]:
    """(variable, if-node) for every branch that renders needs_setup on a failed read without the kind split."""
    sites: list[tuple[str, ast.If]] = []
    elifs = {id(n.orelse[0]) for n in ast.walk(tree)
             if isinstance(n, ast.If) and len(n.orelse) == 1 and isinstance(n.orelse[0], ast.If)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # rule 1: `if/elif not X.ok ...:` whose direct body is needs_setup must split on the kind in its test
        if _calls_setup_state(node.body) and not _splits_on_kind(node.test):
            sites.extend((v, node) for v in sorted(_read_vars(node.test, negated=True)))
        if id(node) in elifs:
            continue
        # rule 2: a trailing `else: needs_setup` after a chain that tests `X.ok` (so the else is the failed
        # read) must be preceded by an elif that splits on the kind
        chain = [node]
        while len(chain[-1].orelse) == 1 and isinstance(chain[-1].orelse[0], ast.If):
            chain.append(chain[-1].orelse[0])
        if chain[-1].orelse and _calls_setup_state(chain[-1].orelse) and not any(
                _splits_on_kind(c.test) for c in chain[1:]):
            tested = set().union(*(_read_vars(c.test, negated=False) for c in chain))
            sites.extend((v, node) for v in sorted(tested))
    return sites


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.AST:
    best: ast.AST = tree
    for fn in ast.walk(tree):
        if (isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and any(n is target for n in ast.walk(fn))
                and (best is tree or any(n is fn for n in ast.walk(best)))):
            best = fn
    return best


def _probe_true(node: ast.AST) -> bool:
    return any(isinstance(k, ast.keyword) and k.arg == "probe" and isinstance(k.value, ast.Constant)
               and k.value.value is True for k in ast.walk(node))


def test_no_probe_failure_reads_as_setup_without_the_kind_split():
    flagged: dict[tuple[str, str], list[ast.If]] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[rel] = tree
        for var, node in _setup_on_failure_sites(tree):
            flagged.setdefault((rel, var), []).append(node)
    assert set(flagged) - _NON_PROBE_ALLOWLIST == set(), (
        "a failed read renders needs_setup without splitting on the kind (is_setup_absence(X.error_kind)): "
        f"{sorted(set(flagged) - _NON_PROBE_ALLOWLIST)}")
    assert _NON_PROBE_ALLOWLIST - set(flagged) == set(), (
        f"fixed -- remove from the allowlist: {sorted(_NON_PROBE_ALLOWLIST - set(flagged))}")
    for (rel, var), nodes in flagged.items():
        for node in nodes:
            fn = _enclosing_function(trees[rel], node)
            assigns = [a for a in ast.walk(fn) if isinstance(a, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == var for t in a.targets)]
            assert assigns, (rel, var, node.lineno)
            assert not any(_probe_true(a.value) for a in assigns), (
                f"{rel}:{node.lineno} {var} is a probe=True read -- split it on the kind, don't allowlist it")


def test_the_ratchet_catches_the_pre_v4605_shapes():
    """The scanner itself: the v4.604 lumped shapes are flagged, the v4.605 split shapes are not."""
    before = ast.parse(
        "def a():\n    if not res.ok:\n        _setup_state('x')\n        return\n"
        "def b():\n    if x.ok and not x.empty:\n        pass\n    elif x.ok:\n        pass\n"
        "    else:\n        empty_state('needs_setup', 'm')\n"
        "def c():\n    if not t.ok:\n        empty_state('unavailable', 'm')\n    else:\n"
        "        empty_state('needs_setup', 'ok-but-empty')\n")
    assert sorted(v for v, _ in _setup_on_failure_sites(before)) == ["res", "x"]
    after = ast.parse(
        "def a():\n    if not res.ok and is_setup_absence(res.error_kind):\n        _setup_state('x')\n"
        "def b():\n    if x.ok and not x.empty:\n        pass\n    elif x.ok:\n        pass\n"
        "    elif is_setup_absence(x.error_kind):\n        empty_state('needs_setup', 'm')\n"
        "    else:\n        empty_state('unavailable', 'm')\n")
    assert _setup_on_failure_sites(after) == []


def test_ratchet_scans_every_ui_module():
    files = {p.name for p in (ROOT / "app" / "ui").rglob("*.py")}
    assert {"security.py", "security_center.py", "workbench.py", "ai_chargeback.py"} <= files
    assert Path(ROOT / "app" / "core" / "result.py").is_file()
