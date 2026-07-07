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
    assert co.classify_user("JSMITH") == "ALFA"
    assert co.classify_user(None) == "ALFA"


def test_warehouse_classification():
    assert co.classify_warehouse("WH_TRXS_LOAD") == "Trexis"
    assert co.classify_warehouse("COMPUTE_WH") == "ALFA"


def test_database_classification_and_environment():
    assert co.classify_database("TRXS_EDW_PRD") == "Trexis"
    assert co.classify_database("ALFA_EDW_PROD") == "ALFA"
    assert co.classify_environment("TRXS_EDW_PRD") == "PROD"
    assert co.classify_environment("ALFA_EDW_PROD") == "PROD"
    assert co.classify_environment("ALFA_EDW_DEV") == "NONPROD"


def test_warehouse_clause_partitions_the_account():
    trexis = co.warehouse_clause("Trexis")
    alfa = co.warehouse_clause("ALFA")
    assert "IN" in trexis and "WH_TRXS_LOAD" in trexis
    assert "NOT IN" in alfa and "WH_TRXS_LOAD" in alfa
    assert co.warehouse_clause("ALL") == ""


def test_user_clause_carries_the_override_both_directions():
    alfa = co.user_clause("ALFA")
    trexis = co.user_clause("Trexis")
    # ALFA scope must include KEBARR1 even though prefix rules alone wouldn't.
    assert "KEBARR1" in alfa
    # Trexis scope must exclude KEBARR1 explicitly.
    assert "KEBARR1" in trexis and "NOT IN" in trexis


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
    """V001's COMPANY_SCOPE seed must list exactly the code's rules."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for wh in co.TREXIS_WAREHOUSES:
        assert re.search(rf"'Trexis'\s*,\s*'WAREHOUSE'\s*,\s*'{wh}'", sql), f"seed missing warehouse {wh}"
    for db in co.TREXIS_DATABASES:
        assert re.search(rf"'Trexis'\s*,\s*'DATABASE'\s*,\s*'{db}'", sql), f"seed missing database {db}"
    assert re.search(r"'Trexis'\s*,\s*'USER_PREFIX'\s*,\s*'TRXS_'", sql), "seed missing user prefix rule"
    for user, company in co.USER_COMPANY_OVERRIDES.items():
        assert re.search(rf"'{company}'\s*,\s*'USER_OVERRIDE'\s*,\s*'{user}'", sql), f"seed missing override {user}"
