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
