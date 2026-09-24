"""Next-Fifty #5 (v4.589.0): one warehouse change must never be booked twice. The app used to INSERT a
manual ESTIMATED ledger row after a guarded resize / auto-suspend change, and the daily change scan's
SP_LEDGER_AUTOBOOK (V038/V145) booked the SAME change again and settled it on measured actuals — the
manual twin stayed ESTIMATED forever (inflating the pipeline) or got verified a second time. Now a
manual row the settled auto row supersedes is excluded from every read + rollup and offered for an
audited REJECT cleanup, and the app stops booking the autobooked levers itself."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import app.logic.actions as actions_mod
from app import config
from app.config import LEDGER_TWIN_MATCH_DAYS
from app.data import mart_sql
from app.logic.actions import ledger_totals, savings_by_lever, savings_by_month, split_superseded

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_twin_rule_shape():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.savings_ledger(limit=None)
    assert "AS SUPERSEDED_BY_CHANGE_ID" in sql
    assert "m.SOURCE_CHANGE_ID IS NULL" in sql and "a.SOURCE_CHANGE_ID = r.CHANGE_ID" in sql
    assert "a.STATE <> 'ESTIMATED'" in sql                       # only a SETTLED auto row supersedes
    assert "IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE'" in sql
    assert f"DATEADD('day', {LEDGER_TWIN_MATCH_DAYS}, m.CREATED_AT)" in sql
    assert "IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')" in sql
    assert "'SCHEDULE'" not in sql                               # the scan cannot see a schedule
    assert "LIMIT" not in sql                                    # the economics read stays uncapped
    sqlglot.parse_one(sql, read="snowflake")


def test_summary_excludes_twins_from_every_aggregate():
    sql = mart_sql.savings_summary_quarter()
    assert sql.count("t.TWIN_ITEM_ID IS NULL") == 5
    for alias in ("VERIFIED_QTD_USD", "VERIFIED_ITEMS", "VERIFIED_ACTIVE_MONTHLY_USD",
                  "VERIFIED_ACTIVE_ITEMS", "ESTIMATED_OPEN_USD"):
        head = sql.split(f"AS {alias}", 1)[0]
        assert "t.TWIN_ITEM_ID IS NULL" in head.rsplit(" AS ", 1)[-1], alias
    assert "AS SUPERSEDED_ITEMS" in sql
    assert "LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID" in sql


def test_ledger_totals_excludes_superseded():
    rows = pd.DataFrame([
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 120, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": "CHG1"},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 100, "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 90, "VERIFIED_USD": 80, "SUPERSEDED_BY_CHANGE_ID": "CHG2"},
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 50, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "REJECTED", "ESTIMATED_USD": 30, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": "CHG3"},
    ])
    t = ledger_totals(rows)
    assert (t["estimated_usd"], t["verified_usd"]) == (50.0, 100.0)
    assert (t["estimated_count"], t["verified_count"]) == (1, 1)
    assert t["superseded_count"] == 2                 # the REJECTED twin is already cleaned up
    assert t["superseded_estimated_usd"] == 210.0
    assert t["realization_pct"] is None               # the only live verified row carried no estimate
    assert ledger_totals(pd.DataFrame())["superseded_count"] == 0


def test_split_superseded_tolerates_missing_column_and_blanks():
    no_col = pd.DataFrame([{"STATE": "VERIFIED"}])
    live, sup = split_superseded(no_col)
    assert len(live) == 1 and sup.empty
    blanks = pd.DataFrame({"STATE": ["A", "B", "C", "D"], "SUPERSEDED_BY_CHANGE_ID": [None, "", "  ", "CHG"]})
    live, sup = split_superseded(blanks)
    assert list(live["STATE"]) == ["A", "B", "C"] and list(sup["STATE"]) == ["D"]
    live, sup = split_superseded(None)
    assert live.empty and sup.empty


def test_month_and_lever_rollups_drop_superseded(monkeypatch):
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 24, 9, 0))
    rows = pd.DataFrame([
        {"STATE": "VERIFIED", "VERIFIED_USD": 100, "ESTIMATED_USD": 100, "VERIFIED_AT": "2026-07-15",
         "FINDING_TYPE": "RESIZE", "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "VERIFIED", "VERIFIED_USD": 900, "ESTIMATED_USD": 900, "VERIFIED_AT": "2026-07-20",
         "FINDING_TYPE": "RESIZE", "SUPERSEDED_BY_CHANGE_ID": "CHG9"},
    ])
    by_month = savings_by_month(rows)
    assert float(by_month["VERIFIED_USD"].sum()) == 100.0
    by_lever = savings_by_lever(rows)
    assert float(by_lever["VERIFIED_USD"].sum()) == 100.0


def test_verification_runs_hide_autobook_and_twin_proposals():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.savings_verification_runs()
    assert "L.SOURCE_CHANGE_ID IS NULL" in sql and "t.TWIN_ITEM_ID IS NULL" in sql
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY V.ITEM_ID ORDER BY V.RUN_AT DESC) = 1" in sql
    sqlglot.parse_one(sql, read="snowflake")


def test_verified_wins_excludes_twins():
    for company in ("ALL", "Trexis"):
        assert "t.TWIN_ITEM_ID IS NULL" in mart_sql.verified_wins(company)
    assert "'Trexis'" in mart_sql.verified_wins("Trexis")        # the company scope is still applied


def test_supersede_sql_is_idempotent_and_audited():
    sqlglot = pytest.importorskip("sqlglot")
    s = mart_sql.supersede_ledger_twins_sql("'JDOE'")
    assert "SET STATE = 'REJECTED'" in s and "AND l.STATE <> 'REJECTED'" in s
    assert "superseded by auto-measured change" in s and "'JDOE'" in s
    assert "VERIFIED_USD" not in s.split("SET", 1)[1].split("FROM", 1)[0]   # audit trail kept
    sqlglot.parse_one(s, read="snowflake")


def test_optimize_stops_double_booking_and_offers_cleanup():
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert "from app.config import LEDGER_AUTOBOOKED_LEVERS" in src
    assert "_autobooked = (_lever in LEDGER_AUTOBOOKED_LEVERS and remediation.autobook_books_change(" in src
    assert src.count('write_gate_open("ledger_twin_reject")') == 1
    assert src.count('stamp_write("ledger_twin_reject", ok)') == 1
    assert "mart_sql.supersede_ledger_twins_sql(identity_sql())" in src
    assert 'get("SUPERSEDED_ITEMS")' in src                      # the UNCAPPED count, not len() of the frame
    assert "split_superseded(res.df)" in src
    assert config.LEDGER_AUTOBOOKED_LEVERS == ("AUTO_SUSPEND", "MAX_CLUSTERS", "RESIZE")


def test_roi_section_discloses_superseded():
    ds = _src("app/ui/decision_studio.py")
    assert "totals['superseded_count']" in ds and "superseded by the " in ds
