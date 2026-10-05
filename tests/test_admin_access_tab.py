"""v4.610.0 Admin ▸ Access (owner decision 2026-10-05).

The tab answers "who can open OVERWATCH, and who can change things in it":
  * the viewer's own resolved access (profile, operator, how it was decided, when);
  * the SNOW_PRI_GFR_PRD_ALFA_DSA lookup status -- OK with its USER count, failed, or unverified (an EMPTY
    answer is a privilege gap and never renders as a clean "no members");
  * 'Re-check now', which clears only the clicking viewer's own session memo (and says so);
  * the DSA direct user members, and its ROLE grantees flagged 'not expanded' (they are NOT admins);
  * the app's USAGE grantees (SHOW GRANTS ON STREAMLIT, read-only) against the four expected roles, with the
    same rule as snowflake/roles.sql's -20011/-20012 proof block;
  * plain guidance.
Read-only apart from the memo clear: the tab issues no write.
"""

from __future__ import annotations

import ast
import re

import pandas as pd
import pytest
import streamlit as st

import app.config as cfg
import app.core.errors as errors_mod
import app.core.identity as ident
import app.core.session as sess
from app.core.result import QueryResult
from app.data import access_sql
from app.logic import access_review as ar
from tests._source import read

_ADMIN = cfg.OPERATOR_USERS[0]
_DSA_USER = "DSA_PERSON1"
_DTI_USER = "DTI_PERSON1"
_APP = f"{cfg.OVERWATCH_DB}.{cfg.CORE_SCHEMA}.{cfg.APP_STREAMLIT_NAME}"


def _role_rows(*pairs: tuple[str, str]) -> list[dict]:
    """SHOW GRANTS OF ROLE rows: (granted_to, grantee_name)."""
    return [{"created_on": "2026-10-05", "role": cfg.ADMIN_ACCESS_ROLE, "granted_to": kind,
             "grantee_name": name, "granted_by": "SNOW_ACCOUNTADMINS"} for kind, name in pairs]


def _grant_frame(*rows: tuple[str, str, str]) -> pd.DataFrame:
    """SHOW GRANTS ON STREAMLIT rows (privilege, granted_to, grantee_name), with SHOW's lower-case columns."""
    return pd.DataFrame([{"created_on": "2026-10-05", "privilege": p, "granted_on": "STREAMLIT",
                          "name": _APP, "granted_to": k, "grantee_name": n, "grant_option": "false",
                          "granted_by": "SNOW_ACCOUNTADMINS"} for p, k, n in rows])


def _four_roles_frame() -> pd.DataFrame:
    return _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                        *[("USAGE", "ROLE", r) for r in cfg.APP_ACCESS_ROLES])


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------
def test_show_grants_on_app_builder_names_the_deployed_streamlit():
    assert access_sql.show_grants_on_app_sql() == f"SHOW GRANTS ON STREAMLIT {_APP}"
    # the same object snowflake.yml deploys and roles.sql grants USAGE on
    assert "name: OVERWATCH_APP" in read("snowflake.yml")
    assert f"GRANT USAGE ON STREAMLIT {_APP} TO ROLE" in read("snowflake/roles.sql")


def test_the_builder_is_a_named_canary_exemption():
    from tests.test_canary_coverage import CANARY_EXEMPT
    assert "show_grants_on_app_sql" in CANARY_EXEMPT["access_sql"]
    assert "show_grants_on_app_sql" in read("app/data/canary.py")      # the house comment beside :92-97


# ---------------------------------------------------------------------------
# app_grant_review: the roles.sql proof-block rule, in Python
# ---------------------------------------------------------------------------
def test_grant_review_four_expected_roles_is_ok():
    r = ar.app_grant_review(_four_roles_frame())
    assert r["status"] == "ok"
    assert r["present"] == cfg.APP_ACCESS_ROLES
    assert r["missing"] == () and r["unexpected"] == ()
    assert r["owners"] == ("SNOW_ACCOUNTADMINS",)
    assert ar.EXPECTED_APP_GRANTEES == cfg.APP_ACCESS_ROLES


def test_grant_review_flags_a_missing_role_after_a_redeploy():
    frame = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                         ("USAGE", "ROLE", "SNOW_ACCOUNTADMINS"), ("USAGE", "ROLE", "SNOW_SYSADMINS"))
    r = ar.app_grant_review(frame)
    assert r["status"] == "drift"
    assert r["missing"] == (cfg.ADMIN_ACCESS_ROLE, cfg.VIEW_ACCESS_ROLE)
    assert r["unexpected"] == ()


def test_grant_review_flags_every_grantee_outside_the_four():
    # mirrors the -20011 rule: a USAGE row is allowed only when granted_to = ROLE AND the name is one of the four,
    # so a DATABASE_ROLE (or a share / application role) spelled like an access role is still unexpected
    frame = pd.concat([_four_roles_frame(),
                       _grant_frame(("USAGE", "ROLE", "PUBLIC"),
                                    ("USAGE", "DATABASE_ROLE", cfg.VIEW_ACCESS_ROLE))], ignore_index=True)
    r = ar.app_grant_review(frame)
    assert r["status"] == "drift" and r["missing"] == ()
    assert r["unexpected"] == (f"DATABASE_ROLE {cfg.VIEW_ACCESS_ROLE}", "ROLE PUBLIC")


def test_grant_review_ignores_non_usage_privileges_for_the_verdict():
    frame = pd.concat([_four_roles_frame(), _grant_frame(("OWNERSHIP", "ROLE", "SOMEONE_ELSE"))],
                      ignore_index=True)
    r = ar.app_grant_review(frame)
    assert r["status"] == "ok"
    assert r["owners"] == ("SNOW_ACCOUNTADMINS", "SOMEONE_ELSE")


def test_grant_review_empty_answer_is_unverified_never_clean():
    for empty in (pd.DataFrame(), [], None):
        r = ar.app_grant_review(empty)
        assert r["status"] == "empty"
        assert r["present"] == () and r["missing"] == cfg.APP_ACCESS_ROLES


def test_grant_review_accepts_rows_mappings_and_quoted_upper_keys():
    class _Row:
        def __init__(self, d):
            self._d = d

        def asDict(self):  # Snowpark's spelling
            return dict(self._d)

    rows = [_Row({'"privilege"': "USAGE", '"granted_to"': "ROLE", '"grantee_name"': r})
            for r in cfg.APP_ACCESS_ROLES]
    assert ar.app_grant_review(rows)["status"] == "ok"
    upper = [{"PRIVILEGE": "usage", "GRANTED_TO": "role", "GRANTEE_NAME": r.lower()} for r in cfg.APP_ACCESS_ROLES]
    assert ar.app_grant_review(upper)["status"] == "ok"


def test_grant_review_table_says_what_each_role_means_and_what_to_do():
    frame = _grant_frame(*[("USAGE", "ROLE", r) for r in cfg.APP_ACCESS_ROLES[:3]], ("USAGE", "ROLE", "PUBLIC"))
    table = ar.app_grant_review(frame)["table"]
    by = {row["GRANTEE"]: row for row in table}
    assert set(by) == {*cfg.APP_ACCESS_ROLES, "PUBLIC"}
    assert by[cfg.VIEW_ACCESS_ROLE]["HAS_USAGE"] == "No" and "roles.sql" in by[cfg.VIEW_ACCESS_ROLE]["STATUS"]
    assert by["PUBLIC"]["EXPECTED"] == "No" and "REVOKE" in by["PUBLIC"]["STATUS"]
    assert by[cfg.ADMIN_ACCESS_ROLE]["STATUS"] == "OK"
    assert "admin" in by[cfg.ADMIN_ACCESS_ROLE]["MEANS"].lower()
    assert "view-only" in by[cfg.VIEW_ACCESS_ROLE]["MEANS"].lower()
    for role in cfg.APP_ACCESS_ROLES:
        assert ar.ROLE_MEANING[role]


def test_ownership_is_read_from_the_answer_never_hardcoded():
    # OWNERSHIP implies every privilege, so the owning role opens the app with or without an explicit USAGE
    # grant; which role owns it is whatever SHOW returned, not an assumption about SNOW_ACCOUNTADMINS
    for role in cfg.APP_ACCESS_ROLES:
        assert "owns" not in ar.ROLE_MEANING[role].lower(), role
    frame = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_SYSADMINS"),
                         *[("USAGE", "ROLE", r) for r in cfg.APP_ACCESS_ROLES])
    by = {row["GRANTEE"]: row for row in ar.app_grant_review(frame)["table"]}
    assert "owns the app" in by["SNOW_SYSADMINS"]["MEANS"].lower()
    assert "owns the app" not in by["SNOW_ACCOUNTADMINS"]["MEANS"].lower()


def test_an_owner_without_explicit_usage_is_missing_but_still_opens_the_app():
    frame = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                         *[("USAGE", "ROLE", r) for r in cfg.APP_ACCESS_ROLES[1:]])
    r = ar.app_grant_review(frame)
    assert r["status"] == "drift" and r["missing"] == ("SNOW_ACCOUNTADMINS",)   # roles.sql requires the grant
    by = {row["GRANTEE"]: row for row in r["table"]}
    assert "still opens" in by["SNOW_ACCOUNTADMINS"]["STATUS"].lower()
    assert "roles.sql" in by["SNOW_ACCOUNTADMINS"]["STATUS"]


# ---------------------------------------------------------------------------
# roles.sql's proof block and app_grant_review are ONE rule (review d6c62b87 #1)
# ---------------------------------------------------------------------------
_PROOF_GRANTEE_LIST = re.compile(r'"grantee_name"\s+(NOT\s+)?IN\s*\(([^)]*)\)', re.IGNORECASE)
_PROOF_ANY_KIND = re.compile(r'''COUNT_IF\(\s*"privilege"\s*=\s*'USAGE'\s+AND\s+NOT\s*\(\s*"granted_to"\s*=\s*'''
                             r'''\s*'ROLE'\s+AND\s+"grantee_name"\s+IN\b''', re.IGNORECASE)


def _streamlit_proof_block(roles_sql: str) -> str:
    blocks = [b for b in re.findall(r"EXECUTE IMMEDIATE \$\$(.*?)\$\$;", roles_sql, re.DOTALL)
              if "SHOW GRANTS ON STREAMLIT" in b]
    assert len(blocks) == 1, "roles.sql must hold exactly one Streamlit-grant proof block"
    return blocks[0]


def _proof_block_problems(roles_sql: str) -> list[str]:
    """Why roles.sql's Streamlit proof block is NOT the rule app_grant_review applies ([] when it is):
    every grantee IN-list is exactly EXPECTED_APP_GRANTEES, the 'bad' count is the any-kind form
    NOT (granted_to = 'ROLE' AND grantee_name IN (...)) (a DATABASE_ROLE / APPLICATION_ROLE / SHARE grantee
    counts), every expected role must be present, and each expected role is granted USAGE on the app."""
    flat = " ".join(roles_sql.split())
    block = _streamlit_proof_block(roles_sql)
    problems: list[str] = []
    expected = sorted(ar.EXPECTED_APP_GRANTEES)
    lists = _PROOF_GRANTEE_LIST.findall(block)
    if not lists:
        problems.append("no grantee IN-list")
    for negated, body in lists:
        if negated:
            problems.append("a NOT IN list (only ROLE rows counted; other grantee kinds slip through)")
        names = sorted(re.findall(r"'([A-Za-z0-9_$]+)'", body))
        if names != expected:
            problems.append(f"IN-list {names} != EXPECTED_APP_GRANTEES {expected}")
    if not _PROOF_ANY_KIND.search(block):
        problems.append("the bad count is not COUNT_IF(USAGE AND NOT (granted_to = 'ROLE' AND grantee_name IN ...))")
    if not re.search(rf"\bpresent\s*<\s*{len(expected)}\b", block):
        problems.append(f"the missing check is not present < {len(expected)}")
    if f"SHOW GRANTS ON STREAMLIT {_APP};" not in block:
        problems.append("the block does not read the app object access_sql.show_grants_on_app_sql() reads")
    problems.extend(f"no GRANT USAGE ON STREAMLIT ... TO ROLE {role}" for role in expected
                    if f"GRANT USAGE ON STREAMLIT {_APP} TO ROLE {role};" not in flat)
    return problems


# The owner-approved PART C proof block (access design 2026-10-05) with its Streamlit grants: the form the
# lock below must accept.
_PART_C = f"""
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;
EXECUTE IMMEDIATE $$
BEGIN
  SHOW GRANTS ON STREAMLIT {_APP};
  SELECT
    COUNT_IF("privilege" = 'USAGE'
             AND NOT ("granted_to" = 'ROLE'
                      AND "grantee_name" IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS',
                                             'SNOW_PRI_GFR_PRD_ALFA_DSA', 'SNOW_PRI_GFR_PRD_ALFA_DTI'))),
    COUNT(DISTINCT CASE WHEN "privilege" = 'USAGE' AND "granted_to" = 'ROLE'
             AND "grantee_name" IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS',
                                    'SNOW_PRI_GFR_PRD_ALFA_DSA', 'SNOW_PRI_GFR_PRD_ALFA_DTI')
             THEN "grantee_name" END)
    INTO :bad, :present
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
  IF (bad > 0) THEN
    RAISE unexpected_grantee;
  END IF;
  IF (present < 4) THEN
    RAISE missing_grantee;
  END IF;
END;
$$;
"""

# The two-role block roles.sql holds before PART C (ROLE rows only, NOT IN, present < 2).
_TWO_ROLE = f"""
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_SYSADMINS;
EXECUTE IMMEDIATE $$
BEGIN
  SHOW GRANTS ON STREAMLIT {_APP};
  SELECT
    COUNT_IF("privilege" = 'USAGE' AND "granted_to" = 'ROLE'
             AND "grantee_name" NOT IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')),
    COUNT(DISTINCT CASE WHEN "privilege" = 'USAGE' AND "granted_to" = 'ROLE'
             AND "grantee_name" IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
             THEN "grantee_name" END)
    INTO :bad, :present
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
  IF (present < 2) THEN
    RAISE missing_grantee;
  END IF;
END;
$$;
"""


def test_the_proof_block_lock_accepts_part_c_and_rejects_the_two_role_block():
    assert _proof_block_problems(_PART_C) == []
    problems = " | ".join(_proof_block_problems(_TWO_ROLE))
    for why in ("NOT IN", "IN-list", "bad count", "present < 4", "SNOW_PRI_GFR_PRD_ALFA_DSA"):
        assert why in problems, (why, problems)
    fewer = _PART_C.replace(", 'SNOW_PRI_GFR_PRD_ALFA_DTI'", "")
    assert any("IN-list" in p for p in _proof_block_problems(fewer))


@pytest.mark.xfail(strict=True, reason=(
    "v4.610.0 integration lands roles.sql PART C (four roles, any-kind NOT (granted_to = 'ROLE' AND ...)) "
    "with the regenerated snowflake/rebuild/03 (law 6). Until then roles.sql still holds the two-role block "
    "and Admin > Access's -20011/-20012 wording is ahead of it. strict: once PART C lands this XPASSes and "
    "FAILS the suite -- delete this marker in the same change."))
def test_roles_sql_proof_block_is_the_rule_app_grant_review_applies():
    assert _proof_block_problems(read("snowflake/roles.sql")) == []


# ---------------------------------------------------------------------------
# roster + source wording
# ---------------------------------------------------------------------------
def _info(**over) -> dict:
    base = {"roster_status": "ok", "admin_users": ("A1", "B2"), "nested_roles": (), "roster_error": "",
            "roster_at": 1_000.0, "admin_role": cfg.ADMIN_ACCESS_ROLE}
    return {**base, **over}


def test_roster_summary_ok_names_the_user_count():
    s = ar.roster_summary(_info(), now=1_030.0)
    assert s["state"] == "ok"
    assert "2 direct user members" in s["headline"] and cfg.ADMIN_ACCESS_ROLE in s["headline"]
    assert "30s ago" in s["headline"]
    assert ar.roster_summary(_info(admin_users=("A1",)), now=1_000.0)["headline"].count("1 direct user member ") == 1


def test_roster_summary_failure_and_empty_are_unavailable():
    failed = ar.roster_summary(_info(roster_status="lookup_failed", admin_users=(), roster_error="Insufficient"),
                               now=1_000.0)
    assert failed["state"] == "unavailable" and "failed" in failed["headline"].lower()
    assert failed["detail"] == "Insufficient"
    unv = ar.roster_summary(_info(roster_status="unverified", admin_users=(), roster_error="listed no USER"),
                            now=1_000.0)
    assert unv["state"] == "unavailable" and "unverified" in unv["headline"].lower()
    assert "privilege gap" in unv["headline"]          # never "no members"
    assert "no members" not in unv["headline"].lower().replace("never read as 'no members'", "")


def test_roster_summary_not_checked_off_sis():
    s = ar.roster_summary(_info(roster_status="not_checked", admin_users=(), roster_at=None), now=1_000.0)
    assert s["state"] == "not_checked"


def test_every_access_source_has_a_plain_label():
    for source in (*sess.ACCESS_SOURCES, "no_identity", "off_sis"):
        assert ar.source_label(source) and ar.source_label(source) != source
    assert ar.source_label("something_new") == "something_new"


def test_resolved_clock_is_central():
    # 2026-10-05 15:00:00 UTC is 10:00:00 Central (CDT)
    import datetime as dt
    at = dt.datetime(2026, 10, 5, 15, 0, 0, tzinfo=dt.UTC).timestamp()
    assert ar.resolved_clock(at) == "10:00:00 Central"
    assert ar.resolved_clock(None) == "—" and ar.resolved_clock("x") == "—"


# ---------------------------------------------------------------------------
# The Settings caption (in-app operator wording)
# ---------------------------------------------------------------------------
def _caption_args(rel: str) -> list[ast.AST]:
    return [node.args[0] for node in ast.walk(ast.parse(read(rel)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "caption" and node.args]


def test_settings_caption_names_both_admin_routes():
    src = read("app/ui/pages/admin.py")
    assert "limited to operators (config OPERATOR_USERS)" not in src
    hits = [a for a in _caption_args("app/ui/pages/admin.py")
            if "ADMIN_ACCESS_HINT" in ast.unparse(a) and "copy the SQL" in ast.unparse(a)]
    assert len(hits) == 1, "the Settings non-operator caption must lead with config.ADMIN_ACCESS_HINT"


# ---------------------------------------------------------------------------
# The tab is read-only
# ---------------------------------------------------------------------------
def _write_paths() -> set[str]:
    """Every way to issue a write: each app.core.query executor (derived, so a new execute_* is covered
    with no edit here), the buffered INSERT, the raw session submitters, the confirm / C48 latch helpers,
    and the memo clear the tab may reach only through session.recheck_access."""
    import app.core.query as query_mod
    executors = {n for n in vars(query_mod) if n.startswith("execute_") and callable(getattr(query_mod, n))}
    return executors | {"_buffer_write", "submit_collect", "submit_pandas", "confirm_gate", "write_gate_open",
                        "stamp_write", "forget_access"}


def test_the_write_path_set_is_not_vacuous():
    paths = _write_paths()
    for name in ("execute_statement", "execute_statement_async", "execute_cancel_query", "execute_action",
                 "_buffer_write"):
        assert name in paths, name


def test_access_tab_issues_no_write():
    tree = ast.parse(read("app/ui/pages/admin.py"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_access_tab")
    called = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
              for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert not called & _write_paths(), sorted(called & _write_paths())
    assert "recheck_access" in called          # the one side effect: this session's memo


# ---------------------------------------------------------------------------
# AppTest: the real shell as an identified SiS viewer
# ---------------------------------------------------------------------------
def _fake_run_factory(state: dict):
    def _run(*args, **kwargs):
        key = str(kwargs.get("key", ""))
        if key == "adm_app_grants":
            state["grant_reads"] += 1
            if state["grants_error"]:
                return QueryResult(df=pd.DataFrame(), ok=False, source="stub", error=state["grants_error"])
            return QueryResult(df=state["grants"].copy(), ok=True, source=str(kwargs.get("source", "stub")))
        return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))
    return _run


@pytest.fixture
def access_app(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import admin

    st.session_state.clear()
    state = {"viewer": _DSA_USER, "sis": True, "shows": 0, "grant_reads": 0, "raise": None,
             "rows": _role_rows(("USER", _DSA_USER), ("USER", "OTHER_DSA"), ("ROLE", "SOME_NESTED_ROLE")),
             "grants": _four_roles_frame(), "grants_error": ""}

    def _lookup():
        state["shows"] += 1
        if state["raise"] is not None:
            raise state["raise"]
        return list(state["rows"])

    fake_run = _fake_run_factory(state)
    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_ACCOUNTADMINS")
    monkeypatch.setattr(main_mod, "_schema_floor_breach", lambda: None)
    monkeypatch.setattr(main_mod, "run", fake_run)
    monkeypatch.setattr(ident, "viewer_name", lambda: state["viewer"])
    monkeypatch.setattr(sess, "is_sis", lambda: state["sis"])
    monkeypatch.setattr(sess, "current_role", lambda: "SNOW_SYSADMINS")
    monkeypatch.setattr(sess, "_admin_role_rows", _lookup)
    monkeypatch.setattr(errors_mod, "record_error", lambda *a, **k: "ref")
    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(components, "user_display_map", lambda _page: {})
    monkeypatch.setattr(admin, "run", fake_run)
    monkeypatch.setattr(admin, "run_batch", lambda specs, **_k: {s.get("key"): fake_run(**s) for s in specs})
    monkeypatch.setattr(admin, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))
    import app.core.query as query_mod
    monkeypatch.setattr(query_mod, "execute_statement_async", lambda *a, **k: True)
    yield state
    st.session_state.clear()


def _entry():
    import app.main

    app.main.main()


def _open_access(state: dict, **seed):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_entry, default_timeout=30)
    for k, v in seed.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    for r in at.radio:
        if str(getattr(r, "key", "") or "").startswith("_ow_nav_") and "Admin" in list(r.options):
            r.set_value("Admin")
            break
    else:
        raise AssertionError("Admin is not offered to this viewer")
    at.run()
    at.session_state["adm_section"] = "Access"
    at.run()
    assert not at.exception, at.exception
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at


def _text(at) -> str:
    parts = [str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning) + list(at.info)
             + list(at.error) + list(at.success)]
    return " ".join(parts)


def _frames(at) -> str:
    return " ".join(df.value.to_string() for df in at.dataframe)


def test_role_admin_sees_own_access_the_roster_and_the_app_grants(access_app):
    at = _open_access(access_app)
    text, frames = _text(at), _frames(at)
    # own access
    assert _DSA_USER in text and "DBA" in text
    assert ar.source_label("role") in text
    # roster: OK with the USER count, members, and the nested role flagged as not expanded
    assert "2 direct user members" in text
    assert _DSA_USER in frames and "OTHER_DSA" in frames
    assert "SOME_NESTED_ROLE" in frames and "not expanded" in frames.lower()
    # app grants: the four expected roles, verified clean
    assert "Exactly the four access roles hold USAGE" in text
    for role in cfg.APP_ACCESS_ROLES:
        assert role in frames
    assert access_app["grant_reads"] >= 1
    # the guidance
    assert "GRANT ROLE " + cfg.ADMIN_ACCESS_ROLE in text


def test_allowlisted_admin_with_a_failed_lookup_sees_it_failed(access_app):
    access_app["viewer"] = _ADMIN
    access_app["raise"] = RuntimeError("SQL access control error: Insufficient privileges")
    at = _open_access(access_app)
    text = _text(at)
    assert ar.source_label("allowlist") in text                 # the named admin is unaffected
    assert "lookup failed" in text.lower()
    assert any("Insufficient privileges" in str(c.value) for c in at.code)   # the error one click away
    assert "direct user members" not in text


def test_allowlisted_admin_with_an_empty_roster_sees_unverified_never_clean(access_app):
    access_app["viewer"] = _ADMIN
    access_app["rows"] = _role_rows(("ROLE", "SOME_NESTED_ROLE"))
    at = _open_access(access_app)
    text, frames = _text(at), _frames(at)
    assert "unverified" in text.lower() and "privilege gap" in text
    assert "0 direct user members" not in text
    assert "SOME_NESTED_ROLE" in frames                          # still reported, still not expanded


def test_app_grant_drift_says_what_to_do(access_app):
    access_app["viewer"] = _ADMIN
    access_app["grants"] = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                                        ("USAGE", "ROLE", "SNOW_ACCOUNTADMINS"),
                                        ("USAGE", "ROLE", "SNOW_SYSADMINS"),
                                        ("USAGE", "ROLE", "PUBLIC"))
    at = _open_access(access_app)
    warn = " ".join(str(w.value) for w in at.warning)
    err = " ".join(str(e.value) for e in at.error)
    assert cfg.ADMIN_ACCESS_ROLE in warn and cfg.VIEW_ACCESS_ROLE in warn and "roles.sql" in warn
    assert "PUBLIC" in err and "REVOKE" in err
    assert "Exactly the four access roles hold USAGE" not in _text(at)


def test_app_grants_empty_or_failed_is_unavailable(access_app):
    access_app["viewer"] = _ADMIN
    access_app["grants"] = pd.DataFrame()
    at = _open_access(access_app)
    assert any("unverified" in str(e.value).lower() for e in at.error)
    access_app["grants_error"] = "Insufficient privileges to operate on streamlit"
    at = _open_access(access_app)
    assert any("Query failed" in str(e.value) for e in at.error)


def test_recheck_now_clears_only_this_sessions_memo_and_says_so(access_app):
    at = _open_access(access_app)
    assert any("only your own session" in str(c.value).lower() for c in at.caption)
    before = access_app["shows"]
    access_app["rows"] = _role_rows(("USER", _DSA_USER), ("USER", "NEW_DSA"))
    btn = [b for b in at.button if str(b.key or "") == "adm_access_recheck"]
    assert btn, [b.key for b in at.button]
    btn[0].click()
    at.run()
    assert not at.exception, at.exception
    assert access_app["shows"] == before + 1                     # exactly one fresh lookup
    assert "NEW_DSA" in _frames(at)
    assert any("Re-checked" in str(c.value) for c in at.caption)


def test_a_stale_recheck_receipt_is_never_shown(access_app):
    # a receipt left behind when the re-check moved the clicker off Admin must not resurface much later
    at = _open_access(access_app, _adm_access_recheck_receipt={"at": 1.0, "text": "Re-checked: long ago"})
    assert not any("Re-checked" in str(c.value) for c in at.caption)
    assert "_adm_access_recheck_receipt" not in at.session_state


def test_recheck_that_revokes_the_clicker_drops_admin(access_app):
    at = _open_access(access_app)
    access_app["rows"] = _role_rows(("USER", "SOMEONE_ELSE"))
    next(b for b in at.button if str(b.key or "") == "adm_access_recheck").click()
    at.run()
    assert not at.exception, at.exception
    options = [o for r in at.radio if str(getattr(r, "key", "") or "").startswith("_ow_nav_") for o in r.options]
    assert sorted(options) == sorted(cfg.PAGES_BY_PROFILE[cfg.VIEWER_UNKNOWN_PROFILE])
    assert at.session_state["_ow_page"] != "Admin"


def test_off_sis_never_runs_the_role_lookup(access_app):
    access_app["sis"] = False
    access_app["viewer"] = ""
    at = _open_access(access_app)
    assert access_app["shows"] == 0
    assert ar.source_label("off_sis") in _text(at)
    assert "runs only on Streamlit-in-Snowflake" in _text(at)


# ---------------------------------------------------------------------------
# Review d6c62b87 follow-ups: honest wording on the tab
# ---------------------------------------------------------------------------
def test_recheck_note_names_the_window_that_actually_applies():
    from app.logic.formulas import humanize_duration as hd

    unavailable = sess.ACCESS_UNAVAILABLE_SOURCES
    ttl, retry = cfg.ACCESS_TTL_S, cfg.ACCESS_RETRY_S
    for source in ("allowlist", "role", "default"):
        note = ar.recheck_note(source, sis=True, unavailable=unavailable)
        assert hd(ttl) in note and hd(retry) not in note, (source, note)
    for source in unavailable:                   # a failed / empty lookup is retried after ACCESS_RETRY_S
        note = ar.recheck_note(source, sis=True, unavailable=unavailable)
        assert hd(retry) in note and hd(ttl) not in note, (source, note)
    # nothing is memoized for an unidentified viewer, or anywhere off Streamlit-in-Snowflake
    for source, sis in (("no_identity", True), ("off_sis", False), ("default", False), ("allowlist", False)):
        note = ar.recheck_note(source, sis=sis, unavailable=unavailable)
        assert "every run" in note and hd(ttl) not in note, (source, sis, note)


def test_off_sis_resolved_help_does_not_promise_a_memo(access_app):
    access_app["sis"] = False
    access_app["viewer"] = ""
    at = _open_access(access_app)
    text = _text(at)
    assert ar.recheck_note("off_sis", sis=False, unavailable=sess.ACCESS_UNAVAILABLE_SOURCES) in text
    assert "A resolved answer is re-checked after" not in text


def test_remove_an_admin_guidance_names_the_allowlist_exception(access_app):
    at = _open_access(access_app)
    bullets = [line for e in at.markdown for line in str(e.value).splitlines() if "Remove an admin" in line]
    assert len(bullets) == 1, bullets
    assert "OPERATOR_USERS" in bullets[0] and "stays an admin" in bullets[0]


def test_missing_usage_for_the_owner_never_claims_its_members_are_locked_out(access_app):
    access_app["viewer"] = _ADMIN
    access_app["grants"] = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                                        ("USAGE", "ROLE", "SNOW_SYSADMINS"))
    at = _open_access(access_app)
    warn = " ".join(str(w.value) for w in at.warning)
    assert "no explicit usage grant" in warn.lower() and "roles.sql requires one" in warn
    locked = re.search(r"members of ([^.]*) cannot open the app", warn)
    assert locked, warn
    assert "SNOW_ACCOUNTADMINS" not in locked.group(1)
    assert cfg.ADMIN_ACCESS_ROLE in locked.group(1) and cfg.VIEW_ACCESS_ROLE in locked.group(1)
    assert "SNOW_ACCOUNTADMINS owns the app, so its members still open it" in warn
    assert "owns the app" in _frames(at).lower()                  # the table's MEANS reads the OWNERSHIP row
