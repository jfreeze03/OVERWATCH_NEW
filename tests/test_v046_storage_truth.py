"""Locks for V046 storage-truth + the 2026-07-14 cost audit fixes (F1-F4).

Authority: docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md.
F1  per-DB storage priced on the monthly average of daily bytes; account tiers
    (stage/hybrid/archive) added via FACT_STORAGE_ACCOUNT_DAILY (R3).
F2  live allocation stays elapsed-share (lock-tested global-share law); the UI
    caveats it and the mart path stays the size-aware default.
F3  storage TB base documented (binary TiB; org usage is billing truth).
F4  warehouse_daily_credits compute column no longer falls back to total.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS
from app.data import cost_sql

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V46 = (_MIG / "V046__storage_truth.sql").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# V046 migration shape
# ---------------------------------------------------------------------------

def test_v046_guard_version_and_house_rules():
    assert "EXCEPTION (-20046" in _V46
    assert "RAISE not_ready;" in _V46 and "RAISE EXCEPTION (" not in _V46
    assert "IF (v < 45) THEN" in _V46
    assert "SELECT 46 AS VERSION" in _V46


def test_v046_creates_fact_proc_task():
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY" in _V46
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH" in _V46
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_STORAGE_TRUTH" in _V46
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_STORAGE_TRUTH RESUME" in _V46
    # sources every tier from the one account-level view
    for col in ("STORAGE_BYTES", "STAGE_BYTES", "FAILSAFE_BYTES",
                "HYBRID_TABLE_STORAGE_BYTES", "ARCHIVE_STORAGE_COOL_BYTES",
                "ARCHIVE_STORAGE_COLD_BYTES"):
        assert col in _V46
    assert "SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE" in _V46


def test_v046_seeds_tier_rates_without_clobbering():
    for key in ("STORAGE_STAGE_USD_PER_TB_MONTH", "STORAGE_HYBRID_USD_PER_TB_MONTH",
                "STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH", "STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH"):
        assert key in _V46
    assert "WHEN NOT MATCHED THEN INSERT (KEY, VALUE)" in _V46  # never overwrites an Admin edit


def test_v046_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V46):
        sqlglot.parse(stmt, dialect="snowflake")


# ---------------------------------------------------------------------------
# F1b — account-tier readers (account grain, monthly average)
# ---------------------------------------------------------------------------

def test_account_storage_readers_average_and_parse():
    fact = cost_sql.storage_account_truth(30)
    live = cost_sql.storage_account_truth_live(30)
    assert "FACT_STORAGE_ACCOUNT_DAILY" in fact
    assert "SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE" in live
    for sql in (fact, live):
        assert "AVG(COALESCE(" in sql
        assert "DAYS_AVERAGED" in sql
        for col in ("HYBRID_BYTES", "ARCHIVE_COOL_BYTES", "ARCHIVE_COLD_BYTES"):
            assert col in sql
        sqlglot.parse(sql, dialect="snowflake")


def test_account_readers_registered_in_canary():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "cost.storage_account_truth" in canary
    assert "cost.storage_account_truth_live" in canary


def test_storage_tab_wires_account_tiers():
    cb = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    assert "def _account_storage_tiers" in cb
    assert "_account_storage_tiers(company, days, settings)" in cb


def test_tier_rate_defaults_present():
    for key in ("STORAGE_STAGE_USD_PER_TB_MONTH", "STORAGE_HYBRID_USD_PER_TB_MONTH",
                "STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH", "STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH"):
        assert key in DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# F1a — per-database storage on the monthly-average billing basis
# ---------------------------------------------------------------------------

def test_per_db_storage_is_windowed_average_not_snapshot():
    for sql in (cost_sql.storage_by_database(90, "ALFA"),
                cost_sql.storage_by_database_live(90, "ALFA")):
        assert "AVG(COALESCE(" in sql and "DAYS_AVERAGED" in sql
        assert "QUALIFY DAY = MAX(DAY) OVER ()" not in sql
        sqlglot.parse(sql, dialect="snowflake")


# ---------------------------------------------------------------------------
# F2 — allocation caveat + size note (live stays elapsed-share by design)
# ---------------------------------------------------------------------------

def test_allocation_caveat_and_size_note():
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "warehouse-size-blind" in sp and "size-aware" in sp
    # the locked global-share law + one-formula contract still hold
    assert 'alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"].map(safe_float) * window_usd' in sp
    cs = (_ROOT / "app" / "data" / "cost_sql.py").read_text(encoding="utf-8")
    assert "mart27_sql.alloc_attribution" in cs and "warehouse-size-blind" in cs
    # live builder keeps the tested global elapsed-share law (unchanged)
    live = cost_sql.allocated_attribution(7, "USER_NAME", "ALFA")
    assert "(SELECT SUM(ELAPSED_MS) FROM scoped)" in live and "RATIO_TO_REPORT" not in live


# ---------------------------------------------------------------------------
# F3 — TB base documented
# ---------------------------------------------------------------------------

def test_tb_base_documented():
    formulas = (_ROOT / "app" / "logic" / "formulas.py").read_text(encoding="utf-8")
    assert "Storage TB base" in formulas and "binary" in formulas


# ---------------------------------------------------------------------------
# F4 — compute column no longer inherits cloud services on a NULL
# ---------------------------------------------------------------------------

def test_warehouse_compute_credits_dont_fall_back_to_total():
    sql = cost_sql.warehouse_daily_credits(7, "ALFA")
    assert "SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE" in sql
    assert "COALESCE(CREDITS_USED_COMPUTE, CREDITS_USED)" not in sql


# ---------------------------------------------------------------------------
# Validate gate advanced
# ---------------------------------------------------------------------------

def test_validate_gate_at_v046():
    import re
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    m = re.search(r"V001\.\.V(\d+) applied", val)
    assert m and int(m.group(1)) >= 46
