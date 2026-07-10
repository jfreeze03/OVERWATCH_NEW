"""Locks for the by-name procedure trend (v4.18.0, owner ask: 'can I enter
it myself'). Same extraction and rollup as the leaderboard so they always
agree; honors the page filters per the triage-filter law."""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_trend_builder_matches_the_leaderboard_semantics():
    sql = insights_sql.proc_cost_trend("SP_LOAD_MARTS_V27", 30, "ALFA")
    assert chr(92) not in sql                                     # POSIX classes only
    assert "REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1)" in sql
    assert "COALESCE(ROOT_QUERY_ID, QUERY_ID)" in sql             # children roll to the CALL
    assert "GROUP BY n.DAY" in sql                                # the trend grain
    assert "ATTRIBUTED_CALLS" in sql                              # $0 rows stay diagnosable
    # bare names match any qualification; exact qualified names match too
    assert "= 'SP_LOAD_MARTS_V27'" in sql and "LIKE '%.SP_LOAD_MARTS_V27'" in sql


def test_trend_honors_the_triage_filters_and_is_safe():
    sql = insights_sql.proc_cost_trend("X", 30, "ALFA", "ALFA_EDW_PRD", "rpt")
    assert "WH_TRXS" in sql or "WAREHOUSE_NAME" in sql            # company scoping present
    assert "ALFA_EDW_PRD" in sql                                  # database filter applied
    assert "c.SCHEMA_NAME" in sql
    assert "''" in insights_sql.proc_cost_trend("x'y", 30)        # injection-safe
    assert "-90," in insights_sql.proc_cost_trend("X", 9999)      # window clamped


def test_panel_wired_with_page_filters_and_canaried():
    uc = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "Trend one procedure" in uc
    assert "proc_cost_trend(" in uc
    assert "_pname.strip(), days, company, database, schema_contains" in uc  # triage-filter law
    assert "charts.spend_trend(tdf" in uc                         # the house trend chart
    assert "same caveats as the leaderboard" in uc
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "insights_sql.proc_cost_trend" in canary
