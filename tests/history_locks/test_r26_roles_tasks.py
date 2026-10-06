"""r26 locks (owner 2026-07-13): "the only roles that will have access is
SNOW_ACCOUNTADMINS and SNOW_SYSADMINS. remove any traces of other roles.
also remove task monitor references. the app is producing a number of
access error messages."

The surviving invariant: roles.sql actively retires the old two-role layer,
and its object grants go to the two SNOW_* roles only. Since the owner
decision of 2026-10-05 (roles.sql change 2026-10-06, 4.610.1) it also grants
SNOW_PRI_GFR_PRD_ALFA_DSA and SNOW_PRI_GFR_PRD_ALFA_DTI USAGE on the
database, schema and app, and nothing else.

(The task-absence half of this file was retired 2026-07-13 by the owner's
correction — "i meant getting rid of resource monitor, not task monitoring"
— task monitoring is restored in v4.45.0 / V045.)
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TO_ROLE = re.compile(r"\bTO\s+ROLE\b", re.IGNORECASE)
_CREATE_ROLE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?ROLE\b", re.IGNORECASE)
_FOUR = {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", "SNOW_PRI_GFR_PRD_ALFA_DSA", "SNOW_PRI_GFR_PRD_ALFA_DTI"}
# DSA/DTI open the app and nothing else: USAGE on the database, the schema and the app, exactly once each
_DSA_DTI_ALLOWED = sorted({"GRANT USAGE ON DATABASE DBA_MAINT_DB",
                           "GRANT USAGE ON SCHEMA DBA_MAINT_DB.OVERWATCH",
                           "GRANT USAGE ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"})


def _code_lines(sql: str) -> list[str]:
    return [line for line in sql.splitlines() if not line.lstrip().startswith("--")]


def _role_grants(sql: str) -> list[tuple[str, str]]:
    """(what, grantee) for every non-comment line that grants TO ROLE, upper-cased first: SQL keywords and
    unquoted names are case-insensitive, so a lowercase `grant ... to role ...` line counts like any other
    (review 4.610.1 #2)."""
    out = []
    for line in _code_lines(sql):
        parts = _TO_ROLE.split(line.upper(), maxsplit=1)
        if len(parts) == 2:
            out.append((" ".join(parts[0].split()), parts[1].strip().rstrip(";").strip()))
    return out


def _grant_problems(roles: str) -> list[str]:
    """Why roles.sql breaks the access-role invariant ([] when it holds)."""
    problems: list[str] = []
    grants = _role_grants(roles)
    grantees = {grantee for _what, grantee in grants}
    if grantees != _FOUR:
        problems.append(f"grantees {sorted(grantees)} != the four access roles")
    for role in ("SNOW_PRI_GFR_PRD_ALFA_DSA", "SNOW_PRI_GFR_PRD_ALFA_DTI"):
        mine = sorted(what for what, grantee in grants if grantee == role)
        if mine != _DSA_DTI_ALLOWED:
            problems.append(f"{role} gets {mine}")
    if any(_CREATE_ROLE.search(line) for line in _code_lines(roles)):
        problems.append("a CREATE ROLE (no custom layer returns)")
    return problems


def test_roles_sql_grants_only_the_four_access_roles():
    # owner decision 2026-10-05 (roles.sql change 2026-10-06, 4.610.1) supersedes 2026-07-13's two roles
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "DROP ROLE IF EXISTS OVERWATCH_OPERATOR;" in roles
    assert "DROP ROLE IF EXISTS OVERWATCH_MONITOR;" in roles
    assert _grant_problems(roles) == []


def test_the_grant_lock_is_case_insensitive():
    """review 4.610.1 #2: Snowflake runs a lowercase or mixed-case grant like an upper-case one, so the lock
    must see it: an extra grant to DSA/DTI, a repeated one, a grant to another role, or a CREATE ROLE."""
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    for extra, why in (
        ("grant imported privileges on database snowflake to role snow_pri_gfr_prd_alfa_dsa;", "DSA gets"),
        ("Grant Select On All Tables In Schema dba_maint_db.overwatch To  Role snow_pri_gfr_prd_alfa_dti;",
         "DTI gets"),
        ("grant usage on database dba_maint_db to role snow_pri_gfr_prd_alfa_dsa;", "DSA gets"),   # twice
        ("grant usage on database dba_maint_db to role public;", "grantees"),
        ("create role overwatch_viewer;", "CREATE ROLE"),
    ):
        problems = " | ".join(_grant_problems(f"{roles}\n{extra}\n"))
        assert why in problems, (extra, problems)
    # a commented-out grant is not a grant
    assert _grant_problems(f"{roles}\n-- grant usage on database dba_maint_db to role public;\n") == []


def test_break_glass_panels_watch_the_admin_holder_roles():
    # the two SNOW_* roles, plus SNOW_PRI_GFR_PRD_ALFA_DSA since the owner decision of 2026-10-05 made
    # its direct members OVERWATCH admins (tests/test_security_admin_tiers.py)
    from app.data import security_sql
    for sql in (security_sql.admin_role_holders(), security_sql.new_network_logins(7)):
        assert "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')" in sql
        assert "SECURITYADMIN" not in sql and "ORGADMIN" not in sql
        assert "SNOW_PRI_GFR_PRD_ALFA_DTI" not in sql
