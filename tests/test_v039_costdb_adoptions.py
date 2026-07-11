"""Locks for V039 + the COST_DB reconciliation adoptions (v4.30.0).

Authority: docs/design/COSTDB_RECONCILIATION.md (R1, R2, R5, R6, R7, R9).
R3 (storage truth) and R4 (client-app cost lens) are deliberately queued
behind Compare Phase 2 — presence here would be scope creep, not progress.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.data import cost_sql, insights_sql, mart27_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_V39 = (_ROOT / "snowflake" / "migrations" / "V039__pseudo_warehouse_filter.sql").read_text(encoding="utf-8")
_V02 = (_ROOT / "snowflake" / "migrations" / "V002__facts.sql").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R1 — the pseudo-warehouse filter
# ---------------------------------------------------------------------------

def test_v039_guard_and_pieces():
    assert "EXCEPTION (-20039" in _V39 and "RAISE not_ready;" in _V39
    assert "RAISE EXCEPTION (" not in _V39
    assert "IF (v < 38) THEN" in _V39 and "SELECT 39 AS VERSION" in _V39
    assert _V39.count("UPPER(WAREHOUSE_NAME) = 'CLOUD_SERVICES_ONLY'") == 2  # fact + eff mart deletes


def test_v039_loader_is_v002_plus_exactly_one_predicate():
    """The house derivation law: a re-derived proc differs from its origin by
    the intended edit and NOTHING else."""
    def _body(text: str) -> str:
        seg = text.split("SP_LOAD_HOURLY_FACTS()", 1)[1]
        return seg.split("$$;", 1)[0]
    v39 = _body(_V39)
    v02 = _body(_V02)
    stripped = v39.replace("\n          AND WAREHOUSE_ID > 0", "")
    assert "WAREHOUSE_ID > 0" in v39 and "WAREHOUSE_ID > 0" not in v02
    assert [ln.strip() for ln in stripped.splitlines() if ln.strip()] == \
           [ln.strip() for ln in v02.splitlines() if ln.strip()]


def test_live_warehouse_builders_skip_the_pseudo_warehouse():
    checks = [
        cost_sql.warehouse_daily_credits(7, "ALFA"),
        cost_sql.warehouse_window_vs_prior(7, "ALFA"),
        cost_sql.cloud_services_ratio_by_warehouse(7, "ALFA"),
        insights_sql.idle_warehouse_analysis(7, "ALFA"),
        insights_sql.warehouse_sizing_profile(7, "ALFA"),
        insights_sql.warehouse_hourly_activity(7, "ALFA"),
        mart27_sql.live_monthly_spend_by_warehouse(12, "ALFA"),
    ]
    for sql in checks:
        assert "WAREHOUSE_ID > 0" in sql, sql[:120]


def test_eff_mart_readers_filter_by_name_until_the_v27_rederivation():
    for sql in (mart27_sql.eff_idle_analysis(30, "ALFA"),
                mart27_sql.eff_sizing_profile(30, "ALFA"),
                mart27_sql.monthly_spend_by_warehouse(12, "ALFA")):
        assert "CLOUD_SERVICES_ONLY" in sql
    backfill = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    assert "WAREHOUSE_ID > 0" in backfill                 # mirror stays in sync


# ---------------------------------------------------------------------------
# R2/R5 — honesty copy + category map
# ---------------------------------------------------------------------------

def test_per_warehouse_dollars_carry_the_cs_caveat():
    cb = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    assert "cloud-services credits, unadjusted" in cb
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "cloud-services credits" in sp and "unadjusted" in sp


def test_categorize_stops_bucketing_new_services_as_other():
    from app.ui.pages.cost_parts.spend import _categorize
    assert _categorize("OPENFLOW_COMPUTE_SNOWFLAKE") == "Serverless"
    assert _categorize("HYBRID_TABLE_REQUESTS") == "Storage"
    assert _categorize("AI_SERVICES") == "AI / Cortex"    # prefix rule still holds


# ---------------------------------------------------------------------------
# R6/R7/R9 — new readers and panels
# ---------------------------------------------------------------------------

def test_cs_by_query_type_reader_and_panel():
    sql = cost_sql.cs_by_query_type(7, "ALFA")
    assert "CREDITS_USED_CLOUD_SERVICES > 0" in sql and "GROUP BY QUERY_TYPE" in sql
    assert "WAREHOUSE_NAME" not in cost_sql.cs_by_query_type(7, "x'y")  # unknown company -> no scope arm (enum-gated)
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "Cloud-services credits by statement type" in sp
    assert "Metadata storms" in sp


def test_clustering_by_table_reader_and_panel():
    sql = insights_sql.clustering_by_table(30, "ALFA")
    assert "AUTOMATIC_CLUSTERING_HISTORY" in sql and "TB_RECLUSTERED" in sql
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    assert "Run clustering-spend scan" in opt             # toggled, never ambient
    assert 'toggle_cost_hint("clustering_")' in opt


def test_year_projection_is_unclamped_and_labeled():
    sql = mart_sql.fact_daily_spend_year()
    assert "DATE_TRUNC('year', CURRENT_DATE())" in sql    # true calendar year
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "_year_projection_strip" in ct
    assert "Straight-line" in ct                          # honesty label
    assert "month-end projections live on" in ct.replace("\n", " ").replace("  ", " ") or "month-end" in ct


def test_wiring_canaries_validate_admin():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("cost.cs_by_query_type", "insights.clustering_by_table",
                 "mart.fact_daily_spend_year"):
        assert name in canary, name
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    m = re.search(r"V001\.\.V(\d+) applied", val)
    assert m and int(m.group(1)) >= 39
