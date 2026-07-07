"""Performance contract: lazy sections, cache scope, mart hot paths."""

import inspect
from pathlib import Path

from app.core import query
from app.data import cost_sql, mart_sql

_PAGES_DIR = Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"


def test_no_eager_tabs_in_pages():
    """st.tabs renders every tab's queries to paint one — banned in pages.

    Use components.lazy_sections instead: only the active section runs.
    """
    offenders = [p.name for p in _PAGES_DIR.glob("*.py")
                 if "st.tabs(" in p.read_text(encoding="utf-8")]
    assert not offenders, f"eager st.tabs in: {offenders} (use lazy_sections)"


def test_cache_scope_excludes_filters():
    """Filters live in the SQL text (already a cache-key argument); putting
    the filter signature in scope cold-started everything on any change."""
    src = inspect.getsource(query._cache_scope)
    assert "filters_signature" not in src
    assert "role" in src and "salt" in src  # what SQL cannot express stays


def test_fact_query_summary_matches_live_aliases():
    """Mart and live summary must stay drop-in interchangeable for the KPI row."""
    from app.data import ops_sql

    fact = mart_sql.fact_query_window_summary(30, "ALFA", "WH_", "KEB", "ALFA_DW")
    live = ops_sql.query_window_summary(30, "ALFA")
    for alias in ("QUERY_COUNT", "FAILED_COUNT", "P95_ELAPSED_SEC", "QUEUED_SEC", "SPILL_REMOTE_GB"):
        assert alias in fact and alias in live, alias
    assert "FACT_QUERY_HOURLY" in fact
    assert "COMPANY = 'ALFA'" in fact
    assert "WAREHOUSE_NAME ILIKE '%WH~_%' ESCAPE '~'" in fact   # _ matched literally
    assert "USER_NAME ILIKE '%KEB%' ESCAPE '~'" in fact
    assert "UPPER(DATABASE_NAME) IN ('ALFA_DW')" in fact
    assert "DATEADD('day', -30" in fact


def test_fact_metering_matches_live_columns():
    fact = mart_sql.fact_metering_by_service(30)
    live = cost_sql.metering_daily_by_service(30)
    for col in ("DAY", "SERVICE_TYPE", "CREDITS_USED", "CREDITS_BILLED", "CREDITS_ADJUSTMENT"):
        assert col in fact and col in live, col
    assert "FACT_METERING_DAILY" in fact


def test_app_statement_stats_scoped_to_app_warehouse():
    sql = mart_sql.app_statement_stats(9999)
    assert "WAREHOUSE_NAME = 'WH_ALFA_OVERWATCH'" in sql
    assert "QUERY_PARAMETERIZED_HASH" in sql
    assert "DATEADD('day', -30" in sql  # clamped to 30
    assert "LIMIT 30" in sql
