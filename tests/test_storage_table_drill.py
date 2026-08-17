"""Storage table-level drill (owner ask 2026-08-17): drill the per-database
storage bars down to the TABLE level with dollars, and surface time-travel /
fail-safe as reduce-retention / purge candidates. Locks the SQL + wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_breakdown_splits_all_four_components_ordered_by_total():
    sql = insights_sql.table_storage_breakdown("ALFA", "ALFA_EDW_PRD")
    assert "TABLE_STORAGE_METRICS" in sql
    # all four billed components — clone-retained included so the drill reconciles
    # with the per-database card (reviewer 2026-08-17), not just active/TT/fail-safe.
    for col in ("ACTIVE_GB", "TIME_TRAVEL_GB", "FAILSAFE_GB", "CLONE_GB", "TOTAL_GB"):
        assert col in sql, col
    assert "RETAINED_FOR_CLONE_BYTES" in sql
    # biggest cost drivers lead (total bytes incl. clone), NOT the waste ordering.
    assert "ORDER BY m.ACTIVE_BYTES + m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES" in sql
    assert "COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) DESC" in sql
    # staleness (no DML in 90d) + current retention + dropped-flag for the decision.
    assert "TABLE_DML_HISTORY" in sql and "'STALE'" in sql
    assert "RETENTION_DAYS" in sql
    assert "AS DROPPED" in sql
    # scoped to the one database the user drilled into.
    assert "UPPER(m.TABLE_CATALOG) IN ('ALFA_EDW_PRD')" in sql


def test_breakdown_is_injection_safe_and_bounded():
    # company is CLASSIFIED (never echoed), so a hostile value can't reach the SQL.
    assert "DROP--" not in insights_sql.table_storage_breakdown("ALFA'; DROP--")
    # database IS echoed (it names a real catalog), but sql_literal doubles any
    # quote so it stays a contained string literal — no statement break-out.
    dsql = insights_sql.table_storage_breakdown("ALFA", "X'; DROP--")
    assert "'X''; DROP--'" in dsql          # doubled quote = safely escaped
    assert "IN ('X'; DROP" not in dsql       # NOT an unescaped break-out
    # window bounds.
    assert "LIMIT 5\n" in insights_sql.table_storage_breakdown("ALFA", limit=1)  # floor
    assert "LIMIT 500" in insights_sql.table_storage_breakdown("ALFA", limit=99999)  # cap


def test_breakdown_without_database_still_scopes_by_company():
    sql = insights_sql.table_storage_breakdown("ALFA")
    assert "TABLE_STORAGE_METRICS" in sql
    # company clause present; no single-database equality clause.
    assert "IN ('ALFA_EDW_PRD')" not in sql


def test_drill_is_wired_into_the_storage_tab_with_dollars_and_retention():
    ui = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "def _storage_table_drill" in ui
    assert "_storage_table_drill(company, settings," in ui  # called from _storage_tab
    assert "table_storage_breakdown" in ui
    # dollars, honest non-active framing (not "reclaimable"), clone column, ALTER handoff.
    assert "Non-active $" in ui and "Clone $" in ui
    assert "Reclaimable $" not in ui            # overstated label removed per review
    assert "Top-50 table storage" in ui         # KPI not sold as the whole-DB total
    assert "no WRITES in 90 days" in ui or "no consumers" in ui  # STALE reads caveat
    assert "retention_fix(" in ui


def test_breakdown_parses_under_sqlglot():
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(insights_sql.table_storage_breakdown("ALFA", "ALFA_EDW_PRD"), dialect="snowflake")
