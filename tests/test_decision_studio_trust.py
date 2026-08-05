"""Decision Studio TRUST pass.

The Impact $ and the 0-1 Confidence look identical across decision surfaces but are
NOT the same thing: on the Portfolio, Impact is measured and Confidence is an
evidence heuristic; on the Action Center, Impact is an authored estimate and
Confidence is an authored belief. These lock that each surface says which is which,
so a reader never mistakes an ordering heuristic for a measurement or a guarantee.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_decision_rows_threads_per_surface_column_help():
    body = _src("app/ui/components.py").split("def decision_rows", 1)[1].split("\ndef ", 1)[0]
    assert "impact_help" in body and "confidence_help" in body
    # accepted AND actually wired into the column configs
    assert "help=impact_help or None" in body
    assert "help=confidence_help or None" in body


def test_portfolio_names_measured_vs_heuristic():
    port = _src("app/ui/decision_studio.py").split("def _portfolio", 1)[1].split("\ndef ", 1)[0]
    assert "NOT statistical confidence" in port          # confidence is not what it looks like
    assert "not promised savings" in port                 # impact is observed cost
    assert "evidence-weighted heuristics" in port         # the honest framing
    assert "ACT NOW" in port and "confidence < 0.5" in port  # the exact lane rule is stated


def test_action_center_names_estimate_and_authored_confidence():
    wb = _src("app/ui/workbench.py")
    assert "Authored ESTIMATE (modeled, not billed)" in wb
    assert "stated belief, not a measurement" in wb
