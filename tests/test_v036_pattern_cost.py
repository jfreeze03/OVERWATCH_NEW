"""Locks for V036 + the boss chart (v4.26.0, owner ask 2026-07-11)."""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V036__pattern_cost_mart.sql").read_text(encoding="utf-8")


def test_v036_guard_shape_and_pieces():
    assert "EXCEPTION (-20036" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                       # the V035 lesson holds
    assert "IF (v < 35) THEN" in _MIG and "SELECT 36 AS VERSION" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(30);" in _MIG
    body = _MIG.split("SP_LOAD_PATTERN_COST", 1)[1]
    assert "COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME)" in body     # V030 shape law
    assert "COMPANY_FOR_WAREHOUSE(MAX(" not in _MIG
    assert "CREDITS_ATTRIBUTED_COMPUTE" in body                  # measured, not estimated
    assert "QUERY_PARAMETERIZED_HASH IS NOT NULL" in body
    view = _MIG.split("MART_SOURCE_FRESHNESS AS", 1)[1].split("ALTER TASK", 1)[0]
    assert view.count("UNION ALL") == 18                         # 19th arm
    tail = _MIG.split("TASK_PATTERN_COST_DAILY RESUME", 1)[1]
    assert "TASK_LOCK_WAIT_DAILY RESUME" in tail                 # siblings resume
    assert "TASK_LOAD_DAILY RESUME" in tail
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY;" in teardown


def test_pattern_reader_is_measured_scoped_and_floored():
    sql = mart27_sql.pattern_cost(30, "ALFA", 25)
    assert "SUM(p.CREDITS_ATTRIBUTED)" in sql and "SUM(CREDITS_ATTRIBUTED)" not in sql
    assert "(p.COMPANY = 'ALFA' OR UPPER(p.COMPANY) = 'ALL')" in sql
    assert "HAVING SUM(p.CREDITS_ATTRIBUTED) > 0.01" in sql      # noise floor
    assert "''" in mart27_sql.pattern_cost(30, "x'y")
    uc = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "Repeated patterns — the silent spend (measured $)" in uc
    assert 'key=f"patterns_{company}_{days}"' in uc              # triage filter honored
    assert "probe=True" in uc.split("patterns_", 1)[1][:300]     # quiet pre-V036


def test_monthly_boss_chart_mart_first_and_honest():
    m = mart27_sql.monthly_spend_by_warehouse(12, "ALFA")
    assert "MART_WAREHOUSE_EFFICIENCY_DAILY" in m
    assert "(c.COMPANY = 'ALFA' OR UPPER(c.COMPANY) = 'ALL')" in m
    live = mart27_sql.live_monthly_spend_by_warehouse(12, "ALFA")
    assert "WAREHOUSE_METERING_HISTORY" in live
    assert "COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME)" in live     # UDF outside aggregation
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "Monthly spend by warehouse" in ov
    assert 'key=f"ov_monthly_{company}"' in ov
    assert "partial, not a drop" in ov                           # house honesty rule
    ch = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
    assert "def monthly_stacked_usd" in ch
    assert 'alt.condition("datum._PARTIAL"' in ch                # dimmed in-flight month
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("monthly_spend_by_warehouse", "live_monthly_spend_by_warehouse", "pattern_cost"):
        assert f"mart27_sql.{name}" in canary, name
