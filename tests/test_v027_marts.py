"""Locks for V027 — the mart family (v4.10.0).

Pins: migration bookkeeping, per-mart loader isolation, the chained tasks
(suspend-before-child, resume-all), the freshness arms, the telemetry rider,
real cache-hit detection, graceful pre-apply degrades, and canaried readers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V027__mart_family.sql").read_text(encoding="utf-8")

_MARTS = ("MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_QUERY_FAMILY_DAILY",
          "FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY",
          "MART_COST_ALLOCATION_DAILY", "MART_TASK_GRAPH_DAILY",
          "MART_SECURITY_POSTURE_DAILY", "MART_INCIDENT_TIMELINE",
          "FACT_AI_USAGE_DAILY")

# ---------------------------------------------------------------------------
# Migration bookkeeping + structure
# ---------------------------------------------------------------------------

def test_v027_guard_and_version():
    assert "EXCEPTION (-20027" in _MIG
    assert "IF (v < 26) THEN" in _MIG
    assert "SELECT 27 AS VERSION" in _MIG


def test_v027_creates_all_nine_marts_with_load_ts():
    for mart in _MARTS:
        assert f"CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.{mart}" in _MIG, mart
    # every mart carries LOAD_TS so the freshness view treats them uniformly
    assert _MIG.count("LOAD_TS TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()") == 9


def test_v027_loader_isolation_per_mart():
    body = _MIG.split("SP_LOAD_MARTS_V27", 1)[1]
    # 10 guarded blocks: 7 hourly-leg loads + posture + two AI arms
    assert body.count("WHEN OTHER THEN") >= 9
    assert body.count("'mart_load_failed'") >= 9
    assert "other marts unaffected" in body


def test_v027_one_loader_codepath_with_days_back():
    assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in _MIG
    assert "GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))" in _MIG   # clamped
    backfill = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    assert "SP_LOAD_MARTS_V27('HOURLY', 90)" in backfill               # same proc, big window
    assert "SP_LOAD_MARTS_V27('DAILY', 365)" in backfill


def test_v027_tasks_chain_and_roots_resume():
    # child creation requires suspended roots; everything resumes after
    assert "TASK_LOAD_HOURLY SUSPEND" in _MIG and "TASK_LOAD_DAILY SUSPEND" in _MIG
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY" in _MIG
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY" in _MIG
    tail = _MIG.split("TASK_LOAD_MARTS_V27_DAILY RESUME", 1)[1]
    assert "TASK_REFRESH_EXEC_BOARD RESUME" in tail                    # existing child too
    assert "TASK_LOAD_HOURLY RESUME" in tail and "TASK_LOAD_DAILY RESUME" in tail
    # first fill so panels aren't empty until the next task tick
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 3);" in _MIG


def test_v027_freshness_covers_every_mart():
    view = _MIG.split("MART_SOURCE_FRESHNESS AS", 1)[1].split("ALTER TASK", 1)[0]
    for mart in _MARTS:
        assert f"'{mart}'" in view, mart
    assert view.count("UNION ALL") == 15                               # 7 original + 9 arms


def test_v027_telemetry_rider_columns():
    for col in ("CACHE_HIT BOOLEAN", "SQL_HASH VARCHAR(64)", "BATCH_SIZE NUMBER(6,0)",
                "TRUNCATED BOOLEAN", "EVENT_KIND VARCHAR(40)", "IS_RERUN BOOLEAN"):
        assert col in _MIG, col


def test_teardown_covers_v027():
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    for mart in _MARTS:
        assert f"DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.{mart};" in teardown, mart
    assert "SP_LOAD_MARTS_V27(VARCHAR, FLOAT)" in teardown
    assert "TASK_LOAD_MARTS_V27_HOURLY" in teardown and "TASK_LOAD_MARTS_V27_DAILY" in teardown


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_readers_are_thin_and_bounded():
    assert "MART_WAREHOUSE_EFFICIENCY_DAILY" in mart27_sql.warehouse_efficiency(7, "ALFA")
    assert "COMPANY = 'ALFA'" in mart27_sql.warehouse_efficiency(7, "ALFA")
    assert "COMPANY = '" not in mart27_sql.warehouse_efficiency(7, "ALL")
    assert "-400," in mart27_sql.query_families(999999)                # clamped
    assert "LIMIT 2000" in mart27_sql.query_families(7, 99999)
    assert "UPPER(DATABASE_NAME) = 'ALFA_EDW_PRD'" in mart27_sql.schema_hourly(7, "ALFA", "ALFA_EDW_PRD")
    assert "DIMENSION = 'USER'" in mart27_sql.cost_allocation(7, "USER")
    with pytest.raises(ValueError):
        mart27_sql.cost_allocation(7, "PLANET")
    assert "COMPANY_FOR_USER" in mart27_sql.ai_usage(7, "ALFA")        # user-grain scoping


def test_all_nine_readers_are_canaried():
    src = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("warehouse_efficiency", "query_families", "role_hourly", "schema_hourly",
                 "cost_allocation", "task_graphs", "security_posture", "incident_timeline",
                 "ai_usage"):
        assert f"mart27_sql.{name}" in src, name


# ---------------------------------------------------------------------------
# App-side telemetry rider
# ---------------------------------------------------------------------------

def test_cache_hit_is_detected_not_guessed():
    src = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    assert '_FETCH_MISS = {"v": False}' in src
    assert '_FETCH_MISS["v"] = True' in src.split("def _execute", 1)[1].split("def ", 1)[0]
    assert "cache_hit = not _FETCH_MISS" in src
    # new-shape insert degrades to the pre-V027 shape, then off entirely
    assert "_ow_qtel_oldshape" in src
    assert "CACHE_HIT, SQL_HASH, BATCH_SIZE, TRUNCATED" in src


def test_usage_rider_keeps_first_paint_p95_honest():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = src.split("def _log_usage", 1)[1].split("\ndef ", 1)[0]
    assert "EVENT_KIND, IS_RERUN" in body
    assert '"rerun", "NULL"' in body                                   # rerun rows carry no RENDER_MS
    assert "0.10" in body                                              # sampled, not spammed
    assert "_ow_usage_oldshape" in body                                # pre-apply degrade
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "def log_ui_event" in comp
    assert 'log_ui_event("saved_view_apply")' in src
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert 'log_ui_event("csv_export"' in sec
