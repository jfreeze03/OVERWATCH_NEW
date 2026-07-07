"""Chargeback builder invariants + V008 seed sync."""

import re
from pathlib import Path

import pytest

from app import companies
from app.data import chargeback_sql

V008 = Path(__file__).resolve().parents[1] / "snowflake" / "migrations" / "V008__chargeback.sql"


def test_department_window_is_exact_metering_with_unmapped_bucket():
    sql = chargeback_sql.department_window_credits(7, "ALFA")
    assert "WAREHOUSE_METERING_HISTORY" in sql
    assert "COALESCE(D.DEPARTMENT, 'Unmapped')" in sql
    assert "DEPARTMENT_MAP" in sql
    assert re.search(r"DATEADD\('day',\s*-7", sql)
    assert "NOT IN" in sql  # ALFA company scope


def test_role_share_normalizes_per_warehouse():
    sql = chargeback_sql.role_share_within_warehouse(7, "Trexis")
    assert "RATIO_TO_REPORT" in sql and "PARTITION BY WAREHOUSE_NAME" in sql
    assert "IN ('WH_TRXS_LOAD'" in sql
    assert "$" not in sql  # dollarization happens app-side at the exact wh spend


def test_month_statement_validates_month():
    good = chargeback_sql.department_month_credits("2026-06", "ALL")
    assert "DATE '2026-06-01'" in good and "DATEADD('month', 1" in good
    with pytest.raises(ValueError):
        chargeback_sql.department_month_credits("June 2026")
    with pytest.raises(ValueError):
        chargeback_sql.department_month_credits("2026-06-01'; DROP TABLE x;--")


def test_v008_seed_covers_known_warehouses():
    sql = V008.read_text(encoding="utf-8")
    v019 = V008.parent / "V019__scoping_fixes.sql"
    if v019.exists():
        sql += v019.read_text(encoding="utf-8")   # WH_TRXS_LINEAGE mapped here
    for wh in companies.TREXIS_WAREHOUSES:
        assert f"'{wh}'" in sql, f"chargeback seed missing {wh}"
    assert "'WH_ALFA_OVERWATCH'" in sql
    # Billing-truth posture is documented, not implied
    assert "Unmapped" in chargeback_sql.department_window_credits(7)


def test_role_department_lens_labels_unmapped():
    sql = chargeback_sql.role_department_map_join(7, "ALL")
    assert "MAP_TYPE = 'ROLE'" in sql
    assert "'Unmapped role'" in sql
