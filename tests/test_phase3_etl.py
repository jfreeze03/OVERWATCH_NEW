"""Locks for Phase 3 — ETL tags + unit-cost KPIs (2026-07-14)."""
from pathlib import Path
import pytest
from app.data import etl_sql
sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def test_tag_keys_convention():
    assert etl_sql.TAG_KEYS == ("pipeline", "run_id", "target_object", "environment", "cost_center")


def test_etl_readers_parse_and_measured():
    for sql in (etl_sql.etl_cost_by_pipeline(30, "ALFA"), etl_sql.etl_tag_coverage(30, "ALFA")):
        assert "GET_PATH(TRY_PARSE_JSON" in sql            # parse-safe JSON tag read
        assert "QUERY_ATTRIBUTION_HISTORY" in sql          # measured credits
        assert "CREDITS_USED_QUERY_ACCELERATION" in sql    # QAS included
        sqlglot.parse(sql, dialect="snowflake")
    pipe = etl_sql.etl_cost_by_pipeline(30, "ALFA")
    for col in ("CREDITS_PER_RUN", "CREDITS_PER_M_ROWS", "CREDITS_PER_TIB", "RETRY_WASTE_CREDITS"):
        assert col in pipe


def test_days_clamped():
    assert "-90," in etl_sql.etl_cost_by_pipeline(9999, "ALFA").replace(" ", "")


def test_panel_and_doc_and_metric_present():
    uc = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "etl_sql.etl_cost_by_pipeline" in uc and "Run ETL unit-cost scan" in uc
    assert (_ROOT / "docs" / "design" / "ETL_COST_TAGS.md").exists()
    from app.logic import metric_registry as mr
    assert "etl_unit_cost" in {m.key for m in mr.METRICS}
