"""Drift-guard for the metric registry (Phase 1 — architectural, 2026-07-14)."""
from app.logic import metric_registry as mr


def test_methods_valid_and_fields_present():
    keys = [m.key for m in mr.METRICS]
    assert len(keys) == len(set(keys))                      # unique keys
    for m in mr.METRICS:
        assert m.method in mr.METHODS, m.key
        for f in (m.key, m.label, m.grain, m.source, m.timezone, m.latency, m.formula_version):
            assert isinstance(f, str) and f.strip(), m.key


def test_core_metrics_registered():
    keys = {m.key for m in mr.METRICS}
    for k in ("account_billed_spend", "org_reconciliation", "warehouse_usage",
              "measured_query_cost", "pattern_cost", "allocated_user_db",
              "per_db_storage", "account_tier_storage"):
        assert k in keys, k


def test_every_method_is_used_and_rows_render():
    grouped = mr.by_method()
    assert all(grouped.get(meth) for meth in mr.METHODS)    # each method has >=1 metric
    rows = mr.as_rows()
    assert len(rows) == len(mr.METRICS) and set(rows[0]) >= {"Metric", "Method", "Source"}


def test_admin_surfaces_the_registry():
    from pathlib import Path
    adm = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "metric_registry" in adm and '"Metrics"' in adm


# ---------------------------------------------------------------------------
# rec 16 — the registry is now an EXECUTABLE contract (window / partial-day /
# unit / filters / required_sources / coverage / owner), drift-guarded in CI.
# ---------------------------------------------------------------------------
def test_rec16_registry_is_a_complete_executable_contract():
    # every metric declares a full, internally-consistent contract (empty = clean)
    assert mr.validate() == []
    # the executable fields are enum-valid on every metric
    for m in mr.METRICS:
        assert m.window in mr.WINDOWS and m.unit in mr.UNITS and m.partial_day in mr.PARTIAL_DAY
        assert m.required_sources and m.coverage and m.owner


def test_rec16_get_accessor():
    assert mr.get("warehouse_usage").method == mr.METERED
    assert mr.get("nope") is None


def test_rec16_contract_runway_registry_matches_the_mart_sql():
    # THE drift-guard this session's rec20 patching motivates: the runway metric's
    # DECLARED contract (trailing-30-complete-days, today excluded) must match the
    # actual mart SQL. Revert contract_exhaustion() to the /30 partial-day form and
    # THIS breaks CI, not just the alert.
    from app.data import mart_sql
    m = mr.get("contract_runway")
    assert m is not None
    assert m.window == "trailing-complete-days" and m.partial_day == "excluded"
    assert m.unit == "days" and "FACT_METERING_DAILY" in m.required_sources
    sql = mart_sql.contract_exhaustion()
    # the SQL implements exactly the declared window
    assert "DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())" in sql  # trailing 30...
    assert "AND DATEADD('day', -1, CURRENT_DATE())" in sql            # ...COMPLETE days (today out)
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in sql                    # divide by real days, not /30


def test_rec16_billed_spend_source_matches_registry():
    # the account billed-spend metric declares FACT_METERING_DAILY (the mart the app
    # reads) as required; the daily-spend query must actually read it. Repoint the
    # query to a different table and this drift-guard breaks CI.
    from app.data import mart_sql
    m = mr.get("account_billed_spend")
    assert "FACT_METERING_DAILY" in m.required_sources
    assert "FACT_METERING_DAILY" in mart_sql.fact_daily_spend(30)
