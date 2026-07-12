"""Codex r18 batch 1 locks — verified correctness fixes (2026-07-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import companies
from app.core.query import _with_row_cap
from app.data import chargeback_sql, insights_sql, mart27_sql

# skip cleanly on the CI floor-compat job, which installs no sqlglot
sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_UC = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
_ADM = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")


def test_sizing_profile_has_one_outer_where_and_parses():
    sql = insights_sql.warehouse_sizing_profile(30, "ALFA")
    assert "WHERE M.WAREHOUSE_ID > 0\nWHERE" not in sql   # the r18 #2 bug
    assert "M.WAREHOUSE_ID > 0" in sql                    # V039 filter kept
    sqlglot.parse(sql, dialect="snowflake")


def test_row_cap_is_authoritative_over_oversized_limits():
    # r18 #1: a canary max_rows=1 must beat a 20,000-row trailing LIMIT
    assert _with_row_cap("SELECT X FROM T LIMIT 20000", 1).rstrip().endswith("LIMIT 2")
    # small trailing limits still win (they already answer within budget)
    assert _with_row_cap("SELECT X FROM T LIMIT 20", 100).rstrip().endswith("LIMIT 20")


def test_role_shares_follow_the_attribution_law():
    # visibility outside the denominator, in BOTH builders (live + mart)
    for sql in (chargeback_sql.role_share_within_warehouse(30, "ALFA"),
                mart27_sql.role_share(30, "ALFA")):
        vis = companies.role_clause("ALFA", "ROLE_NAME")
        head, tail = sql.split("FROM shared", 1)
        assert vis not in head          # scoped CTE = whole warehouse
        assert vis in tail              # display rows filtered after
        assert "PARTITION BY WAREHOUSE_NAME" in head  # per-warehouse semantics kept


def test_unit_costs_reads_ai_fact_before_paying_the_live_scan():
    assert _UC.find("ai_costs_by_model") < _UC.find("run_batch(")
    assert 'if not _ai_m.usable():' in _UC              # live member is conditional
    assert _UC.count("cortex_model_costs(days)") == 2   # batch member + serial fallback only


def test_admin_tuning_drill_uses_positional_index_and_never_passes_silently():
    assert '_tt.iloc[int(_sel)]["PAGE"]' in _ADM
    assert '_sel["PAGE"]' not in _ADM                   # the flash-and-nothing bug
    drill = _ADM.split('key="adm_tt_sel"', 1)[1][:2000]
    assert "except (KeyError, TypeError, ValueError) as exc:" in drill
    assert "Tuning-target drill unavailable" in drill   # failure is visible


def test_expensive_queries_filter_is_display_only():
    """Audit find (same law): the db/schema filter must not shrink the
    warehouse-hour denominator it shares credits over."""
    sql = insights_sql.expensive_queries_usd(7, "ALFA", 50, database="ALFA_EDW_PRD")
    q_cte = sql.split("WITH q AS", 1)[1].split("),", 1)[0]
    assert "ALFA_EDW_PRD" not in q_cte                  # denominator sees every query
    assert "ALFA_EDW_PRD" in sql.split("JOIN m", 1)[1]  # display filter after the joins
    sqlglot.parse(sql, dialect="snowflake")
    # page passes the sidebar filters into the scan
    _opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    _blk = _opt.split("expensive_queries_usd(", 1)[1][:220]
    assert "flt_database" in _blk and "flt_schema_contains" in _blk
