"""Regression locks for bug-hunt round 25 — chart-format + deep-link contract-accuracy siblings.

Both classes had hit twice before (chart-format: r23 bar_count, r24 hour_heatmap; deep-link: r24
warehouse jumps + Investigate note); the exhaustive sweeps found the remaining instances.

CREDITS-FMT   the change-impact credits/day line passed unit="credits", which charts.py rendered at 0
              decimals on the axis, tooltip AND peak caption — rounding a genuinely sub-unit
              single-warehouse series (ROUND(...,4)) to "0/1 cr". Credits now render at 3 decimals.
OPT-CONTRACT  Cost ▸ Optimization & Savings honors warehouse_contains in its measured-cost scan, but
              the section contract omitted it from `partial`, so the banner falsely warned "Active but
              ignored: Warehouse". Added it to `partial` (matching database/schema).
FIX-HELP      the alert "Generate fix →" help still said "scope applied" (round 24 fixed only the
              sibling Investigate note), contradicting the destination's "Active but ignored" banner.
              Reworded "scope set".
"""

from __future__ import annotations

from pathlib import Path

from app.ui.charts import _METRIC_AXIS_FMT, _fmt_metric_value

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- CREDITS-FMT: sub-unit credits survive the chart formatter (behavioral) -----------------------
def test_credits_chart_unit_keeps_sub_unit_precision():
    out = _fmt_metric_value(0.28, "credits")
    assert "0.28" in out and out != "0 cr"          # was rounded to "0 cr" at 0 decimals
    assert _fmt_metric_value(0.61, "credits") == "0.610 cr"
    # a whole-credit value still renders (verbose but honest), never a bare int for this metric
    assert "12.000" in _fmt_metric_value(12.0, "credits")
    # the y-axis d3 format matches (3 decimals), not the old ",.0f"
    assert _METRIC_AXIS_FMT["credits"] == ",.3f"


# --- OPT-CONTRACT: the section declares the warehouse filter it actually honors -------------------
def test_optimization_savings_contract_declares_warehouse_contains():
    src = _src("app/ui/pages/cost.py")
    opt = src.split('"Optimization & Savings": {', 1)[1].split('"note"', 1)[0]
    assert '"partial": ("company", "days", "warehouse_contains", "database", "schema_contains")' in opt


# --- FIX-HELP: the Generate-fix help does not over-claim ------------------------------------------
def test_generate_fix_help_says_scope_set_not_applied():
    src = _src("app/ui/pages/alerts.py")
    assert "scope set — generate, confirm, execute, audited." in src
    assert "scope applied — generate, confirm, execute, audited." not in src
