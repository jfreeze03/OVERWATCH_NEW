"""v4.610.0 Admin ▸ App access (owner decision 2026-10-05); roles only since v4.611.0 (owner 2026-10-07).

The tab answers "who can open OVERWATCH, and who can change things in it":
  * the viewer's own resolved access (profile, operator, how it was decided, when);
  * the SNOW_PRI_GFR_PRD_ALFA_DSA lookup status -- OK with its USER count, failed, or unverified (an EMPTY
    answer is a privilege gap and never renders as a clean "no members");
  * 'Re-check now', which clears only the clicking viewer's own session memo (and says so);
  * the DSA direct user members, and its ROLE grantees flagged 'not expanded' (they are NOT admins);
  * the app's USAGE grantees (SHOW GRANTS ON STREAMLIT, read-only) against the four expected roles: the rule
    roles.sql's proof block applies (it grants all four since 4.610.1), with every remedy split on
    config.ROLES_SQL_APP_GRANTEES, which a plain lock below pins to what roles.sql grants. With the real config
    nothing is pending; the pending / held-ahead wording for a role roles.sql does not grant is still covered,
    by pinning the managed set to the two SNOW_* roles (_manage_only_snow);
  * plain guidance.
Read-only apart from the memo clear: the tab issues no write. Since v4.611.0 no username is an admin: the
fixture's DSA_PERSON1 reaches Admin through a scripted DSA roster, and during an outage nobody reaches the tab
(the sidebar's 'Why read-only?' panel carries the diagnostic instead).
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


def _snow_only_frame() -> pd.DataFrame:
    """The app's grants when only the two SNOW_* roles hold USAGE (DSA/DTI lost theirs, or never got them)."""
    return _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                        ("USAGE", "ROLE", "SNOW_ACCOUNTADMINS"), ("USAGE", "ROLE", "SNOW_SYSADMINS"))


_SNOW_ONLY = ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")
# Wording only a role roles.sql does not grant may carry. Since 4.610.1 roles.sql grants all four, so none of it
# may appear for the real config.
_PENDING_WORDS = ("not granted by roles.sql yet", "owner-side change", "pending", "does not grant")


def _manage_only_snow(monkeypatch) -> None:
    """Pin roles.sql's managed set to the two SNOW_* roles (its pre-4.610.1 state). The real config manages all
    four, so the pending / held-ahead branch is reachable only this way; the tests whose subject is that branch
    use it to keep the wording covered for a role the decision ever adds ahead of roles.sql."""
    from app.ui.pages import admin
    monkeypatch.setattr(ar, "ROLES_SQL_MANAGED", _SNOW_ONLY)
    monkeypatch.setattr(admin, "ROLES_SQL_APP_GRANTEES", _SNOW_ONLY)
    assert ar.pending_roles() == (cfg.ADMIN_ACCESS_ROLE, cfg.VIEW_ACCESS_ROLE)


def _pending_words_in(text: str) -> list[str]:
    return [w for w in _PENDING_WORDS if w in text]


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
    assert by[cfg.VIEW_ACCESS_ROLE]["HAS_USAGE"] == "No"
    assert by["PUBLIC"]["EXPECTED"] == "No" and "REVOKE" in by["PUBLIC"]["STATUS"]
    assert by["SNOW_SYSADMINS"]["STATUS"] == "OK"
    assert "admin" in by[cfg.ADMIN_ACCESS_ROLE]["MEANS"].lower()
    assert "view-only" in by[cfg.VIEW_ACCESS_ROLE]["MEANS"].lower()
    for role in cfg.APP_ACCESS_ROLES:
        assert ar.ROLE_MEANING[role]
        assert "OPERATOR_USERS" not in ar.ROLE_MEANING[role], role      # v4.611.0: roles alone decide


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
    assert "re-run snowflake/roles.sql" in by["SNOW_ACCOUNTADMINS"]["STATUS"]


# ---------------------------------------------------------------------------
# Holistic 4.610 #0/#3/#5/#6/#11: every remedy follows what roles.sql grants TODAY
# ---------------------------------------------------------------------------
def test_roles_sql_manages_every_access_role_so_nothing_is_pending():
    # owner change 2026-10-06 (4.610.1): roles.sql grants all four, so no role takes the pending wording
    assert set(ar.ROLES_SQL_MANAGED) == set(cfg.APP_ACCESS_ROLES) == set(ar.EXPECTED_APP_GRANTEES)
    assert ar.pending_roles() == ()
    for frame in (_four_roles_frame(), _snow_only_frame(), _grant_frame(("OWNERSHIP", "ROLE", "SNOW_SYSADMINS"))):
        r = ar.app_grant_review(frame)
        assert r["missing_pending"] == () and r["ahead"] == ()
        assert not _pending_words_in(" ".join(row["STATUS"] for row in r["table"])), r["table"]


def test_missing_wording_is_split_on_what_roles_sql_grants(monkeypatch):
    # today: DSA/DTI lost their USAGE (a deploy re-created the app, and only the SNOW_* grants are back); roles.sql
    # grants both, so the remedy is the re-run (-20012), never the pending wording
    r = ar.app_grant_review(_snow_only_frame())
    assert r["missing_rerun"] == (cfg.ADMIN_ACCESS_ROLE, cfg.VIEW_ACCESS_ROLE)
    assert r["missing_pending"] == () and r["ahead"] == ()
    by = {row["GRANTEE"]: row for row in r["table"]}
    for role in (cfg.ADMIN_ACCESS_ROLE, cfg.VIEW_ACCESS_ROLE):
        status = by[role]["STATUS"]
        assert status == ar.MISSING_RERUN, (role, status)
        assert "re-run snowflake/roles.sql's Streamlit block" in status and "-20012" in status
        assert "-20011" not in status and not _pending_words_in(status), status
    # a SNOW_* role takes the same re-run remedy
    frame = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"), ("USAGE", "ROLE", "SNOW_ACCOUNTADMINS"),
                         ("USAGE", "ROLE", cfg.ADMIN_ACCESS_ROLE), ("USAGE", "ROLE", cfg.VIEW_ACCESS_ROLE))
    r = ar.app_grant_review(frame)
    assert r["missing_rerun"] == ("SNOW_SYSADMINS",) and r["missing_pending"] == ()
    by = {row["GRANTEE"]: row for row in r["table"]}
    assert by["SNOW_SYSADMINS"]["STATUS"] == ar.MISSING_RERUN and "-20012" in ar.MISSING_RERUN

    # the branch for a role roles.sql does not grant (its pre-4.610.1 state, pinned here)
    _manage_only_snow(monkeypatch)
    r = ar.app_grant_review(_snow_only_frame())
    pending = ar.pending_roles()
    assert r["missing_pending"] == pending and r["missing_rerun"] == () and r["ahead"] == ()
    by = {row["GRANTEE"]: row for row in r["table"]}
    for role in pending:
        status = by[role]["STATUS"]
        assert status == ar.MISSING_PENDING, (role, status)
        # never the remedy that cannot work: re-running roles.sql adds nothing for a role it does not grant
        assert "re-run" not in status.lower() and "-20012" not in status
        assert "not granted by roles.sql yet" in status and "-20011" in status
    # a role roles.sql grants keeps the re-run remedy
    r = ar.app_grant_review(frame)
    assert r["missing_rerun"] == ("SNOW_SYSADMINS",) and r["missing_pending"] == ()
    by = {row["GRANTEE"]: row for row in r["table"]}
    assert by["SNOW_SYSADMINS"]["STATUS"] == ar.MISSING_RERUN


def test_a_role_held_ahead_of_roles_sql_is_flagged_not_silently_ok(monkeypatch):
    # today: roles.sql grants all four, so the four-role answer is plain OK on every row
    r = ar.app_grant_review(_four_roles_frame())
    assert r["status"] == "ok" and r["ahead"] == ()
    assert [row["STATUS"] for row in r["table"]] == ["OK"] * len(cfg.APP_ACCESS_ROLES)
    # a role roles.sql does not grant (pinned) that holds USAGE anyway: the target is met, but roles.sql's
    # proof block raises -20011 on it, so the row says so instead of a silent OK
    _manage_only_snow(monkeypatch)
    r = ar.app_grant_review(_four_roles_frame())
    assert r["status"] == "ok" and r["ahead"] == ar.pending_roles() != ()
    by = {row["GRANTEE"]: row for row in r["table"]}
    for role in ar.pending_roles():
        assert by[role]["STATUS"] == ar.HELD_AHEAD and "-20011" in by[role]["STATUS"]
    for role in _SNOW_ONLY:
        assert by[role]["STATUS"] == "OK"


def test_every_unexpected_grantee_is_promised_the_20011():
    # roles.sql's proof block (4.610.1) counts every USAGE row that is not an access ROLE, whatever its kind, so
    # the -20011 promise holds for a database role too (it was ROLE-only while the block counted ROLE rows only)
    assert _PROOF_ANY_KIND.search(_streamlit_proof_block(read("snowflake/roles.sql")))
    frame = pd.concat([_four_roles_frame(), _grant_frame(("USAGE", "ROLE", "PUBLIC"),
                                                         ("USAGE", "DATABASE_ROLE", "DB.DR"))], ignore_index=True)
    r = ar.app_grant_review(frame)
    assert r["status"] == "drift" and r["unexpected"] == ("DATABASE_ROLE DB.DR", "ROLE PUBLIC")
    by = {row["GRANTEE"]: row for row in r["table"]}
    for name in ("PUBLIC", "DB.DR"):
        assert by[name]["EXPECTED"] == "No" and "REVOKE" in by[name]["STATUS"] and "-20011" in by[name]["STATUS"]
    assert "not an access role" in by["DB.DR"]["STATUS"]


_GRANT_ON_APP = re.compile(r"^\s*GRANT\s+USAGE\s+ON\s+STREAMLIT\s+(\S+)\s+TO\s+ROLE\s+([A-Za-z0-9_$]+)\s*;",
                           re.IGNORECASE | re.MULTILINE)


def test_roles_sql_app_grantees_is_what_roles_sql_grants_and_proves_today():
    """config.ROLES_SQL_APP_GRANTEES is the wording switch (holistic 4.610 #0): it must be exactly the roles
    roles.sql grants USAGE on the app AND the roles its proof block's IN-lists name (all four access roles
    since 4.610.1), so the in-app text always matches the file."""
    roles_sql = read("snowflake/roles.sql")
    granted = {role.upper() for obj, role in _GRANT_ON_APP.findall(roles_sql) if obj.upper() == _APP.upper()}
    managed = set(cfg.ROLES_SQL_APP_GRANTEES)
    assert granted == managed, (sorted(granted), sorted(managed))
    block = _streamlit_proof_block(roles_sql)
    lists = _PROOF_GRANTEE_LIST.findall(block)
    assert lists, "the proof block names no grantee IN-list"
    for _negated, body in lists:
        assert set(re.findall(r"'([A-Za-z0-9_$]+)'", body)) == managed, body
    assert re.search(rf"\bpresent\s*<\s*{len(managed)}\b", block), "the missing check counts another set"
    # the managed roles are access roles, in the decision's order
    assert tuple(r for r in cfg.APP_ACCESS_ROLES if r in managed) == cfg.ROLES_SQL_APP_GRANTEES
    assert ar.pending_roles() == tuple(r for r in cfg.APP_ACCESS_ROLES if r not in managed)


def test_the_grantees_lock_is_not_vacuous():
    # the pre-4.610.1 two-role file (DSA/DTI grant lines removed) fails the lock against the four-role tuple
    two_role = "\n".join(line for line in read("snowflake/roles.sql").splitlines()
                         if not re.search(r"TO ROLE SNOW_PRI_GFR_PRD_ALFA_D(SA|TI)\b", line))
    granted = {role.upper() for obj, role in _GRANT_ON_APP.findall(two_role) if obj.upper() == _APP.upper()}
    assert granted == {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"} != set(cfg.ROLES_SQL_APP_GRANTEES)
    # ...and PART C's text, which roles.sql now carries, grants exactly the four
    assert {role.upper() for _obj, role in _GRANT_ON_APP.findall(_PART_C)} == set(cfg.ROLES_SQL_APP_GRANTEES)


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
    # which check raises which error (review 4.610.1 #1): the app promises -20011 for every unexpected grantee and
    # -20012 for a missing access role, so each count must raise its own exception, declared with its own code,
    # before the block returns its OK
    raises = {
        "unexpected": re.search(r"\bIF\s*\(\s*bad\s*>\s*0\s*\)\s*THEN\s*RAISE\s+unexpected_grantee\s*;", block),
        "missing": re.search(rf"\bIF\s*\(\s*present\s*<\s*{len(expected)}\s*\)\s*THEN\s*RAISE\s+missing_grantee\s*;",
                             block),
    }
    if not raises["unexpected"]:
        problems.append("the bad count is not IF (bad > 0) THEN RAISE unexpected_grantee")
    if not raises["missing"]:
        problems.append(f"the missing check is not IF (present < {len(expected)}) THEN RAISE missing_grantee")
    for name, code in (("unexpected_grantee", -20011), ("missing_grantee", -20012)):
        if not re.search(rf"\b{name}\s+EXCEPTION\s*\(\s*{code}\s*,", block):
            problems.append(f"{name} is not declared EXCEPTION ({code}, ...)")
    ok = re.search(r"\bRETURN\s+'Streamlit grants OK\b", block)
    if not ok:
        problems.append("the block does not RETURN 'Streamlit grants OK'")
    elif any(m and m.start() > ok.start() for m in raises.values()):
        problems.append("the block RETURNs 'Streamlit grants OK' before a check can raise")
    if f"SHOW GRANTS ON STREAMLIT {_APP};" not in block:
        problems.append("the block does not read the app object access_sql.show_grants_on_app_sql() reads")
    problems.extend(f"no GRANT USAGE ON STREAMLIT ... TO ROLE {role}" for role in expected
                    if f"GRANT USAGE ON STREAMLIT {_APP} TO ROLE {role};" not in flat)
    return problems


# The owner-approved four-role form (owner decision 2026-10-05; roles.sql PART C, which roles.sql carries since
# 4.610.1) with its Streamlit grants: the form the lock below must accept.
_PART_C = f"""
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
GRANT USAGE ON STREAMLIT {_APP} TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;
EXECUTE IMMEDIATE $$
DECLARE
  unexpected_grantee EXCEPTION (-20011, 'a USAGE grantee outside the four access roles');
  missing_grantee EXCEPTION (-20012, 'an access role is missing USAGE');
  bad INTEGER;
  present INTEGER;
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
  RETURN 'Streamlit grants OK: the four access roles are present, no unexpected grantee.';
END;
$$;
"""

# The two-role block roles.sql held before PART C, through 4.610.0 (ROLE rows only, NOT IN, present < 2).
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


def _mutant(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_the_proof_block_lock_knows_which_check_raises_which_error():
    """review 4.610.1 #1: the app promises -20011 for every unexpected grantee and -20012 for a missing access
    role. A block that never raises on 'bad', raises the other exception, swaps the codes or returns its OK
    before the checks counts the right rows and still breaks that promise; the lock rejects each."""
    def problems(text: str) -> str:
        return " | ".join(_proof_block_problems(text))

    no_bad = _mutant(_PART_C, "  IF (bad > 0) THEN\n    RAISE unexpected_grantee;\n  END IF;\n", "")
    assert "IF (bad > 0) THEN RAISE unexpected_grantee" in problems(no_bad)
    swapped = _mutant(_mutant(_mutant(_PART_C, "RAISE unexpected_grantee;", "RAISE @swap@;"),
                              "RAISE missing_grantee;", "RAISE unexpected_grantee;"),
                      "RAISE @swap@;", "RAISE missing_grantee;")
    assert ("IF (bad > 0) THEN RAISE unexpected_grantee" in problems(swapped)
            and "IF (present < 4) THEN RAISE missing_grantee" in problems(swapped))
    codes = _mutant(_mutant(_mutant(_PART_C, "(-20011,", "(@swap@,"), "(-20012,", "(-20011,"), "(@swap@,", "(-20012,")
    assert ("unexpected_grantee is not declared EXCEPTION (-20011" in problems(codes)
            and "missing_grantee is not declared EXCEPTION (-20012" in problems(codes))
    loose = _mutant(_PART_C, "IF (present < 4) THEN", "IF (present < 3) THEN")
    assert "IF (present < 4) THEN RAISE missing_grantee" in problems(loose)
    ok_line = "  RETURN 'Streamlit grants OK: the four access roles are present, no unexpected grantee.';\n"
    early = _mutant(_mutant(_PART_C, ok_line, ""), "  IF (bad > 0) THEN", ok_line + "  IF (bad > 0) THEN")
    assert "RETURNs 'Streamlit grants OK' before a check can raise" in problems(early)
    assert "does not RETURN 'Streamlit grants OK'" in problems(_mutant(_PART_C, ok_line, ""))
    # each mutant breaks only what it says: the unmutated rule (IN-lists, any-kind count, grants) still holds
    for text in (no_bad, swapped, codes, loose, early):
        assert "IN-list" not in problems(text) and "bad count is not COUNT_IF" not in problems(text)


def test_roles_sql_proof_block_is_the_rule_app_grant_review_applies():
    # owner change 2026-10-06 (4.610.1): roles.sql grants DSA/DTI and proves the four-role, any-kind set
    assert _proof_block_problems(read("snowflake/roles.sql")) == []
    assert _proof_block_problems(read("snowflake/rebuild/03_roles.sql")) == []    # the copy a rebuild runs


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
    # v4.611.0: no admin bypasses the lookup, so both say every viewer is read-only
    for s in (failed, unv):
        assert "Every viewer" in s["headline"] and "OPERATOR_USERS" not in s["headline"], s["headline"]


def test_roster_summary_not_checked_off_sis():
    s = ar.roster_summary(_info(roster_status="not_checked", admin_users=(), roster_at=None), now=1_000.0)
    assert s["state"] == "not_checked"


def test_every_access_source_has_a_plain_label():
    for source in (*sess.ACCESS_SOURCES, "no_identity", "off_sis"):
        assert ar.source_label(source) and ar.source_label(source) != source
    assert ar.source_label("something_new") == "something_new"
    # v4.611.0: the username route is retired, with no label
    assert "allowlist" not in sess.ACCESS_SOURCES and "allowlist" not in ar.SOURCE_LABELS


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


def test_settings_caption_names_the_admin_role():
    src = read("app/ui/pages/admin.py")
    assert "limited to operators (config OPERATOR_USERS)" not in src
    assert "OPERATOR_USERS" not in src                       # v4.611.0: no username route to name
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
    at.session_state["adm_section"] = "App access"
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


def test_role_admin_sees_own_access_the_roster_and_the_app_grants(access_app, monkeypatch):
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
    # roles.sql grants all four (4.610.1): the clean answer carries no warning and no pending wording
    assert not at.warning, [str(w.value) for w in at.warning]
    assert not _pending_words_in(text), _pending_words_in(text)
    # the guidance
    assert "GRANT ROLE " + cfg.ADMIN_ACCESS_ROLE in text
    # v4.611.0: the roster IS the admin list; no username route is named anywhere on the tab
    assert "OPERATOR_USERS" not in text and "named admin" not in text.lower()
    assert "ALSO_NAMED_ADMIN" not in frames
    assert "no username is hard-coded" in text
    # a role roles.sql does not grant (pinned) that holds USAGE anyway (a hand-made grant) warns about its next
    # run (holistic 4.610 #7)
    _manage_only_snow(monkeypatch)
    at = _open_access(access_app)
    assert "Exactly the four access roles hold USAGE" in _text(at)
    warn = " ".join(str(w.value) for w in at.warning)
    for role in ar.pending_roles():
        assert role in warn
    assert "-20011" in warn and "roles.sql does not grant" in warn


def test_recheck_during_an_outage_drops_the_admin_and_the_sidebar_says_why(access_app):
    """v4.611.0: no username bypasses the lookup, so an outage makes every admin read-only and nobody can open
    this tab meanwhile. The diagnostic moves to the sidebar's 'Why read-only?' panel, which every viewer sees."""
    from app.main import ACCESS_CHECK_UNAVAILABLE

    at = _open_access(access_app)
    access_app["raise"] = RuntimeError("SQL access control error: Insufficient privileges")
    next(b for b in at.button if str(b.key or "") == "adm_access_recheck").click()
    at.run()
    assert not at.exception, at.exception
    options = [o for r in at.radio if str(getattr(r, "key", "") or "").startswith("_ow_nav_") for o in r.options]
    assert sorted(options) == sorted(cfg.PAGES_BY_PROFILE[cfg.VIEWER_UNKNOWN_PROFILE])
    assert at.session_state["_ow_page"] != "Admin"
    assert ACCESS_CHECK_UNAVAILABLE in [str(c.value) for c in at.sidebar.caption]
    panel = [e for e in at.sidebar.expander if str(e.label) == "Why read-only?"]
    assert len(panel) == 1
    assert any("Insufficient privileges" in str(c.value) for c in panel[0].code)   # the error, one click away
    assert "lookup failed" in " ".join(str(c.value) for c in panel[0].caption).lower()


def test_app_grant_drift_says_what_to_do(access_app, monkeypatch):
    access_app["grants"] = pd.concat([_snow_only_frame(), _grant_frame(("USAGE", "ROLE", "PUBLIC"))],
                                     ignore_index=True)
    at = _open_access(access_app)
    warn = " ".join(str(w.value) for w in at.warning)
    err = " ".join(str(e.value) for e in at.error)
    assert cfg.ADMIN_ACCESS_ROLE in warn and cfg.VIEW_ACCESS_ROLE in warn
    # roles.sql grants DSA/DTI (4.610.1): a lost grant (a deploy re-created the app) is restored by re-running
    # its Streamlit block, and its proof block raises -20012 until then
    assert "Re-run snowflake/roles.sql's Streamlit block" in warn and "-20012" in warn
    assert "deploy --replace" in warn and "drops its USAGE grants" in warn
    assert "-20011" not in warn and not _pending_words_in(warn), warn
    assert "PUBLIC" in err and "REVOKE" in err and "-20011" in err
    assert "Exactly the four access roles hold USAGE" not in _text(at)
    # a role roles.sql does not grant (pinned) is never told to re-run it: re-running adds nothing for it
    _manage_only_snow(monkeypatch)
    at = _open_access(access_app)
    warn = " ".join(str(w.value) for w in at.warning)
    assert cfg.ADMIN_ACCESS_ROLE in warn and cfg.VIEW_ACCESS_ROLE in warn
    assert "not granted by roles.sql yet" in warn and "-20011" in warn
    assert "Re-run snowflake/roles.sql" not in warn and "-20012" not in warn


def test_app_grants_empty_or_failed_is_unavailable(access_app):
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
    for source in ("role", "default"):
        note = ar.recheck_note(source, sis=True, unavailable=unavailable)
        assert hd(ttl) in note and hd(retry) not in note, (source, note)
    for source in unavailable:                   # a failed / empty lookup backs off from ACCESS_RETRY_S to the TTL
        note = ar.recheck_note(source, sis=True, unavailable=unavailable)
        assert ar.retry_schedule() in note, (source, note)
        assert f"after {hd(retry)}" in note and f"every {hd(ttl)}" in note, (source, note)
    assert ar.retry_schedule() == f"after {hd(retry)}, then {hd(2 * retry)}, {hd(4 * retry)}, then every {hd(ttl)}"
    # nothing is memoized for an unidentified viewer, or anywhere off Streamlit-in-Snowflake
    for source, sis in (("no_identity", True), ("off_sis", False), ("default", False)):
        note = ar.recheck_note(source, sis=sis, unavailable=unavailable)
        assert "every run" in note and hd(ttl) not in note, (source, sis, note)


def test_off_sis_resolved_help_does_not_promise_a_memo(access_app):
    access_app["sis"] = False
    access_app["viewer"] = ""
    at = _open_access(access_app)
    text = _text(at)
    assert ar.recheck_note("off_sis", sis=False, unavailable=sess.ACCESS_UNAVAILABLE_SOURCES) in text
    assert "A resolved answer is re-checked after" not in text


def _guidance_lines(at) -> list[str]:
    return [line for e in at.markdown for line in str(e.value).splitlines()]


def test_how_access_works_says_what_roles_sql_does_today(access_app, monkeypatch):
    """holistic 4.610 #6: the guidance says what roles.sql grants. Since 4.610.1 that is all four, USAGE on the
    database, schema and app only, and every deploy drops the app's USAGE grants, so its Streamlit block is
    re-run after each one; with a role it does not grant (pinned) the guidance says that role is pending."""
    at = _open_access(access_app)
    lines = _guidance_lines(at)
    opening = [line for line in lines if "**Opening the app**" in line]
    assert len(opening) == 1, opening
    assert "grants all four USAGE on the database, schema and app (nothing else)" in opening[0]
    assert "-20012" in opening[0] and "-20011" in opening[0]
    assert "re-creates the app and drops its USAGE grants" in opening[0]
    assert "re-run roles.sql's Streamlit block after each deploy" in opening[0]
    assert "until a redeploy is confirmed" not in opening[0]
    assert not _pending_words_in(opening[0]) and " today" not in opening[0], opening[0]
    help_text = [line for line in lines if "Who can open OVERWATCH" in line]
    assert len(help_text) == 1 and "snowflake/roles.sql grants all four." in help_text[0], help_text
    assert not _pending_words_in(_text(at)), _pending_words_in(_text(at))
    make = [line for line in lines if "**Make someone an admin**" in line]
    assert len(make) == 1 and make[0].endswith("can create an OVERWATCH admin."), make
    # v4.611.0: the admins are the direct members of the DSA role, and only them
    admins = [line for line in lines if "**Admins**" in line]
    assert len(admins) == 1, admins
    assert f"DIRECT user members of `{cfg.ADMIN_ACCESS_ROLE}`, and only them" in admins[0], admins[0]
    assert not [line for line in lines if "named admin" in line.lower()]
    lookup = [line for line in lines if "**When the lookup fails or lists nobody**" in line]
    assert len(lookup) == 1 and "admins included" in lookup[0] and "Snowsight" in lookup[0], lookup

    # a role roles.sql does not grant (pinned): named as pending, with what that means for its members
    _manage_only_snow(monkeypatch)
    lines = _guidance_lines(_open_access(access_app))
    opening = [line for line in lines if "**Opening the app**" in line]
    assert len(opening) == 1, opening
    assert f"grants and proves {', '.join(_SNOW_ONLY)} today" in opening[0]
    assert "not granted by roles.sql yet" in opening[0] and "-20011" in opening[0]
    assert "USAGE on the database, schema and app" in opening[0]
    assert "grants all four" not in opening[0]
    help_text = [line for line in lines if "Who can open OVERWATCH" in line]
    assert len(help_text) == 1 and "owner-side change still pending" in help_text[0], help_text
    make = [line for line in lines if "**Make someone an admin**" in line]
    assert len(make) == 1 and "only of someone who can open the app" in make[0], make


def _make_admin_line(at) -> str:
    lines = [line for e in at.markdown for line in str(e.value).splitlines() if "**Make someone an admin**" in line]
    assert len(lines) == 1, lines
    return lines[0]


def test_make_admin_caveat_follows_the_live_grant_review(access_app, monkeypatch):
    """final review #1: the 'Make someone an admin' caveat. Since 4.610.1 roles.sql grants DSA, so there is no
    caveat in any grant state (a lost grant is the grant review's warning above, with its re-run remedy). While
    roles.sql did not grant DSA (pinned below) it had to be true in BOTH grant states: with a hand-made DSA grant
    (the fixture's four-role answer) its members open the app through DSA itself, so the line must not say they
    need another role; without one, or when the grants could not be read, it keeps the 'until DSA holds USAGE'
    wording."""
    assert cfg.ADMIN_ACCESS_ROLE not in ar.pending_roles()     # the state this release ships in
    for grants, error in ((_four_roles_frame(), ""), (_snow_only_frame(), ""), (pd.DataFrame(), ""),
                          (_four_roles_frame(), "Insufficient privileges")):
        access_app["grants"], access_app["grants_error"] = grants, error
        line = _make_admin_line(_open_access(access_app))
        assert line.endswith("can create an OVERWATCH admin."), line
        assert "until " not in line and "another role" not in line and "does not grant" not in line, line

    _manage_only_snow(monkeypatch)
    assert cfg.ADMIN_ACCESS_ROLE in ar.pending_roles()
    access_app["grants"], access_app["grants_error"] = _four_roles_frame(), ""
    # hand-grant state: DSA holds USAGE on the app
    line = _make_admin_line(_open_access(access_app))
    assert line.endswith(ar.admin_reach_note(True)), line
    assert f"{cfg.ADMIN_ACCESS_ROLE} holds USAGE on the app" in line
    assert "another role with USAGE" not in line and "until " not in line
    # that roles.sql's state: only the two SNOW_* roles hold it
    access_app["grants"] = _snow_only_frame()
    line = _make_admin_line(_open_access(access_app))
    assert line.endswith(ar.admin_reach_note(False)), line
    assert f"until {cfg.ADMIN_ACCESS_ROLE} holds USAGE on the database, schema and app" in line
    assert "another role with USAGE on it" in line
    # unknown (an empty or failed grants read): the 'until' wording, which holds whichever way the grants stand
    for grants, error in ((pd.DataFrame(), ""), (_four_roles_frame(), "Insufficient privileges")):
        access_app["grants"], access_app["grants_error"] = grants, error
        assert _make_admin_line(_open_access(access_app)).endswith(ar.admin_reach_note(None))


def test_admin_reach_note_wording(monkeypatch):
    # roles.sql grants the admin role (the owner's 2026-10-06 change, 4.610.1): no caveat in any grant state;
    # the grant review covers a lost grant
    assert [ar.admin_reach_note(v) for v in (True, False, None)] == ["", "", ""]
    # a roles.sql that does not grant the admin role (pinned): the caveat, worded from the grant state
    _manage_only_snow(monkeypatch)
    held, lacking, unknown = (ar.admin_reach_note(v) for v in (True, False, None))
    assert unknown == lacking != held
    for note in (held, lacking):
        assert note.startswith(" Membership makes an admin only of someone who can open the app: ")
        assert "roles.sql does not" in note
    assert "provided it also holds USAGE on the database and schema" in held
    assert "until" not in held and "another role" not in held


def test_remove_an_admin_guidance_has_no_exception(access_app):
    # v4.611.0 (owner 2026-10-07): REVOKE alone removes an admin; no username list outlives it
    from app.logic.formulas import humanize_duration as hd

    at = _open_access(access_app)
    bullets = [line for e in at.markdown for line in str(e.value).splitlines() if "Remove an admin" in line]
    assert len(bullets) == 1, bullets
    b = bullets[0]
    assert f"REVOKE ROLE {cfg.ADMIN_ACCESS_ROLE} FROM USER" in b
    assert hd(cfg.WRITE_RECHECK_S) in b and hd(cfg.ACCESS_TTL_S) in b and "no redeploy" in b
    for gone in ("OPERATOR_USERS", "stays an admin", "named admin", "allowlist"):
        assert gone not in b, gone


def _assert_owner_is_not_locked_out(at) -> str:
    warn = " ".join(str(w.value) for w in at.warning)
    assert "no explicit usage grant" in warn.lower()
    locked = re.search(r"members of ([^.]*) cannot open the app", warn)
    assert locked, warn
    assert "SNOW_ACCOUNTADMINS" not in locked.group(1)
    assert cfg.ADMIN_ACCESS_ROLE in locked.group(1) and cfg.VIEW_ACCESS_ROLE in locked.group(1)
    assert "SNOW_ACCOUNTADMINS owns the app, so its members still open it" in warn
    assert "owns the app" in _frames(at).lower()                  # the table's MEANS reads the OWNERSHIP row
    return warn


def test_missing_usage_for_the_owner_never_claims_its_members_are_locked_out(access_app, monkeypatch):
    access_app["grants"] = _grant_frame(("OWNERSHIP", "ROLE", "SNOW_ACCOUNTADMINS"),
                                        ("USAGE", "ROLE", "SNOW_SYSADMINS"))
    warn = _assert_owner_is_not_locked_out(_open_access(access_app))
    # roles.sql grants all three missing roles (4.610.1): one re-run remedy names them all, nothing is pending
    rerun = re.search(r"No explicit USAGE grant for ([^:]*): roles\.sql grants one to each of them", warn)
    expected = ", ".join(r for r in cfg.APP_ACCESS_ROLES if r != "SNOW_SYSADMINS")
    assert rerun and rerun.group(1) == expected, warn
    assert not _pending_words_in(warn), warn
    # a roles.sql that grants only the SNOW_* roles (pinned): the re-run remedy for the role it grants, the
    # pending wording for the others, and the owner still never claimed locked out
    _manage_only_snow(monkeypatch)
    warn = _assert_owner_is_not_locked_out(_open_access(access_app))
    rerun = re.search(r"No explicit USAGE grant for ([^:]*): roles\.sql grants one to each of them", warn)
    assert rerun and rerun.group(1) == "SNOW_ACCOUNTADMINS", warn
    pending = re.search(r"No explicit USAGE grant for ([^:]*): not granted by roles\.sql yet", warn)
    assert pending and pending.group(1) == ", ".join(ar.pending_roles()), warn
