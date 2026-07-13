"""Company scoping tests, including the code<->V001-seed sync guard."""

import re
from pathlib import Path

from app import companies as co

MIGRATION = Path(__file__).resolve().parents[1] / "snowflake" / "migrations" / "V001__core.sql"


def test_kebarr1_is_alfa_despite_dual_roles():
    assert co.classify_user("KEBARR1") == "ALFA"
    assert co.classify_user("kebarr1") == "ALFA"


def test_trexis_prefix_users():
    assert co.classify_user("TRXS_LOADER") == "Trexis"
    # V044 (#18): no role evidence python-side -> UNKNOWN (the SQL UDF adds
    # role evidence; python mirror is prefix-only and says so honestly)
    assert co.classify_user("JSMITH") == "UNKNOWN"
    assert co.classify_user(None) == "UNKNOWN"   # no context = no evidence (V044)


def test_warehouse_classification():
    assert co.classify_warehouse("WH_TRXS_LOAD") == "Trexis"
    assert co.classify_warehouse("WH_TRXS_LINEAGE") == "Trexis"
    assert co.classify_warehouse("WH_ALFA_QUERY") == "ALFA"
    # V044 (#18): a warehouse with neither company's evidence is UNKNOWN —
    # it surfaces on Cost -> Chargeback until a COMPANY_SCOPE row maps it
    assert co.classify_warehouse("COMPUTE_WH") == "UNKNOWN"


def test_database_classification_and_environment():
    assert co.classify_database("TRXS_EDW_PRD") == "Trexis"
    assert co.classify_database("ALFA_EDW_PRD") == "ALFA"
    assert co.classify_environment("TRXS_EDW_PRD") == "PROD"
    assert co.classify_environment("ALFA_EDW_PRD") == "PROD"
    assert co.classify_environment("ALFA_EDW_DEV") == "NONPROD"


def test_warehouse_clause_partitions_the_account():
    trexis = co.warehouse_clause("Trexis")
    alfa = co.warehouse_clause("ALFA")
    assert "IN" in trexis and "WH_TRXS_LOAD" in trexis
    # V044 (#18): the account no longer partitions two-ways — ALFA needs
    # WH_ALFA_* evidence; UNKNOWN takes the residual
    assert "WH!_ALFA!_%" in alfa
    unknown = co.warehouse_clause("UNKNOWN")
    assert "NOT IN" in unknown and "WH_TRXS_LOAD" in unknown and "NOT LIKE" in unknown
    assert co.warehouse_clause("ALL") == ""


def test_user_clause_is_role_based_via_company_for_user():
    # Trexis users hold _TRXS_ roles (not TRXS_ names); scope routes through
    # COMPANY_FOR_USER, which carries the KEBARR1 override server-side.
    assert co.user_clause("ALFA") == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) = 'ALFA'"
    assert co.user_clause("Trexis") == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) = 'Trexis'"
    assert co.user_clause("ALL") == ""
    assert co.user_clause("ALFA", "U.NAME").endswith("(U.NAME) = 'ALFA'")


def test_clauses_pass_injection_gate():
    # assert_no_control_tokens runs inside every builder; a change that breaks
    # that contract should explode here, not in production.
    for company in co.COMPANIES:
        co.warehouse_clause(company)
        co.database_clause(company)
        co.user_clause(company)
    for env in co.ENVIRONMENTS:
        co.environment_clause(env)


def test_company_scope_seed_matches_code():
    """The COMPANY_SCOPE seed (across V001 + later migrations) must list every
    code rule — WH_TRXS_LINEAGE is seeded by V019."""
    sql = MIGRATION.read_text(encoding="utf-8")
    v019 = MIGRATION.parent / "V019__scoping_fixes.sql"
    if v019.exists():
        sql += v019.read_text(encoding="utf-8")
    for wh in co.TREXIS_WAREHOUSES:
        assert f"'{wh}'" in sql and "'Trexis'" in sql, f"seed missing warehouse {wh}"
    for db in co.TREXIS_DATABASES:
        assert re.search(rf"'Trexis'\s*,\s*'DATABASE'\s*,\s*'{db}'", sql), f"seed missing database {db}"
    assert re.search(r"'Trexis'\s*,\s*'USER_PREFIX'\s*,\s*'TRXS_'", sql), "seed missing user prefix rule"
    for user, company in co.USER_COMPANY_OVERRIDES.items():
        assert re.search(rf"'{company}'\s*,\s*'USER_OVERRIDE'\s*,\s*'{user}'", sql), f"seed missing override {user}"


def test_alfa_is_the_default_company_on_open():
    """Owner requirement 2026-07: the app must open scoped to ALFA.

    app/core/state.py seeds flt_company from DEFAULT_COMPANY and resets any
    invalid persisted value back to it, and the sidebar selectbox is bound to
    that state key — so this constant IS the open-the-app default.
    """
    assert co.DEFAULT_COMPANY == "ALFA"
    assert co.COMPANIES[0] == "ALFA"


def test_admin_roles_resolve_to_dba_profile():
    """Owner requirement 2026-07: DBA work runs as SNOW_ACCOUNTADMINS /
    SNOW_SYSADMINS — both must get the full DBA profile (all pages + Admin,
    operator-gated execution)."""
    from app.config import OPERATOR_PROFILES, PAGES_BY_PROFILE, resolve_role_profile

    for role in ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", "snow_sysadmins"):
        profile = resolve_role_profile(role)
        assert profile == "DBA", (role, profile)
        assert profile in OPERATOR_PROFILES
        assert "Admin" in PAGES_BY_PROFILE[profile]
