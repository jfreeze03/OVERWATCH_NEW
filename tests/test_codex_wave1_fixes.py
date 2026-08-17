"""Codex-review wave-1 fixes (ground-truthed 2026-08-17): #17 storage label,
#22 experiment-verify proof gate, #37 filter-matrix auto-discovery. (#1 FAILS
presence is locked in test_v4142_decision_studio.)"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_storage_labels_name_time_travel():
    # Codex #17: AVERAGE_DATABASE_BYTES INCLUDES Time Travel, so "active + fail-safe"
    # only was inaccurate — the label must credit Time Travel.
    reg = _src("app/logic/metric_registry.py")
    assert "Active + Time Travel + fail-safe" in reg
    assert "Active + fail-safe bytes, binary TiB" not in reg  # old wording gone
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert "active + Time Travel + fail-safe" in spend
    assert "ACTIVE + TIME-TRAVEL\n" in spend or "ACTIVE + TIME-TRAVEL " in spend


def test_experiment_verify_is_gated_on_proof():
    # Codex #22: a VERIFIED experiment books SAVINGS_LEDGER + feeds the "Verified
    # savings" headline, so the Save button is blocked without proof.
    ds = _src("app/ui/decision_studio.py")
    assert "_proof_gaps" in ds
    assert "disabled=bool(_proof_gaps)" in ds
    # the three required proofs.
    assert "result evidence" in ds
    assert "verified $ amount above 0" in ds
    assert "observation window to close" in ds


def test_filter_matrix_auto_discovers_sql_modules():
    # Codex #37: the matrix now globs every app/data/*_sql.py instead of a hand list
    # that silently omitted workbench_sql / dq_sql / app_cost_sql / etc.
    src = _src("tests/test_p4_filter_matrix.py")
    assert 'glob("*_sql.py")' in src
    # the old hardcoded tuple (which named these) is gone.
    assert '"change_impact_sql", "chargeback_sql"' not in src
