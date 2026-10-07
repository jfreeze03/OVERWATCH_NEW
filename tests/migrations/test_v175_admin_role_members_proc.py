"""Locks for V175 -- SP_ADMIN_ROLE_MEMBERS(), the owner-run admin-access lookup (owner decision 2026-10-06:
SNOW_SYSADMINS will own the app, design D4).

A NEW procedure (no re-derivation, so no generator and no lineage compare). What this file proves:
  * shape -- first line, guard (-20175, v < 174), one procedure, one grant, the idempotent version row;
  * the procedure -- EXECUTE AS OWNER, COPY GRANTS before RETURNS, the two-column table the app reads, one SHOW
    GRANTS OF ROLE whose role literal IS config.ADMIN_ACCESS_ROLE, read back by RESULT_SCAN(LAST_QUERY_ID()), no
    argument, no write, no other statement;
  * the grant -- USAGE to SNOW_SYSADMINS in the same file, after the CREATE (a re-run re-grants), and no other grant;
  * the app's names (access_sql) match the file, and the run docs list it.
"""

from __future__ import annotations

import re

import app.config as cfg
from app.data import access_sql
from tests._source import read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V175__admin_role_members_proc.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_VERSION = int(_NAME[1:4])
_PROC = f"{cfg.OVERWATCH_DB}.{cfg.CORE_SCHEMA}.{access_sql.ADMIN_MEMBERS_PROC}"
_GRANT = f"GRANT USAGE ON PROCEDURE {_PROC}() TO ROLE SNOW_SYSADMINS;"


def _create() -> str:
    s = _MIG.index(f"CREATE OR REPLACE PROCEDURE {_PROC}()")
    o = _MIG.index("$$", s)
    return _MIG[s:_MIG.index("$$;", o + 2) + 3]


def _body() -> str:
    c = _create()
    return c[c.index("$$") + 2:c.rindex("$$")]


def _code(text: str) -> str:
    """``text`` without -- comments (the header and the body note mention words the code must not contain)."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def test_first_line_guard_and_version_row():
    assert _MIG.splitlines()[0] == f"-- {_NAME}"
    assert "not_ready EXCEPTION (-20175, 'V175 requires V174 first - apply migrations in order.');" in _MIG
    assert "IF (v < 174) THEN" in _MIG
    assert _MIG.index("RAISE not_ready") < _MIG.index("CREATE OR REPLACE PROCEDURE")
    assert f"SELECT {_VERSION} AS VERSION," in _MIG
    assert f"WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = {_VERSION});" in _MIG
    assert _MIG.rstrip().endswith(f"WHERE VERSION = {_VERSION});")


def test_the_app_names_this_migration_and_procedure():
    assert access_sql.ADMIN_MEMBERS_MIGRATION == _VERSION
    assert access_sql.call_admin_role_members_sql() == f"CALL {_PROC}()"


def test_exactly_one_procedure_and_one_grant():
    code = _code(_MIG)
    assert len(re.findall(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\b", code)) == 1
    assert len(re.findall(r"\bGRANT\b", code)) == 1
    assert not re.search(r"\b(?:REVOKE|DROP|ALTER|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TASK|VIEW|TABLE|FUNCTION))\b", code)


def test_owner_rights_copy_grants_and_the_returned_table():
    head = _create()[:_create().index("$$")]
    assert re.fullmatch(
        rf"CREATE OR REPLACE PROCEDURE {re.escape(_PROC)}\(\)\nCOPY GRANTS\n"
        r'RETURNS TABLE \("granted_to" VARCHAR, "grantee_name" VARCHAR\)\nLANGUAGE SQL\nEXECUTE AS OWNER\nAS\n',
        head), head


def test_the_body_is_one_show_of_the_admin_role_read_back_and_returned():
    code = _code(_body())
    shows = re.findall(r"\bSHOW\s+GRANTS\s+OF\s+ROLE\s+(\w+)\s*;", code)
    assert shows == [cfg.ADMIN_ACCESS_ROLE]          # hard-coded, and exactly the role the app checks
    assert len(re.findall(r"\bSHOW\b", code)) == 1
    assert 'res := (SELECT "granted_to", "grantee_name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));' in code
    assert code.index("SHOW GRANTS") < code.index("RESULT_SCAN") < code.index("RETURN TABLE(res);")
    # read-only: no write, no call, no dynamic SQL
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|CALL|EXECUTE\s+IMMEDIATE|GRANT|CREATE|ALTER|DROP)\b",
                         code)


def test_usage_for_snow_sysadmins_in_the_same_file_after_the_create():
    assert _MIG.count(_GRANT) == 1
    assert _MIG.index(_GRANT) > _MIG.index("$$;", _MIG.index("CREATE OR REPLACE PROCEDURE"))
    assert _MIG.index(_GRANT) < _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")


def test_the_procedure_compiles_in_the_syntax_sweep():
    # the shared proc-syntax lock strips comments and string literals; the stripped body still holds the SHOW
    assert "SHOW GRANTS OF ROLE" in _strip_noise(_body())


def test_run_docs_list_v175():
    assert f"snowflake/migrations/{_NAME}" in read("DEPLOYMENT.md")
    assert f"snowflake/migrations/{_NAME} -- " in read("README.md")
    assert "**Rolling back V175.**" in read("RUNBOOK.md")
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS();" in read("snowflake/teardown.sql")
