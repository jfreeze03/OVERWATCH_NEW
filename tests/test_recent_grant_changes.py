"""Recent grant-changes feed (owner ask 2026-08-17): a granular, time-ordered
"who granted what to whom, and when" log across roles/users/objects — the gap the
aggregated DDL panel didn't fill. Locks the SQL + its wiring into Security."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import security_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_feed_unions_user_and_object_grants_both_directions():
    sql = security_sql.recent_grant_changes(30)
    # both sources: role->user AND privilege->role (objects).
    assert "GRANTS_TO_USERS" in sql and "GRANTS_TO_ROLES" in sql
    # each grant AND revoke is its own event (CREATED_ON / DELETED_ON arms).
    assert sql.count("'GRANTED'") == 2 and sql.count("'REVOKED'") == 2
    assert "CREATED_ON >= DATEADD" in sql and "DELETED_ON >= DATEADD" in sql
    # the who/what/whom/when columns.
    for col in ("CHANGED_AT", "CHANGE", "GRANT_TYPE", "CHANGED_BY", "GRANTEE", "WHAT"):
        assert col in sql, col
    # object grants describe the privilege on the object.
    assert "PRIVILEGE || ' ON ' || GRANTED_ON" in sql
    # newest first; system grantor is labeled, not blank.
    assert "ORDER BY CHANGED_AT DESC" in sql
    assert "'(system)'" in sql


def test_feed_window_and_limit_are_bounded():
    assert "DATEADD('day', -30," in security_grant_sql(30)
    assert "DATEADD('day', -365," in security_grant_sql(99999)   # window cap
    assert "LIMIT 10\n" in security_sql.recent_grant_changes(30, limit=1)      # floor
    assert "LIMIT 2000" in security_sql.recent_grant_changes(30, limit=99999)  # cap


def security_grant_sql(days: int) -> str:
    return security_sql.recent_grant_changes(days)


def test_feed_scopes_by_company_via_grantee():
    # owner ask 2026-08-17: the triage company filter must apply here.
    trxs = security_sql.recent_grant_changes(30, "Trexis")
    assert "LIKE '%TRXS%'" in trxs          # role-grantee heuristic (role_clause)
    assert "COMPANY_FOR_USER" in trxs        # user-grantee classification (user_clause)
    account_wide = security_sql.recent_grant_changes(30, "ALL")
    assert "TRXS" not in account_wide and "COMPANY_FOR_USER" not in account_wide  # ALL = no-op
    # the UI passes the active company filter (cache key includes it).
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "recent_grant_changes(int(_gc_days), company)" in src
    assert "grant_changes_feed_{company}_" in src


def test_feed_is_wired_into_the_changes_section():
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "recent_grant_changes(" in src
    assert "Recent grant changes" in src
    # rendered ABOVE the existing aggregated DDL/DCL panel.
    assert src.index("Recent grant changes") < src.index("Who changed what (DDL/DCL)")


def test_feed_parses_under_sqlglot():
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(security_sql.recent_grant_changes(30), dialect="snowflake")
