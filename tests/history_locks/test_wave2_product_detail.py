"""DS Wave-2 #14: a real Entity 360 detail view for a data product (was empty because
DATA_PRODUCT is a catalog attribute, not an entity type).
"""

from pathlib import Path

from app.data import workbench_sql

_UI = (Path(__file__).resolve().parents[2] / "app" / "ui" / "workbench.py").read_text(encoding="utf-8")


def test_product_detail_builder_lists_constituent_entities():
    sql = workbench_sql.product_detail("Sales Analytics")
    assert "ENTITY_CATALOG" in sql
    assert "UPPER(TRIM(DATA_PRODUCT)) = UPPER('Sales Analytics')" in sql
    assert "ENTITY_TYPE, ENTITY_KEY, LABEL, TEAM, OWNER_NAME, CRITICALITY, SLO_NAME" in sql
    # most-severe criticality first, so the riskiest constituents lead
    assert "WHEN 'CRITICAL' THEN 0" in sql


def test_product_detail_quotes_the_key_safely():
    # a hostile product name stays a single quoted literal (inner quote doubled),
    # never breaks out into interpolated SQL
    sql = workbench_sql.product_detail("x') OR 1=1 --")
    assert "'x'') OR 1=1 --'" in sql


def test_entity_360_renders_data_product_detail():
    assert "def _render_data_product_detail(" in _UI
    assert 'if kind == "DATA_PRODUCT":' in _UI
    assert "product_detail(product)" in _UI
