"""v4.605: a failed probe read renders by its failure KIND.

needs_setup ("not installed / not readable by this app") is ONLY for a true absence -- an absent or
unauthorised object ('absent'), an "Insufficient privileges" error on an existing one ('privilege') or an
unavailable function ('unknown_function'), app.core.result.is_setup_absence. A 'privilege' error proves the object
exists, so run() logs it on a probe read and every consumer of the literal 'absent' (a legitimate zero) reads it as a
failed read (review r2 R2-1).
A missing column on an existing view ('missing_column') is schema drift, and a 'timeout' or any 'other'
failure is a failed read: each renders empty_state("unavailable", <panel sentence>, detail=<error>), never
needs_setup and never the clean state. The reported bug: Security > Access's admin network-policy panel
routed a missing column to needs_setup ("the policy-reference view isn't readable") through a lumped
_PROBE_ABSENT tuple. query.run(probe=True) still leaves missing_column unlogged (its expected-absence tuple is
unchanged), so no new unavailable sentence may point at the Admin error log.

Render tests with fakes (the tests/test_policy_coverage.py ``_patch`` / ``_failed(kind)`` pattern), source
locks for sites embedded in large functions, and an AST ratchet over app/ui: a branch that turns a failed
read into needs_setup must split on the kind -- a setup-absence check on that read's own error_kind gating the
branch (review R1-12: a mere mention of error_kind, a lumped tuple, `!=` or a negated check is not a split); the
remaining NON-probe sites of that class are allowlisted exactly -- (file, variable) -> (function, callee), one
site each -- and their reads carry no probe=True, directly or through a same-module wrapper, so the list can
only shrink (follow-up PR; review R1-14).
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

_SETUP = ("absent", "privilege", "unknown_function")
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
    assert frozenset({"absent", "privilege", "unknown_function"}) == SETUP_ABSENCE_KINDS
    m = re.search(r"_expected_absence = probe and kind in \(([^)]*)\)", read("app/core/query.py"))
    assert m, "query.run's expected-absence tuple moved"
    run_unlogged = {s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()}
    assert run_unlogged == {"absent", "unknown_function", "missing_column"}
    # drift is unlogged but not setup; a privilege error is setup (needs_setup) but LOGGED: the object exists
    assert run_unlogged - SETUP_ABSENCE_KINDS == {"missing_column"}
    assert SETUP_ABSENCE_KINDS - run_unlogged == {"privilege"}
    assert is_setup_absence("absent") and is_setup_absence("ABSENT") and is_setup_absence(" unknown_function ")
    assert is_setup_absence(" Privilege ")
    for kind in (None, "", "timeout", "missing_column", "other"):
        assert not is_setup_absence(kind), kind
    assert "missing_column | timeout" in read("app/core/result.py")      # the kinds comment names drift


def test_insufficient_privileges_is_a_setup_absence(monkeypatch):
    """Review R1-17: format_snowflake_error rewrites "Insufficient privileges" and "does not exist or not
    authorized" to the same setup advice guard() routes to needs_setup, so both are setup absences -- one failure,
    one state, whichever helper renders it. Review r2 R2-1: but a privilege error proves the object exists, so it
    is its own kind ('privilege'), never the literal 'absent' a legitimate-zero consumer reads."""
    from app.core.errors import format_snowflake_error
    from app.core.query import _classify_error
    from app.core.result import is_privilege_error
    from app.ui import components
    raw = Exception("003001 (42501): SQL access control error: Insufficient privileges to operate on table 'X'")
    gone = Exception("002003 (02000): SQL compilation error: Object 'X' does not exist or not authorized.")
    assert _classify_error(raw) == "privilege" and _classify_error(gone) == "absent"
    assert is_setup_absence(_classify_error(raw)) and is_setup_absence(_classify_error(gone))
    assert is_privilege_error(_classify_error(raw)) and not is_privilege_error(_classify_error(gone))
    for kind in (None, "", "absent", "unknown_function", "missing_column", "timeout", "other"):
        assert not is_privilege_error(kind), kind
    assert format_snowflake_error(raw) == format_snowflake_error(gone)
    states: list[str] = []
    monkeypatch.setattr(components, "empty_state", lambda kind, *_a, **_k: states.append(kind))
    for exc in (raw, gone):
        res = QueryResult(ok=False, error=format_snowflake_error(exc), error_kind=_classify_error(exc))
        assert components.guard(res, "no rows") is False
    assert states == ["needs_setup", "needs_setup"]


def test_a_privilege_error_is_logged_and_never_a_legitimate_zero(monkeypatch):
    """Review r2 R2-1: while 003001 classified 'absent', a probe read left it unlogged and every consumer of the
    literal 'absent' failed OPEN for an object that provably exists -- the health score dropped that source's
    penalty (no Incomplete), a declared canary gap read GAP instead of FAIL. As its own kind it is logged and
    each of those consumers reads it as a failed read again; a 002003 keeps its old behaviour."""
    import app.core.query as q
    from app.logic.scoring import degraded_sources
    logged: list[str] = []
    monkeypatch.setattr(q, "_telemetry", lambda *_a, **_k: None)
    monkeypatch.setattr(q, "record_error", lambda _page, exc, **_k: logged.append(str(exc)))
    errors = {"priv": "003001 (42501): SQL access control error: Insufficient privileges to operate on table 'X'",
              "gone": "002003 (02000): SQL compilation error: Object 'X' does not exist or not authorized."}
    kinds = {}
    for name, text in errors.items():
        def _boom(_sql, _scope, _page, _text=text):
            raise RuntimeError(_text)
        monkeypatch.setitem(q._FETCHERS, "recent", _boom)
        res = q.run(f"SELECT 1 /* {name} */", page="T", key=name, tier="recent", probe=True)
        assert not res.ok
        kinds[name] = res.error_kind
    assert kinds == {"priv": "privilege", "gone": "absent"}
    assert logged == [errors["priv"]]                            # only the privilege error reaches APP_ERROR_LOG
    assert degraded_sources({"owner-queue": QueryResult(ok=False, error_kind="privilege"),
                             "freshness": QueryResult(ok=False, error_kind="absent")}) == {"owner-queue"}
    # the literal-'absent' consumers stay literal, so 'privilege' takes their failed-read path
    assert '_gap = (res.error_kind in ("absent", "unknown_function")' in read("app/ui/pages/admin.py")
    assert 'getattr(_bt_hist, "error_kind", "") != "absent"' in read("app/ui/pages/overview.py")


def test_schema_drift_is_the_missing_column_kind():
    from app.core.result import is_schema_drift
    assert is_schema_drift("missing_column") and is_schema_drift(" MISSING_COLUMN ")
    for kind in (None, "", "absent", "unknown_function", "timeout", "other"):
        assert not is_schema_drift(kind), kind


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


_CRITICAL_IDENTITY = pd.DataFrame([{"SEVERITY": "CRITICAL", "DOMAIN": "IDENTITY", "TITLE": "t", "IMPACT_COUNT": 3}])


@pytest.mark.parametrize("kind", (*_FAILED, "privilege"))
@pytest.mark.parametrize("queue_rows", [False, True])
def test_security_overview_failed_coverage_is_unavailable(monkeypatch, kind, queue_rows):
    """Review R1-16: the queue resolved but the coverage read failed -- an unavailable state with the error, and
    an empty queue never claims "coverage is not complete" for a contract that was never read. Review r2 R2-1: an
    "Insufficient privileges" error on the contract is a failed read too (the view exists), never silence."""
    sc = _sc()
    queue = _ok(_CRITICAL_IDENTITY if queue_rows else pd.DataFrame())
    _, seen = _patch(monkeypatch, sc, {"sec_exception_queue_ALL": queue, "sec_domain_coverage": _failed(kind)},
                     _render_change_risk_diagnostic=lambda: None, stash_section_count=lambda *_a, **_k: None,
                     selectable_table=lambda *_a, **_k: None)
    sc.render_security_overview("ALL")
    failed = [(i, m) for i, (state, m) in enumerate(seen["empty"]) if state == "unavailable"]
    ((i, msg),) = failed
    assert "security domain coverage contract could not be read" in msg and "error log" not in msg.lower()
    assert seen["detail"][i] == f"boom ({kind})"
    assert all("not complete" not in m for _, m in seen["empty"])
    assert all(k["value"] == "--" for k in seen["kpis"][0])                  # scores withheld, never a 100
    if not queue_rows:
        assert any(state == "no_data_yet" and "coverage contract could not be read" in m
                   for state, m in seen["empty"])


@pytest.mark.parametrize("coverage,signal", [
    (_failed("timeout"), True), (_failed("missing_column"), True), (_failed("other"), True),
    (_failed("privilege"), True), (_failed("absent"), False), (None, False)])
def test_security_verdict_names_an_unread_coverage_contract(monkeypatch, coverage, signal):
    """Review R1-16: with coverage unread no domain can reach 'need action', so a CRITICAL finding read as a plain
    'Watch'; the verdict now says the coverage could not be read."""
    sc = _sc()
    cov = coverage if coverage is not None else _ok(pd.DataFrame([{"DOMAIN": "IDENTITY", "COVERAGE": "COMPLETE"}]))
    _patch(monkeypatch, sc, {"sec_exception_queue_ALL": _ok(_CRITICAL_IDENTITY), "sec_domain_coverage": cov})
    verdict = sc.security_posture_verdict("ALL")
    assert ("domain coverage could not be read, so no domain is scored" in verdict["body"]) is signal
    assert verdict["level"] == ("bad" if coverage is None else "warn")      # control: a readable contract


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


_DEP_EDGES = pd.DataFrame({"REFERENCED_FQN": ["DB.S.T"], "REFERENCING_FQN": ["DB.S.V"],
                           "REFERENCING_DOMAIN": ["VIEW"]})
_ENTERPRISE = "Enterprise-only) is unavailable"


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_blast_radius_root_consumers_split(monkeypatch, kind):
    """Review R1-13: no declared dependent -> the object's own observed consumers. Any failure of that
    ACCESS_HISTORY read used to say "Enterprise-only ... unavailable here" (this account is Enterprise)."""
    from app.ui import workbench as wb
    fake, seen = _patch(monkeypatch, wb, {
        "object_dep_edges": _ok(pd.DataFrame({"REFERENCED_FQN": ["DB.S.OTHER"], "REFERENCING_FQN": ["DB.S.V"]})),
        "object_blast_root_DB.S.T": _failed(kind)})
    wb._object_blast_radius_panel("DB.S.T")
    assert seen["runs"] == ["object_dep_edges", "object_blast_root_DB.S.T"]
    failed = [(i, m) for i, (state, m) in enumerate(seen["empty"]) if state == "unavailable"]
    if kind in _SETUP:
        assert "Observed-consumer evidence (ACCESS_HISTORY, " + _ENTERPRISE in fake.text("caption")
        assert failed == []
    else:
        ((i, msg),) = failed
        assert "Observed consumers (ACCESS_HISTORY) could not be read" in msg and "error log" not in msg.lower()
        assert seen["detail"][i] == f"boom ({kind})"
        assert "Enterprise" not in fake.text("caption")


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_blast_radius_consumers_split(monkeypatch, kind):
    """Review R1-13: the declared half still renders; the observed half says WHY it is unmeasured by kind, and a
    non-absence failure is a red unavailable with its error."""
    from app.ui import workbench as wb
    fake, seen = _patch(monkeypatch, wb, {"object_dep_edges": _ok(_DEP_EDGES),
                                          "object_blast_cons_DB.S.T": _failed(kind)})
    wb._object_blast_radius_panel("DB.S.T")
    (kpis,) = seen["kpis"]
    observed = next(k for k in kpis if k["label"] == "Observed in last 30d")
    assert observed["value"] == "n/a" and len(seen["tables"]) == 1          # the declared half still renders
    failed = [(i, m) for i, (state, m) in enumerate(seen["empty"]) if state == "unavailable"]
    if kind in _SETUP:
        assert _ENTERPRISE + " on this account/role" in observed["help"]
        assert _ENTERPRISE + " on this account/role" in fake.text("caption")
        assert failed == []
    else:
        why = {"timeout": "the ACCESS_HISTORY read timed out", "missing_column": "hit schema drift",
               "other": "the ACCESS_HISTORY read failed"}[kind]
        assert why in observed["help"] and why in fake.text("caption")
        assert "Enterprise" not in observed["help"] and "Enterprise" not in fake.text("caption")
        ((i, msg),) = failed
        assert "Observed consumers (ACCESS_HISTORY) could not be read" in msg and "error log" not in msg.lower()
        assert seen["detail"][i] == f"boom ({kind})"


def test_observed_unmeasured_reason_by_kind():
    from app.logic.lineage import observed_unmeasured_reason as why
    assert why("absent") == why(" UNKNOWN_FUNCTION ") == (
        "ACCESS_HISTORY (Enterprise-only) is unavailable on this account/role")
    assert why("timeout") == "the ACCESS_HISTORY read timed out"
    assert why("missing_column") == "the ACCESS_HISTORY read hit schema drift (a column this build reads is missing)"
    assert why("other") == why("") == why(None) == "the ACCESS_HISTORY read failed"


@pytest.mark.parametrize("kind", (*_SETUP, *_FAILED, "ok_empty", "ok_rows"))
def test_product_consumer_reads_split(monkeypatch, kind):
    """Review R1-13: Proof's retirement board said "Consumer reach needs Enterprise ACCESS_HISTORY, which isn't
    available here" for any failed consumer read."""
    from app.ui import decision_studio as ds
    fake, seen = _patch(monkeypatch, ds, {})
    res = ({"ok_empty": _ok(pd.DataFrame()), "ok_rows": _ok(pd.DataFrame({"DATA_PRODUCT": ["P"]}))}.get(kind)
           or _failed(kind))
    ds._consumer_reads_state(res)
    caps = fake.text("caption")
    if kind in _SETUP:
        assert "Consumer reach needs Enterprise ACCESS_HISTORY" in caps and seen["empty"] == []
    elif kind == "ok_empty":
        assert "ACCESS_HISTORY returned no reads for mapped products" in caps and seen["empty"] == []
    elif kind == "ok_rows":
        assert caps == "" and seen["empty"] == []
    else:
        assert "Consumer reads (ACCESS_HISTORY) could not be read" in _one_unavailable(seen, kind)
        assert "Enterprise" not in caps
    body = read("app/ui/decision_studio.py").split("def _products(", 1)[1].split("\ndef ", 1)[0]
    assert "_consumer_reads_state(reads)" in body and "if not reads.ok:" not in body


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

def _setup_rows(monkeypatch, results: dict, details: list | None = None):
    from app.ui.pages import admin
    tables: list[pd.DataFrame] = []
    empties: list[tuple[str, str]] = []
    warnings: list[str] = []
    seen_details = details if details is not None else []

    def _run(_sql, *, key, **_k):
        return results.get(key, QueryResult(ok=True))

    def _empty(kind, msg, *_a, **k):
        empties.append((kind, msg))
        seen_details.append(k.get("detail"))

    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", _empty)
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
    details: list = []
    rows, empties, _ = _setup_rows(monkeypatch, {
        "setup_schema_version": QueryResult(ok=False, error="boom (absent)", error_kind="absent"),
        "setup_freshness": QueryResult(ok=False, error="boom (timeout)", error_kind="timeout"),
        "setup_routes": QueryResult(ok=False, error="boom (other)", error_kind="other")}, details)
    mig = rows.loc["Database migrations"]
    assert mig["STATUS"] == "Pending" and "nothing applied yet" in mig["DETAIL"]
    assert rows.loc["Marts loading", "STATUS"] == "Unknown"
    assert "could not be read" in rows.loc["Marts loading", "DETAIL"]
    assert rows.loc["Alert routes configured", "STATUS"] == "Unknown"
    assert rows.loc["Alert routes configured", "DETAIL"] == "ALERT_ROUTES could not be read, so the routes are unknown."
    assert ("unavailable", "2 setup item(s) could not be checked: a read failed, which is not a setup gap. "
                           "Retry.") in empties
    assert all(k != "clean" for k, _ in empties)
    # review R1-15: the failed reads' errors (never the absent one) are the Error detail
    assert details[[k for k, _ in empties].index("unavailable")] == ("SOURCE_FRESHNESS_STATE: boom (timeout)\n"
                                                                    "ALERT_ROUTES: boom (other)")


def test_setup_checklist_privilege_error_is_unknown_not_nothing_applied(monkeypatch):
    """Review r2 R2-1: "Insufficient privileges" on SCHEMA_VERSION proves the table exists, so the row is Unknown
    with a failed-read detail and a grants FIX, never Pending 'nothing applied yet'; its error is in the Error
    detail, and a retry is never offered for it."""
    details: list = []
    rows, empties, _ = _setup_rows(monkeypatch, {
        "setup_schema_version": QueryResult(ok=False, error="boom (privilege)", error_kind="privilege"),
        "setup_freshness": QueryResult(ok=False, error="boom (privilege)", error_kind="privilege"),
        "setup_routes": QueryResult(ok=False, error="boom (privilege)", error_kind="privilege")}, details)
    mig = rows.loc["Database migrations"]
    assert mig["STATUS"] == "Unknown" and "nothing applied yet" not in mig["DETAIL"]
    assert mig["DETAIL"] == "SCHEMA_VERSION could not be read, so which migrations are applied is unknown."
    for step in ("Database migrations", "Marts loading", "Alert routes configured"):
        row = rows.loc[step]
        assert row["STATUS"] == "Unknown", step
        assert row["FIX"].startswith("Insufficient privileges: the object exists"), step
        assert "roles.sql" in row["FIX"] and "a retry will not clear it" in row["FIX"] and "Retry." not in row["FIX"]
    assert rows.loc["Marts loading", "DETAIL"] == ("SOURCE_FRESHNESS_STATE could not be read, so whether the marts "
                                                   "are loading is unknown.")
    assert empties == [("unavailable", "3 setup item(s) could not be checked: a read failed, which is not a setup "
                                       "gap. An 'Insufficient privileges' error needs the app's grants re-applied "
                                       "(roles.sql), which a retry will not clear: see the FIX column.")]
    assert details == ["SCHEMA_VERSION: boom (privilege)\nSOURCE_FRESHNESS_STATE: boom (privilege)\n"
                       "ALERT_ROUTES: boom (privilege)"]


def test_setup_checklist_drift_is_not_a_retry(monkeypatch):
    """Review R1-15: a missing column is schema drift -- a retry never clears it -- and its error, left unlogged
    by the probe read, is the summary's Error detail."""
    details: list = []
    rows, empties, _ = _setup_rows(monkeypatch, {
        "setup_schema_version": QueryResult(ok=False, error="boom (missing_column)", error_kind="missing_column"),
        "setup_freshness": QueryResult(ok=False, error="boom (missing_column)", error_kind="missing_column"),
        "setup_routes": QueryResult(ok=False, error="boom (timeout)", error_kind="timeout")}, details)
    for step in ("Database migrations", "Marts loading"):
        row = rows.loc[step]
        assert row["STATUS"] == "Unknown" and "Retry" not in row["FIX"], step
        assert row["FIX"].startswith("Schema drift: a column this build reads is missing.")
        assert "a retry will not clear it" in row["FIX"]
    assert rows.loc["Alert routes configured", "FIX"] == "Retry. The read failed; this is not a setup gap."
    assert empties == [("unavailable", "3 setup item(s) could not be checked: a read failed, which is not a setup "
                                       "gap. A missing column is schema drift, which a retry will not clear: see "
                                       "the FIX column.")]
    assert details == ["SCHEMA_VERSION: boom (missing_column)\nSOURCE_FRESHNESS_STATE: boom (missing_column)\n"
                       "ALERT_ROUTES: boom (timeout)"]


# ------------------------------------------------------------------------- the AST ratchet ----

# The NON-probe `if not X.ok -> needs_setup` class, left for a follow-up PR: errors ARE logged (run() without
# probe=True records every failure), but the panel still says "not installed". Review R1-14: keyed EXACTLY --
# (file, variable) -> (enclosing function, callee of the read) -- and each pair must map to exactly ONE flagged
# branch, so a second un-split site cannot hide behind a generic name ('res') and a new read through a wrapper
# needs its own allowlist edit. Every pair must still be flagged (fixing one removes it here) and its read must
# carry no probe=True, directly or through a same-module wrapper (a probe read of this shape is in scope and
# must split on the kind now). "or-run" = a `prefetched or run(...)` fallback.
_NON_PROBE_ALLOWLIST: dict[tuple[str, str], tuple[str, str]] = {
    ("app/ui/workbench.py", "base"): ("render_action_center", "run"),
    ("app/ui/workbench.py", "detail"): ("_render_data_product_detail", "run"),
    ("app/ui/workbench.py", "record"): ("render_entity_360", "run"),
    ("app/ui/workbench.py", "changes"): ("render_entity_360", "run"),
    ("app/ui/pages/alerts.py", "prec"): ("render", "run"),
    ("app/ui/pages/control_room.py", "res"): ("_freshness_board", "run_mart_first"),
    ("app/ui/pages/cost_parts/contract.py", "org_m"): ("_rate_card_reconciliation", "run"),
    ("app/ui/pages/cost_parts/contract.py", "model_m"): ("_rate_card_reconciliation", "run"),
    ("app/ui/pages/cost_parts/contract.py", "res"): ("_org_accounts_spend", "run"),
    ("app/ui/pages/cost_parts/compare.py", "pat"): ("_compare_tab", "_get"),
    ("app/ui/pages/cost_parts/optimize.py", "res"): ("_savings_tab", "run"),
    ("app/ui/pages/operations.py", "res"): ("_pipeline_data_checks", "run"),
    ("app/ui/pages/brief.py", "events"): ("render", "or-run"),
    ("app/ui/pages/brief.py", "actions"): ("render", "or-run"),
    ("app/ui/decision_studio.py", "result"): ("_products", "run"),
}


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


def _kind_var(node: ast.AST) -> str | None:
    """X for an `X.error_kind` read."""
    if isinstance(node, ast.Attribute) and node.attr == "error_kind" and isinstance(node.value, ast.Name):
        return node.value.id
    return None


# 'absent' ("does not exist or not authorized") and 'privilege' ("Insufficient privileges") are the one
# not-authorized family: a literal split must keep them together, or a lost grant renders as a red failed read
# on one panel and needs_setup on the next (review r3: four panels kept the pre-'privilege' tuple).
_NOT_AUTHORIZED = frozenset({"absent", "privilege"})


def _closed_kinds(kinds: set[object]) -> bool:
    """A non-empty subset of SETUP_ABSENCE_KINDS that holds both not-authorized kinds or neither."""
    return (bool(kinds) and kinds <= SETUP_ABSENCE_KINDS
            and (not kinds & _NOT_AUTHORIZED or kinds >= _NOT_AUTHORIZED))


def _absence_subset(node: ast.AST) -> bool:
    """SETUP_ABSENCE_KINDS itself, or a literal tuple / set / list of constant kinds that _closed_kinds accepts."""
    if isinstance(node, ast.Name | ast.Attribute):
        return (node.id if isinstance(node, ast.Name) else node.attr) == "SETUP_ABSENCE_KINDS"
    return (isinstance(node, ast.Tuple | ast.Set | ast.List) and bool(node.elts)
            and all(isinstance(e, ast.Constant) for e in node.elts)
            and _closed_kinds({e.value for e in node.elts}))


def _absence_check(node: ast.AST, *, negative: bool = False) -> str | None:
    """X when `node` checks X.error_kind against the setup-absence kinds.

    positive (negative=False): true ONLY for a subset of SETUP_ABSENCE_KINDS -- is_setup_absence(X.error_kind),
    `X.error_kind == <absence kind>`, `X.error_kind in <subset literal | SETUP_ABSENCE_KINDS>`, or an `or` of such
    checks on one X. negative=True: the complement -- true for EVERY kind outside such a subset (`not <positive>`,
    `!=`, `not in`). A literal kind or kind set must hold 'absent' and 'privilege' together or neither
    (_closed_kinds), so `== "absent"` alone is not a split (review r3). Anything else -- a lumped
    `in _PROBE_ABSENT`, a literal with 'timeout' or 'missing_column', a positive check under a `not` -- is not a
    split (review R1-12)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _absence_check(node.operand, negative=not negative)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "is_setup_absence"
            and len(node.args) == 1 and not node.keywords):
        return None if negative else _kind_var(node.args[0])
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if _kind_var(left) is None and isinstance(op, ast.Eq | ast.NotEq):
            left, right = right, left                                   # "absent" == X.error_kind
        var = _kind_var(left)
        if var is None:
            return None
        if isinstance(op, ast.Eq | ast.NotEq):
            fits = isinstance(right, ast.Constant) and _closed_kinds({right.value})   # so never "absent" alone
            return var if fits and isinstance(op, ast.NotEq if negative else ast.Eq) else None
        if isinstance(op, ast.In | ast.NotIn):
            return var if _absence_subset(right) and isinstance(op, ast.NotIn if negative else ast.In) else None
        return None
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and not negative:
        found = {_absence_check(v) for v in node.values}
        return found.pop() if len(found) == 1 and None not in found else None
    return None


def _conjuncts(test: ast.expr) -> list[ast.expr]:
    return list(test.values) if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]


def _kind_read_vars(test: ast.expr) -> set[str]:
    return {v for n in ast.walk(test) if (v := _kind_var(n))}


def _split_vars(test: ast.expr) -> set[str]:
    """X whose branch is reached only by a setup absence: a positive absence check on X.error_kind that is the
    test itself or one of its top-level `and` conjuncts (so an `or` can never widen it to a timeout)."""
    return {v for c in _conjuncts(test) if (v := _absence_check(c))}


def _routes_failures_away(test: ast.expr) -> set[str]:
    """X for an elif that sends every NON-absence kind of X elsewhere, so the trailing `else:` sees only a setup
    absence: a negative absence check that is the whole test, an `or` operand, or an `and` conjunct beside only
    `not X.ok` reads."""
    parts = (list(test.values) if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test])
    out = {v for p in parts if (v := _absence_check(p, negative=True))}
    conj = _conjuncts(test)
    if len(conj) > 1:
        for i, c in enumerate(conj):
            v = _absence_check(c, negative=True)
            if v and all(_read_vars(o, negated=True) == {v} and not _kind_read_vars(o)
                         for o in conj[:i] + conj[i + 1:]):
                out.add(v)
    return out


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
        # rule 1: an `if/elif` whose direct body is needs_setup, testing `not X.ok` or reading X.error_kind, must
        # route only a setup absence of X there (a positive absence check on X as a required conjunct)
        if _calls_setup_state(node.body):
            wanted = _read_vars(node.test, negated=True) | _kind_read_vars(node.test)
            sites.extend((v, node) for v in sorted(wanted - _split_vars(node.test)))
        if id(node) in elifs:
            continue
        # rule 2: a trailing `else: needs_setup` after a chain that tests `X.ok` or X.error_kind (so the else
        # is the failed read) must be preceded by an elif that sends every non-absence kind of X elsewhere
        chain = [node]
        while len(chain[-1].orelse) == 1 and isinstance(chain[-1].orelse[0], ast.If):
            chain.append(chain[-1].orelse[0])
        if chain[-1].orelse and _calls_setup_state(chain[-1].orelse):
            tested = set().union(*(_read_vars(c.test, negated=False) | _kind_read_vars(c.test) for c in chain))
            routed = set().union(*(_routes_failures_away(c.test) for c in chain[1:]))
            sites.extend((v, node) for v in sorted(tested - routed))
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


def _callee(value: ast.expr) -> str:
    """The read's callee as the allowlist names it: 'run', 'run_mart_first', a wrapper's name, or 'or-run' for
    a `prefetched or run(...)` fallback."""
    if isinstance(value, ast.Call):
        return ast.unparse(value.func)
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and isinstance(value.values[-1], ast.Call):
        return "or-" + ast.unparse(value.values[-1].func)
    return ast.unparse(value)


def _wrapper_probe(tree: ast.Module, callee: str) -> bool:
    """A same-module wrapper (module-level or nested def) named by the callee whose body passes probe=True."""
    name = callee.removeprefix("or-")
    return any(isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name == name and _probe_true(fn)
               for fn in ast.walk(tree))


def _flagged_ui_sites() -> tuple[dict[tuple[str, str], list[ast.If]], dict[str, ast.Module]]:
    flagged: dict[tuple[str, str], list[ast.If]] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[rel] = tree
        for var, node in _setup_on_failure_sites(tree):
            flagged.setdefault((rel, var), []).append(node)
    return flagged, trees


def test_no_probe_failure_reads_as_setup_without_the_kind_split():
    flagged, trees = _flagged_ui_sites()
    assert set(flagged) - set(_NON_PROBE_ALLOWLIST) == set(), (
        "a failed read renders needs_setup without splitting on the kind (is_setup_absence(X.error_kind)): "
        f"{sorted(set(flagged) - set(_NON_PROBE_ALLOWLIST))}")
    assert set(_NON_PROBE_ALLOWLIST) - set(flagged) == set(), (
        f"fixed -- remove from the allowlist: {sorted(set(_NON_PROBE_ALLOWLIST) - set(flagged))}")
    for (rel, var), nodes in flagged.items():
        want_fn, want_callee = _NON_PROBE_ALLOWLIST[(rel, var)]
        assert len({id(n) for n in nodes}) == 1, (
            f"{rel} {var}: {len(nodes)} un-split needs_setup branches at lines "
            f"{sorted(n.lineno for n in nodes)} -- the allowlist covers exactly one; split the new one on the kind")
        node = nodes[0]
        fn = _enclosing_function(trees[rel], node)
        assert getattr(fn, "name", "<module>") == want_fn, (rel, var, node.lineno, getattr(fn, "name", None))
        assigns = [a for a in ast.walk(fn) if isinstance(a, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == var for t in a.targets)]
        assert assigns, (rel, var, node.lineno)
        assert {_callee(a.value) for a in assigns} == {want_callee}, (
            rel, var, node.lineno, sorted({_callee(a.value) for a in assigns}))
        assert not any(_probe_true(a.value) for a in assigns) and not _wrapper_probe(trees[rel], want_callee), (
            f"{rel}:{node.lineno} {var} is a probe=True read -- split it on the kind, don't allowlist it")


# The real pre-v4.605 shapes (review R1-12): v4.604 security.py's lumped tuple, a literal that lumps 'timeout',
# an inverted `!=`, a negated split, an `or` that widens the split, a lumped kind read with no `.ok`, and a
# trailing else fed by a lumped elif. Each must be flagged on its own variable.
_PRE_V4605 = '''
_PROBE_ABSENT = ("absent", "missing_column", "unknown_function")
_LUMPED = (*SETUP_ABSENCE_KINDS, "missing" + "_column", "timeout")
_FAILED = ("missing_column", "timeout", "other")
def netpol():
    npc = run("x", key="admin_netpol", probe=True)
    if not npc.ok and npc.error_kind in _PROBE_ABSENT:
        empty_state("needs_setup", "the policy-reference view isn't readable")
        return
def auth():
    if not auth.ok and auth.error_kind in _LUMPED:
        empty_state("needs_setup", "m")
def lumped_literal():
    if not a.ok and a.error_kind in ("absent", "unknown_function", "timeout"):
        empty_state("needs_setup", "m")
def inverted():
    if not b.ok and b.error_kind != "absent":
        empty_state("needs_setup", "m")
def negated_split():
    if not c.ok and not is_setup_absence(c.error_kind):
        _setup_state("m")
def widened():
    if not d.ok or is_setup_absence(d.error_kind):
        empty_state(kind="needs_setup", message="m")
def kind_only():
    if e.error_kind in _LUMPED:
        empty_state("needs_setup", "m")
def not_in_lumped():
    if not g.ok and g.error_kind not in ("timeout",):
        empty_state("needs_setup", "m")
def absent_only_tuple():
    if not k.ok and k.error_kind in ("absent", "unknown_function"):
        empty_state("needs_setup", "m")
def absent_only_eq():
    if not m.ok and m.error_kind == "absent":
        empty_state("needs_setup", "m")
def absent_only_kind():
    if n.error_kind == "absent":
        empty_state("needs_setup", "m")
def absent_only_else():
    if q.ok:
        pass
    elif not q.ok and q.error_kind != "absent":
        empty_state("unavailable", "m")
    else:
        empty_state("needs_setup", "m")
def trailing_else():
    if f.ok:
        pass
    elif f.error_kind in _FAILED:
        empty_state("unavailable", "m")
    else:
        empty_state("needs_setup", "m")
'''

# Every split shape the app uses today (negative controls): none may be flagged.
_V4605_SPLITS = '''
def s1():
    if not r.ok and r.error_kind in ("absent", "privilege", "unknown_function"):
        empty_state("needs_setup", "m")
def s2():
    if not r.ok and r.error_kind == "unknown_function":
        empty_state("needs_setup", "m")
def s3():
    if not r.ok and is_setup_absence(r.error_kind):
        _setup_state("m")
def s4():
    if (not queue.ok and not coverage.ok and is_setup_absence(queue.error_kind)
            and is_setup_absence(coverage.error_kind)):
        _setup_state("m")
def s5():
    if blk.error_kind in SETUP_ABSENCE_KINDS:
        empty_state("needs_setup", "m")
def s6():
    if not r.ok and r.error_kind in SETUP_ABSENCE_KINDS:
        empty_state("needs_setup", "m")
def s7():
    if x.ok and not x.empty:
        pass
    elif x.ok:
        pass
    elif is_setup_absence(x.error_kind):
        empty_state("needs_setup", "m")
    else:
        empty_state("unavailable", "m")
def s8():
    if g.ok:
        pass
    elif not is_setup_absence(g.error_kind):
        empty_state("unavailable", "m")
    else:
        empty_state("needs_setup", "m")
def s9():
    if h.ok:
        pass
    elif not h.ok and h.error_kind not in ("absent", "privilege"):
        empty_state("unavailable", "m")
    else:
        empty_state("needs_setup", "m")
def s10():
    if not live_res.ok and (live_res.error_kind == "unknown_function" or is_setup_absence(live_res.error_kind)):
        empty_state("needs_setup", "m")
'''


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


def test_the_ratchet_flags_a_lumped_kind_split():
    """Review R1-12: mentioning X.error_kind is not a split -- the kind must be checked against the setup-absence
    kinds, as a required conjunct, not under a `not`."""
    flagged = sorted(v for v, _ in _setup_on_failure_sites(ast.parse(_PRE_V4605)))
    assert flagged == ["a", "auth", "b", "c", "d", "e", "f", "g", "k", "m", "n", "npc", "q"]
    assert _setup_on_failure_sites(ast.parse(_V4605_SPLITS)) == []


def test_the_allowlist_is_exact_per_site():
    """Review R1-14: a second un-split `res` site in an allowlisted file, or a probe read through a same-module
    wrapper, is not hidden by the (file, variable) pair."""
    # c09 R1-175 split admin.py's last un-split `res` site (_migrations_tab), so the allowlisted-site half of
    # this pair is now a stand-in module of the same shape rather than admin.py's source.
    extra = ast.parse((
        "def _allowlisted_site():\n    res = run('SELECT 0', key='site')\n"
        "    if not res.ok:\n        empty_state('needs_setup', 'Not installed yet.')\n"
        "\n\ndef _mutant_admin():\n    res = run('SELECT 1', key='mutant')\n"
        "    if not res.ok:\n        empty_state('needs_setup', 'Not installed yet.')\n"))
    nodes = [n for v, n in _setup_on_failure_sites(extra) if v == "res"]
    assert len({id(n) for n in nodes}) == 2                   # the exact-one assertion above fails on this
    wrapped = ast.parse("def _optional(sql):\n    return run(sql, probe=True)\n"
                        "def panel():\n    res = _optional('x')\n")
    assert _wrapper_probe(wrapped, "_optional") and not _wrapper_probe(wrapped, "run")
    assert _callee(ast.parse("x = cache.get('k') or run('q')").body[0].value) == "or-run"


def test_ratchet_scans_every_ui_module():
    files = {p.name for p in (ROOT / "app" / "ui").rglob("*.py")}
    assert {"security.py", "security_center.py", "workbench.py", "ai_chargeback.py"} <= files
    assert Path(ROOT / "app" / "core" / "result.py").is_file()
