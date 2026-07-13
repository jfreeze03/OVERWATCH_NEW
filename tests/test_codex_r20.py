"""Codex r20 locks — verified ships (2026-07-12). #20 was DECLINED: a
whole-tree scan shows every 'orphaned' canary reader has a live caller
(Codex grepped a subtree; the registry stays full-coverage)."""

from __future__ import annotations

from pathlib import Path

from app.core.query import _quarantine_key
from app.data import insights_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]
_OPT = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")


def test_remediation_reuses_the_advisors_mart_first_pair():
    remed = _OPT.split('key=f"remed_idle_', 1)[0].rsplit("idle_res = ", 1)[1]
    assert "run_mart_first" in remed
    assert "eff_idle_analysis(days, company)" in remed
    # identical builder pair as the advisor -> identical SQL identity -> cache
    assert _OPT.count("mart27_sql.eff_idle_analysis(days, company)") == 2


def test_quarantine_keys_are_namespaced_by_page_and_sql():
    a = _quarantine_key("ControlRoom", "act", "SELECT 1")
    b = _quarantine_key("Compare", "act", "SELECT 1")
    c = _quarantine_key("ControlRoom", "act", "SELECT 2")
    assert len({a, b, c}) == 3          # page and sql both matter
    assert a.startswith("ControlRoom:act:")


def test_supporting_cte_scans_carry_the_warehouse_predicate():
    for sql in (insights_sql.idle_warehouse_analysis(30, "ALFA"),
                insights_sql.warehouse_sizing_profile(30, "ALFA"),
                insights_sql.warehouse_hourly_activity(14, "ALFA")):
        head = sql.split("\nSELECT\n", 1)[0] if "\nSELECT\n" in sql else sql
        # every helper CTE body mentions the company predicate (TRXS list)
        assert "TRXS" in head or "WH!_ALFA!_%" in head   # V044 arm shape
    # ALL keeps the SQL valid via the neutral predicate
    assert "1 = 1" in insights_sql.idle_warehouse_analysis(30, "ALL")


def test_sparks_are_scoped_like_their_neighbors():
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "fact_daily_activity(14, company)" in ov
    assert "fact_daily_activity(14, company, database)" in ops
    assert "fact_daily_activity(14)" not in ov and "fact_daily_activity(14)" not in ops


def test_credential_counts_come_from_one_scan():
    sql = security_sql.governance_counts()
    assert sql.count("FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS") == 1
    assert "COUNT_IF" in sql
