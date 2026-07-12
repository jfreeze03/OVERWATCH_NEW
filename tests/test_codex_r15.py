"""Codex r15 hotfix locks (v4.32.1) — and the class-killer for r15 #1.

The r14 chargeback swap changed a builder's FROM to a fact but left the
live view's time column in its WHERE; the lock checked the table name and
missed the column. This sweep checks every registered builder: a pure
fact/mart read must never reference the live views' time columns."""

from __future__ import annotations

import re

from app.data import chargeback_sql
from app.data.canary import CANARIES

# Day-grain facts/marts (time column: DAY) and the hourly fact (HOUR_TS).
_DAY_GRAIN = ("FACT_WAREHOUSE_DAILY", "FACT_METERING_DAILY", "FACT_TASK_DAILY",
              "FACT_STORAGE_DAILY", "FACT_LOGIN_DAILY", "FACT_AI_USAGE_DAILY",
              "MART_PATTERN_COST_DAILY", "MART_LOCK_WAIT_DAILY",
              "MART_COST_ALLOCATION_DAILY", "MART_QUERY_FAMILY_DAILY",
              "MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_TAG_COVERAGE_DAILY",
              "MART_SECURITY_POSTURE_DAILY")


def test_the_regression_itself_is_dead():
    sql = chargeback_sql.department_window_credits(7, "ALFA")
    assert "FACT_WAREHOUSE_DAILY" in sql
    assert "START_TIME" not in sql                        # r15 #1: the phantom column
    assert re.search(r"M\.DAY >= DATEADD\('day',\s*-7", sql)


def test_pure_fact_reads_never_reference_live_time_columns():
    """Sweep the whole canary registry: any builder that reads ONLY our
    facts/marts (no ACCOUNT_USAGE / INFORMATION_SCHEMA in the text) must
    filter on the fact's own time column, not the live views'."""
    offenders = []
    for name, builder in CANARIES:
        try:
            sql = builder()
        except Exception:  # noqa: BLE001 — parameter-validating builders
            continue
        if not sql or "ACCOUNT_USAGE" in sql or "INFORMATION_SCHEMA" in sql:
            continue
        if any(t in sql for t in _DAY_GRAIN) and "START_TIME" in sql:
            offenders.append(name)
    assert not offenders, f"fact readers using live time columns: {offenders}"


def test_brief_shares_the_shells_health_strip_cache():
    """r15 #14 (concrete case): the shell runs health_strip every render;
    Brief's batch tuple-cache paid the same SQL again. Brief now calls run()
    with the SAME key, sharing one cache entry."""
    from pathlib import Path
    _ROOT = Path(__file__).resolve().parents[1]
    br = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert '"key": "strip"' not in br                     # out of the batch
    assert 'key="health_strip"' in br                     # shell-shared entry
    mn = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'key="health_strip"' in mn                     # same key both sides
