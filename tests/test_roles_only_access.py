"""v4.611.0: roles alone decide who is an OVERWATCH admin (owner instruction 2026-10-07).

The owner: "the hardcoded users like H21427, E22292 and the others need to be removed because their roles
should show who gets access to what". So the username allowlist (and its per-user profile pins and the
account-lever flag that only meant something beside it) are gone: an admin is a DIRECT user member of
config.ADMIN_ACCESS_ROLE (SNOW_PRI_GFR_PRD_ALFA_DSA), found by the live lookup, exact-case, fail closed. There
is no break-glass: while the lookup fails or lists no user, nobody can make an in-app change.

This file locks the removal from four sides: config holds no username-keyed admin or profile list; no app
module names a former admin (KEBARR1 survives only as the ALFA billing override, which is cost attribution,
not access); the runtime has no 'allowlist' source left to honour, in the resolver or the executor; and the
five former named admins, in either case, are admins only when the roster lists them exactly. These five
names appear here as the absence probe and the former-admin-is-not-privileged probe, nowhere else in an
access test.
"""

from __future__ import annotations

import ast
import contextlib
import re

import pytest
import streamlit as st

import app.config as cfg
import app.core.identity as ident
import app.core.query as q
import app.core.session as sess
from app.logic import access_review as ar
from tests._access import script_dsa_roster
from tests._source import ROOT, read

#: The usernames config.OPERATOR_USERS held until v4.611.0. Test-local on purpose: no app module holds them.
_FORMER_NAMED_ADMINS = ("H21427", "E22292", "KEBARR1", "CLROY", "N22514")
#: The admin-route names v4.611.0 deleted from config.
_REMOVED = ("OPERATOR_USERS", "is_operator_user", "VIEWER_PROFILES", "resolve_viewer_profile",
            "ROLE_ADMIN_ACCOUNT_LEVERS")
# H21427, E22292, CLROY, N22514 (former allowlist) and LD8283 (a DSA member) have no place in app code
_NO_USERNAME = re.compile(r"\b(H21427|E22292|CLROY|N22514|LD8283)\b")
_KEBARR1 = re.compile(r"\bKEBARR1\b")
# the ALFA-vs-Trexis COMPANY_SCOPE billing override (owner decision, tests/test_companies.py), not access
_KEBARR1_HOMES = {"app/companies.py", "app/data/cortex_sql.py"}
_GONE_TOKENS = (*_REMOVED, "ALSO_NAMED_ADMIN")
# the current docs (history -- CHANGELOG, migrations, rebuild/02 -- keeps its own words)
_DOCS = ("CLAUDE.md", "README.md", "DEPLOYMENT.md", "RUNBOOK.md", "ARCHITECTURE.md", "FEATURE_GLOSSARY.md",
         "snowflake/validate.sql", "snowflake/rebuild/05_validate.sql")


def _app_files() -> list[str]:
    files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py"))
    return [*files, "streamlit_app.py"]


# ---------------------------------------------------------------------------
# (1) config: no username-keyed admin or profile list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", _REMOVED)
def test_the_username_admin_routes_are_gone_from_config(name):
    assert not hasattr(cfg, name), name


def test_config_holds_no_module_level_user_list():
    tree = ast.parse(read("app/config.py"))
    names = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        names.update(t.id for t in targets if isinstance(t, ast.Name))
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
    assert names, "the scan found nothing: app/config.py moved?"
    assert not [n for n in names if n.upper().endswith("_USERS")], sorted(names)


# ---------------------------------------------------------------------------
# (2) app source: no former admin name, no removed route
# ---------------------------------------------------------------------------
def test_no_app_module_names_a_former_admin():
    hits = [(rel, m.group(0)) for rel in _app_files() for m in _NO_USERNAME.finditer(read(rel))]
    assert not hits, hits
    kebarr1 = {rel for rel in _app_files() if _KEBARR1.search(read(rel))}
    assert kebarr1 == _KEBARR1_HOMES, kebarr1      # the company override only (it is billing, not access)


def test_no_app_module_mentions_a_removed_route():
    hits = [(rel, tok) for rel in _app_files() for tok in _GONE_TOKENS if tok in read(rel)]
    assert not hits, hits


# ---------------------------------------------------------------------------
# (3) the current docs never describe a username route
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", _DOCS)
def test_the_docs_describe_no_username_route(rel):
    text = read(rel)
    assert "OPERATOR_USERS" not in text, rel
    assert "named admin" not in text.lower(), rel


# ---------------------------------------------------------------------------
# (4)-(5) the runtime has no 'allowlist' source to honour
# ---------------------------------------------------------------------------
def test_no_allowlist_source_survives():
    assert sess.ACCESS_SOURCES == ("role", "default", "lookup_failed", "unverified")
    assert "allowlist" not in ar.SOURCE_LABELS
    assert q._OPERATOR_SOURCES_NO_RECHECK == ("off_sis",)
    assert not hasattr(q, "_ACCOUNT_LEVER_REFUSAL") and not hasattr(q, "_ACCOUNT_LEVER_PREFIXES")


def test_the_resolver_has_one_admin_outcome_and_it_is_the_role():
    tree = ast.parse(read("app/core/session.py"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_resolve_identified")
    admin_dicts = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {k.value: v for k, v in zip(node.keys, node.values, strict=True)
                 if isinstance(k, ast.Constant)}
        op = pairs.get("operator")
        if isinstance(op, ast.Constant) and op.value is True:
            admin_dicts.append(pairs)
    assert len(admin_dicts) == 1, len(admin_dicts)
    source = admin_dicts[0].get("source")
    assert isinstance(source, ast.Constant) and source.value == "role"
    referenced = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
        a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not referenced & {"is_operator_user", "resolve_viewer_profile"}, referenced


# ---------------------------------------------------------------------------
# (6) the five former named admins: admins only when the roster lists them exactly
# ---------------------------------------------------------------------------
_CASES = [n for name in _FORMER_NAMED_ADMINS for n in (name, name.lower())]


@pytest.fixture(autouse=True)
def _clean_state():
    st.session_state.clear()
    yield
    st.session_state.clear()


@pytest.mark.parametrize("name", _CASES)
def test_a_former_named_admin_the_roster_omits_is_view_only(monkeypatch, name):
    state = script_dsa_roster(monkeypatch, viewer=name, members=("DSA_PERSON1",))
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "default")
    assert sess.is_operator() is False
    assert state["lookups"] == 1


@pytest.mark.parametrize("name", _CASES)
def test_a_former_named_admin_is_read_only_while_the_lookup_fails(monkeypatch, name):
    script_dsa_roster(monkeypatch, viewer=name, members=(name,),
                      raise_=RuntimeError("Insufficient privileges"))
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("MONITOR", False, "lookup_failed")
    assert sess.is_operator() is False                    # no break-glass


@pytest.mark.parametrize("name", _CASES)
def test_a_former_named_admin_is_an_admin_only_on_an_exact_roster_row(monkeypatch, name):
    script_dsa_roster(monkeypatch, viewer=name, members=(name,))
    a = sess.viewer_access()
    assert (a["profile"], a["operator"], a["source"]) == ("DBA", True, "role")
    other_case = name.lower() if name.isupper() else name.upper()
    script_dsa_roster(monkeypatch, viewer=other_case, members=(name,))
    assert sess.viewer_access()["source"] == "default"      # exact case only: another spelling is no one


@pytest.mark.parametrize("name", _CASES)
def test_a_former_named_admin_off_sis_is_view_only(monkeypatch, name):
    def _no_lookup():
        raise AssertionError("off SiS there is no owner's-rights session to look the role up as")

    monkeypatch.setattr(ident, "viewer_name", lambda: name)
    monkeypatch.setattr(sess, "is_sis", lambda: False)
    monkeypatch.setattr(sess, "_admin_role_rows", _no_lookup)
    assert sess.active_profile("SNOW_SYSADMINS") == "MONITOR"
    assert sess.is_operator() is False


# ---------------------------------------------------------------------------
# (7) a pre-4.611 'allowlist' memo is re-resolved, never reused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("listed", [False, True])
def test_a_stale_allowlist_memo_is_re_resolved_from_the_roster(monkeypatch, listed):
    name = _FORMER_NAMED_ADMINS[0]
    state = script_dsa_roster(monkeypatch, viewer=name, members=(name,) if listed else ("DSA_PERSON1",))
    monkeypatch.setattr(sess, "_clock", lambda: 1_000_000.0)
    st.session_state["_ow_access"] = {"viewer": name, "source": "allowlist", "operator": True,
                                      "profile": "DBA", "at": 1_000_000.0, "error": ""}
    a = sess.viewer_access()
    assert state["lookups"] == 1
    assert a["source"] == ("role" if listed else "default")
    assert a["operator"] is listed and sess.is_operator() is listed


# ---------------------------------------------------------------------------
# (8) the executor refuses a stale 'allowlist' source
# ---------------------------------------------------------------------------
class _Stmt:
    def __init__(self, log: list, sql: str) -> None:
        self.log, self.sql = log, sql

    def collect(self, **_k):
        self.log.append(self.sql)
        return [("OK: done",)]

    def collect_nowait(self, **_k):
        self.log.append(self.sql)


class _Session:
    def __init__(self) -> None:
        self.log: list[str] = []

    def sql(self, sql: str) -> _Stmt:
        return _Stmt(self.log, sql)


def test_the_executor_refuses_a_stale_allowlist_source(monkeypatch):
    s, errs, rechecks = _Session(), [], []

    monkeypatch.setattr(q, "get_session", lambda: s)
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q.st, "spinner", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(q, "record_error",
                        lambda page, exc, context="": errs.append((page, exc, context)) or "r")
    monkeypatch.setattr(sess, "is_operator", lambda: True)
    monkeypatch.setattr(sess, "access_source", lambda: "allowlist")
    # a recording stub, not a raising one: the executor swallows any exception from the re-check and refuses, so
    # a raising stub could not tell "refused outright" from "re-verified, then refused" (review r1)
    monkeypatch.setattr(sess, "reverify_role_admin", lambda: rechecks.append(1) or True)
    ok, msg = q.execute_statement("ALTER USER U SET DISABLED = TRUE", page="Operations")
    assert ok is False and msg == q._ENTITLEMENT_REFUSAL      # not _ROLE_RECHECK_REFUSAL
    assert rechecks == []                                     # an 'allowlist' source is never re-verified
    assert cfg.ADMIN_ACCESS_ROLE in msg and "OPERATOR_USERS" not in msg
    assert s.log == [] and len(errs) == 1 and isinstance(errs[0][1], PermissionError)


# ---------------------------------------------------------------------------
# (9) the owner picker's roster: the last good lookup, never a lookup of its own
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("memo, expected", [
    ({"status": "ok", "users": ("DSA_PERSON1", "other_dsa"), "roles": (), "at": 1.0, "error": "", "fails": 0},
     ("DSA_PERSON1", "other_dsa")),
    ({"status": "lookup_failed", "users": (), "roles": (), "at": 1.0, "error": "x", "fails": 1}, ()),
    ({"status": "unverified", "users": (), "roles": ("R",), "at": 1.0, "error": "x", "fails": 1}, ()),
    (None, ()),
])
def test_admin_roster_users_reads_only_a_good_memo(monkeypatch, memo, expected):
    def _no_lookup():
        raise AssertionError("admin_roster_users never runs a lookup")

    monkeypatch.setattr(sess, "_admin_role_rows", _no_lookup)
    if memo is not None:
        st.session_state["_ow_access_roster"] = memo
    assert sess.admin_roster_users() == expected
