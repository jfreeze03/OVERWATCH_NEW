"""Locks for bug-hunt round 10 (wf_da49e8c1-d73): the bounds-into-prior-calendar-month sweep.

Three sibling call sites of the r9 class — Spend Attribution vs-prior table, Security egress
baseline, and Decision Studio product retirement trend — must gate the calendar vs-prior comparison
to LAST_MONTH (is_prior_month_window), so the period-to-date presets don't do a partial-vs-full
comparison.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_spend_attribution_suppresses_vs_prior_for_period_to_date():
    src = _read("app/ui/pages/cost_parts/spend.py")
    assert "is_prior_month_window" in src
    # the vs-prior columns/total render only for a trailing window or Last month
    assert "_show_vs_prior = bounds is None or is_prior_month_window(bounds)" in src
    assert "if _show_vs_prior:" in src
    # the bounded current pool is still read with bounds (allocation reconciles) — unchanged
    assert "fact_warehouse_window_vs_prior(days, company, bounds=bounds)" in src
    # the false "equal-length windows" note is now conditional, and the half-window disclosure
    # only fires for trailing windows (under bounds the builder scans the full range)
    assert '_eq_note = ("Equal-length windows excluding the current partial day' in src
    assert "if _eff_wh < int(days) and bounds is None:" in src


def test_egress_baseline_gates_calendar_prior_to_last_month():
    src = _read("app/ui/pages/security.py")
    assert "from app.logic.date_windows import is_prior_month_window" in src
    assert "egress_baseline(days, bounds=bounds if is_prior_month_window(bounds) else None)" in src


def test_product_consumer_reads_trend_gates_calendar_prior_to_last_month():
    src = _read("app/ui/decision_studio.py")
    assert "from app.logic.date_windows import is_prior_month_window" in src
    assert "bounds=bounds if is_prior_month_window(bounds) else None" in src
