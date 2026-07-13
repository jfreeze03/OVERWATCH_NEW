"""V013 user prefs: key gating, CURRENT_USER scoping, escaping."""

import pytest

from app.data import prefs_sql


def test_reader_scopes_to_current_user():
    sql = prefs_sql.user_prefs()
    assert "USER_NAME = CURRENT_USER()" in sql and "USER_PREFS" in sql


def test_upsert_is_merge_scoped_and_escaped():
    sql = prefs_sql.upsert_pref_sql("VIEW:Trexis prod 7d", '{"a": "it\'s"}')
    assert sql.startswith("MERGE INTO")
    assert "CURRENT_USER() AS U" in sql
    assert "'VIEW:Trexis prod 7d'" in sql
    assert "it''s" in sql                       # single quotes doubled


def test_bad_keys_rejected():
    for bad in ("", "VIEW:", "VIEW:" + "x" * 41, "OTHER", "VIEW:bad;drop", "DEFAULT_VIEW2"):
        with pytest.raises(ValueError):
            prefs_sql.upsert_pref_sql(bad, "{}")
        with pytest.raises(ValueError):
            prefs_sql.delete_pref_sql(bad)


def test_delete_scoped_to_current_user():
    sql = prefs_sql.delete_pref_sql("DEFAULT_VIEW")
    assert "USER_NAME = CURRENT_USER()" in sql and "'DEFAULT_VIEW'" in sql


def test_v013_and_grants():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    v13 = (root / "snowflake" / "migrations" / "V013__user_prefs.sql").read_text(encoding="utf-8")
    assert "USER_PREFS" in v13 and "SELECT 13 AS VERSION" in v13
    roles = (root / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    # r26 (owner 2026-07-13): per-table grants collapsed into the blanket
    # ALL/FUTURE TABLES grants to the two admin roles.
    assert "ON ALL TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS" in roles
