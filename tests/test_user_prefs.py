"""V013 user prefs: key gating, CURRENT_USER scoping, escaping."""

from app.data import prefs_sql


def test_reader_scopes_to_current_user():
    sql = prefs_sql.user_prefs()
    assert "USER_NAME = CURRENT_USER()" in sql and "USER_PREFS" in sql


def test_v013_and_grants():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    v13 = (root / "snowflake" / "migrations" / "V013__user_prefs.sql").read_text(encoding="utf-8")
    assert "USER_PREFS" in v13 and "SELECT 13 AS VERSION" in v13
    roles = (root / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    # r26 (owner 2026-07-13): per-table grants collapsed into the blanket
    # ALL/FUTURE TABLES grants to the two admin roles.
    assert "ON ALL TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS" in roles
