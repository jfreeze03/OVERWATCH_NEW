"""Number-formatting locks (owner ask 2026-08-17: "costs are dollars, time is
Hr/min/sec/ms"). A parallel audit found cost values rendered as raw floats or
"N USD" suffixes and a p95-seconds KPI shown as a bare number; these lock the fixes."""

from __future__ import annotations

from pathlib import Path

from app.logic import wh_change

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_change_deltas_exposes_source_column_for_unit_aware_formatting():
    row = {"BASELINE_P95_S": 1800.0, "AFTER_P95_S": 5400.0}
    deltas = {d["col"]: d for d in wh_change.change_deltas(row)}
    assert "P95_S" in deltas                       # source column exposed
    assert deltas["P95_S"]["metric"] == "p95 s"    # pure-logic label unchanged (test_graph pins it)


def test_operations_humanizes_the_p95_seconds_change_kpi():
    src = _src("app/ui/pages/operations.py")
    # the warehouse-change KPI humanizes ONLY the P95_S (seconds) metric.
    assert 'd.get("col") == "P95_S"' in src
    assert "humanize_duration(d['base'], 's')" in src


def test_all_in_billed_and_replication_render_dollars_not_currency_suffix():
    src = _src("app/ui/pages/cost_parts/spend.py")
    # the USD paths use format_usd (leading $), not a "N USD" / raw-float value.
    assert 'f"{safe_float(_ar.get(\'TOTAL_USD\')):,.0f} {_cur}"' not in src
    assert "_cur_fmt(_ar.get(\"TOTAL_USD\"))" in src
    assert 'f"{rep_amt:,.2f}"' not in src           # replication KPI no longer bare
    assert "format_usd(rep_amt) if _is_usd" in src
    # the org data-transfer reconciliation caption (USD path) uses format_usd.
    assert "is {format_usd(org_transfer)} " in src
    assert "{org_transfer:,.2f} USD " not in src


def test_unit_cost_per_run_columns_are_dollar_formatted():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    # both USD_PER_RUN tables (query-pattern + pipeline daily detail) now carry a
    # $-formatted column_config instead of rendering a bare many-decimal float.
    assert src.count('"USD_PER_RUN": st.column_config.NumberColumn') >= 3
