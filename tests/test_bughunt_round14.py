"""Regression locks for the round-14 consistency hunt (v4.436.0).

Two MED cross-surface consistency fixes (a third finding — the exec-board UTC vs
account-clock window — is migration-gated and tracked separately):
  DAG-1  task-graph DAG node durations humanize ("1h 30m"), matching the page's cards + table
  ROI-1  Brief "Verified savings (QTD)" does not paint a false green when app cost is a degenerate 0
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- DAG-1: DAG node durations humanize, not raw seconds --------------------------------
def test_dag_node_durations_humanize():
    c = _src("app/ui/charts.py")
    assert "from app.logic.formulas import humanize_duration" in c
    # the critical-path suffix and the selected-run tooltip both humanize RUN_SEC
    assert 'f" · {humanize_duration(duration, \'s\')}"' in c
    assert "f\"selected run: {humanize_duration(row.get('RUN_SEC'), 's')}\"" in c
    # the raw fixed-decimal-seconds formatting is gone from those node labels
    assert "{float(duration):,.1f}s" not in c
    assert "{float(row.get('RUN_SEC')):,.1f}s" not in c


# --- ROI-1: Brief ROI treats a zero app-cost denominator as unmeasured, not $0 ----------
def test_brief_roi_zero_denominator_is_unmeasured():
    b = _src("app/ui/pages/brief.py")
    blk = b.split('"Verified savings (QTD)"', 1)[0][-700:]
    # app_usd is None (unmeasured) when the app-cost denominator is <= 0, so no false green
    assert "_app_credits * rate if _app_credits > 0 else None" in blk
    # the old unconditional "* rate if cost_q.usable() else None" (which yielded 0.0) is gone
    assert 'get("APP_CREDITS_30D")) * rate\n                   if cost_q.usable() else None' not in b
