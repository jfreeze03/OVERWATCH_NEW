"""Locks for bug-hunt round 8 (wf_b1adcea0-f8c): cross-page reconciliation, render consistency,
silent-failure guards.

Five defects — SLA-finish forecast window divergence (Brief vs Operations), task-health mart-path
undercount (round-7 fix was incomplete), Trust Center false all-clear, Admin timeout-ceiling raw
seconds, and Compare remote-spill byte humanization — pinned so a regression re-fails here.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_sla_finish_forecast_is_window_independent_like_brief():
    # Brief and Operations reuse the same builder + fixed 14-night baseline; the Operations panel
    # must NOT thread the scope-bar `days` (it did -> contradicting verdicts + violated its own
    # "Window ignored" tab contract). The panel + its prefetch call the scan all-time.
    src = _read("app/ui/pages/operations.py")
    assert "def _sla_finish_forecast_panel(*, pf: dict | None = None)" in src
    assert "_sla_finish_forecast_panel(pf=_pf)" in src
    assert 'key="etl_cycle_finish"' in src  # cache key no longer varies by window
    # neither the panel nor the prefetch passes days into the cycle-finish scan
    assert "cycle_finish_history_scan(fqn, start_workflow=start_wf, end_workflow=end_wf)" in src
    assert "cycle_finish_history_scan(ctrl, start_workflow=start_wf, end_workflow=end_wf)" in src
    assert "cycle_finish_history_scan(fqn, start_workflow=start_wf, end_workflow=end_wf, days=days)" not in src


def test_fact_task_daily_carries_uncapped_window_totals():
    # the mart path survives run()'s 5000-row transport cap: read window totals, not a frame sum.
    sql = mart_sql.fact_task_daily(365, "ALFA")
    assert "SUM(RUNS) OVER () AS TOTAL_RUNS_WIN" in sql
    assert "SUM(FAILED) OVER () AS TOTAL_FAILED_WIN" in sql
    # the KPI branch (shared with the r7 live path) reads the total when present
    assert 'df["TOTAL_RUNS_WIN"].iloc[0]' in _read("app/ui/pages/operations.py")


def test_trust_center_empty_delta_is_not_a_green_all_clear():
    src = _read("app/ui/pages/security.py")
    # the covered mart-delta path no longer paints "clean" on an empty delta — it mirrors the
    # r31 fallback's honest needs_setup wording ("nothing was scanned, not that nothing is at risk").
    assert 'empty_state("clean", "No Trust Center findings in the latest materialized snapshot.")' not in src
    # the covered path now carries its own needs_setup message (distinct wording from the fallback)
    assert "No Trust Center scanner results in the latest snapshot" in src
    assert "nothing is at risk." in src  # the honest wording, shared by the covered path + r31 fallback


def test_admin_statement_timeout_ceiling_is_humanized():
    src = _read("app/ui/pages/admin.py")
    assert 'humanize_duration(safe_float(_val), "s") if _val else "—"' in src
    assert '"value": f"{_val}s" if _val else "—"' not in src


def test_compare_remote_spill_uses_humanize_gb():
    src = _read("app/ui/pages/cost_parts/compare.py")
    assert "humanize_gb," in src  # imported
    assert "return humanize_gb(v)" in src
    assert 'return f"{v:,.2f} GB"' not in src
