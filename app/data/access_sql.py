"""App-access metadata statements (v4.610.0, owner decision 2026-10-05).

SHOW GRANTS OF ROLE lists who holds a role: one row per grantee, with ``granted_to`` (USER / ROLE / ...)
and ``grantee_name``. session.viewer_access() runs it once per viewer session, as the app OWNER (owner's
rights), to decide whether the viewer is a DIRECT user grantee of config.ADMIN_ACCESS_ROLE. It is a
metadata command (cloud services, no warehouse scan, no ACCOUNT_USAGE read).

Owner's-rights Streamlit bars only the SHOW GRANTS forms with NO IN / ON / TO / OF clause, so the OF ROLE
form is allowed. SHOW lists only what the current role can see: an EMPTY answer is a privilege gap, not
proof of "no members", and the caller treats it as unverified (read-only), never as a verified answer.

SHOW GRANTS ON STREAMLIT lists who holds a privilege on the app itself (USAGE = who can open it; OWNERSHIP =
whose rights every viewer runs with). Admin ▸ App access reads it (read-only) to compare the USAGE grantees
with the four config.APP_ACCESS_ROLES the 2026-10-05 decision names (since 4.610.1 snowflake/roles.sql grants
all four and its -20011/-20012 proof block accepts exactly that set). The ON form is
allowed in owner's-rights code, and the owner role owns the app, so it always sees at least its own
OWNERSHIP row: an empty answer is unverified, never "no grantees".

V175 (owner decision 2026-10-06: SNOW_SYSADMINS will own the app) wraps the admin-role SHOW in
SP_ADMIN_ROLE_MEMBERS(), an EXECUTE AS OWNER procedure with the role hard-coded. SHOW answers as the APP owner;
the CALL answers as the PROCEDURE's owner (the role that applied V175), so the lookup keeps listing the same
members after the app changes owner. session._admin_lookup_sql picks the CALL once V175 is applied.

Not canaried: the Admin canary EXPLAINs every entry and EXPLAIN cannot compile a SHOW or a CALL (see
app/data/canary.py and tests/test_canary_coverage.py's CANARY_EXEMPT).
"""

from __future__ import annotations

import re

from app.config import ADMIN_ACCESS_ROLE, APP_STREAMLIT_NAME, CORE_SCHEMA, OVERWATCH_DB, core_object

# V175: the owner-run admin-role lookup and the migration that creates it (session gates the CALL on it).
ADMIN_MEMBERS_PROC = "SP_ADMIN_ROLE_MEMBERS"
ADMIN_MEMBERS_MIGRATION = 175

# An UNQUOTED Snowflake identifier: a letter or underscore, then letters, digits, '_' or '$' (255 max).
# Nothing else can reach the statement, so neither a quote nor a ';' can.
_ROLE_RE = re.compile(r"[A-Z_][A-Z0-9_$]{0,254}")


def _role_ident(role: object) -> str:
    """``role`` as a double-quoted identifier, or ValueError. Upper-cased first: an unquoted role name is
    stored upper-case, so quoting the raw lower-case text would name a different (missing) role."""
    name = str(role).strip().upper() if role is not None else ""
    if not _ROLE_RE.fullmatch(name):
        raise ValueError(f"Invalid role name: {role!r}")
    return f'"{name}"'


def show_grants_of_role_sql(role: str = ADMIN_ACCESS_ROLE) -> str:
    """Who holds ``role``: the admin-access lookup (and, on Admin ▸ App access, the view-role roster).

    No LIMIT and no run() row cap: the caller collects every row (a role's grantee list is small), and a
    truncated roster could drop the very viewer being checked. Only config constants are passed in
    practice; the identifier check is the injection defence regardless."""
    return f"SHOW GRANTS OF ROLE {_role_ident(role)}"


def show_grants_on_app_sql() -> str:
    """Who can open OVERWATCH: every privilege granted ON the deployed Streamlit (snowflake.yml's
    DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP). Admin ▸ App access reads it through run(..., max_rows=0) and compares
    the USAGE rows with config.APP_ACCESS_ROLES (logic.access_review.app_grant_review). Takes no input."""
    return f"SHOW GRANTS ON STREAMLIT {OVERWATCH_DB}.{CORE_SCHEMA}.{APP_STREAMLIT_NAME}"


def call_admin_role_members_sql() -> str:
    """The V175 admin-access lookup: SP_ADMIN_ROLE_MEMBERS() runs SHOW GRANTS OF ROLE <ADMIN_ACCESS_ROLE> as the
    procedure's owner and returns the same granted_to / grantee_name rows, so role_grant_members reads either
    answer. The procedure takes no argument (the role is hard-coded in V175), and neither does this builder.

    A read: session._admin_role_rows collects it directly, never through the write executor, so it needs no
    admin entitlement (it is what decides entitlement) and invalidates no cache domain."""
    # the literal name keeps the CALL visible to tests/test_proc_domain_invalidation.py's grep (a lock there
    # requires it to equal ADMIN_MEMBERS_PROC)
    return f"CALL {core_object('SP_ADMIN_ROLE_MEMBERS')}()"
