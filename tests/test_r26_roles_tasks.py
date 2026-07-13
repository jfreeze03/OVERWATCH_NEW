"""r26 locks (owner 2026-07-13): "the only roles that will have access is
SNOW_ACCOUNTADMINS and SNOW_SYSADMINS. remove any traces of other roles.
also remove task monitor references. the app is producing a number of
access error messages."

The surviving invariant: roles.sql grants to exactly the two SNOW_* roles
and actively retires the old two-role layer.

(The task-absence half of this file was retired 2026-07-13 by the owner's
correction — "i meant getting rid of resource monitor, not task monitoring"
— task monitoring is restored in v4.45.0 / V045.)
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_roles_sql_grants_only_the_two_snow_roles():
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "DROP ROLE IF EXISTS OVERWATCH_OPERATOR;" in roles
    assert "DROP ROLE IF EXISTS OVERWATCH_MONITOR;" in roles
    grantees = {line.split("TO ROLE", 1)[1].strip().rstrip(";")
                for line in roles.splitlines() if "TO ROLE" in line}
    assert grantees == {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"}, grantees
    assert "CREATE ROLE" not in roles                      # no custom layer returns


def test_break_glass_panels_watch_the_two_real_roles():
    from app.data import security_sql
    for sql in (security_sql.admin_role_holders(), security_sql.new_network_logins(7)):
        assert "'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'" in sql
        assert "SECURITYADMIN" not in sql and "ORGADMIN" not in sql
