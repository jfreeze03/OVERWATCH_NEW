"""Locks for the Codex 'Next' batch items 7, 8a, 8b (2026-07-14)."""
from pathlib import Path
import pytest
from app import companies
from app.data import cost_sql
sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def test_calendar_storage_readers():
    for prior in (False, True):
        for sql in (cost_sql.storage_by_database_calendar("ALFA", prior=prior),
                    cost_sql.storage_by_database_calendar_live("ALFA", prior=prior)):
            assert "DATE_TRUNC('month'" in sql
            assert "FAILSAFE_BYTES" in sql and "DAYS_AVERAGED" in sql
            sqlglot.parse(sql, dialect="snowflake")
    assert "< CURRENT_DATE()" in cost_sql.storage_by_database_calendar("ALFA", prior=False)
    assert "DATEADD('month', -1" in cost_sql.storage_by_database_calendar("ALFA", prior=True)


def test_company_labels_use_evidence_udf_residual_unknown():
    assert companies.company_case_sql() == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)"
    assert companies.database_case_sql() == "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)"
    assert "ELSE 'ALFA'" not in companies.company_case_sql()
    assert "ELSE 'ALFA'" not in companies.database_case_sql()


def test_environment_chip_marked_picker_only():
    assert "Env (DB picker)" in (_ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_org_readers_document_latency_caveats():
    assert "72h" in (_ROOT / "app" / "data" / "cost_sql.py").read_text(encoding="utf-8")


def test_classify_databases_live_inventory():
    from app import companies
    names = ["ALFA_EDW_PRD", "TRXS_EDW_PRD", "ALFA_EDW_DEV", "SOMEDB_X", "admin"]
    alfa = companies.classify_databases(names, "ALFA")
    assert "ALFA_EDW_PRD" in alfa and "ADMIN" in alfa
    assert "TRXS_EDW_PRD" not in alfa and "SOMEDB_X" not in alfa
    assert companies.classify_databases(names, "Trexis") == ("TRXS_EDW_PRD",)
    prod = companies.classify_databases(names, "ALFA", "PROD")
    assert "ALFA_EDW_PRD" in prod and "ALFA_EDW_DEV" not in prod


def test_sidebar_db_picker_has_inventory_and_fallback():
    m = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "security_sql.show_databases_sql()" in m       # live inventory
    assert "databases_for(_company, _env)" in m           # offline fallback preserved
