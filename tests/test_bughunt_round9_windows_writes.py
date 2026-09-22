"""Locks for bug-hunt round 9 (wf_287a1ca2-ff3): write-safety, allocation-pool windows, and the
period-to-date-vs-prior-calendar-month comparison class.

Six defects — emergency-confirm key scoping, chargeback + spend pool bounds guards, Overview +
Control Room vs-prior comparison gated to Last month, and the ops query-drill consume-on-arrival —
pinned so a regression re-fails here.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from app.config import (
    CURRENT_MONTH_WINDOW,
    CURRENT_YEAR_WINDOW,
    LAST_MONTH_WINDOW,
)
from app.logic.date_windows import is_prior_month_window, window_bounds

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_is_prior_month_window_only_true_for_last_month():
    today = dt.date(2026, 9, 22)
    assert is_prior_month_window(window_bounds(LAST_MONTH_WINDOW, today), today) is True
    # period-to-date presets are NOT a valid vs-prior-calendar-month comparison
    assert is_prior_month_window(window_bounds(CURRENT_MONTH_WINDOW, today), today) is False
    assert is_prior_month_window(window_bounds(CURRENT_YEAR_WINDOW, today), today) is False
    assert is_prior_month_window(None, today) is False


def test_emergency_confirm_key_is_lever_scoped():
    src = _read("app/ui/pages/operations.py")
    # the confirm widget key must be the per-lever _emg_key, never the fixed "emg"
    assert 'confirm_gate("EMERGENCY", "Execute + audit", key=_emg_key' in src
    assert 'confirm_gate("EMERGENCY", "Execute + audit", key="emg"' not in src


def test_chargeback_pool_rematch_is_bounds_guarded():
    src = _read("app/ui/pages/cost_parts/ai_chargeback.py")
    assert 'if bounds is None and "QUERY_HISTORY" in str(share_res.source) and days > MAX_LIVE_WINDOW_DAYS:' in src


def test_spend_alloc_pool_keeps_bounded_pool():
    src = _read("app/ui/pages/cost_parts/spend.py")
    # _alloc_pool returns the bounded window_usd early under a calendar preset (before the
    # trailing-only _live_eff/_pool_eff rematch, which is bounds-blind)
    assert "def _alloc_pool(res_source: str) -> float:" in src
    assert "if bounds is not None:\n                return window_usd" in src


def test_overview_and_control_room_gate_vs_prior_to_last_month():
    ov = _read("app/ui/pages/overview.py")
    assert "is_prior_month_window" in ov
    assert "_vp_bounds = _ov_bounds if is_prior_month_window(_ov_bounds) else None" in ov
    assert "bounds=_vp_bounds" in ov
    cr = _read("app/ui/pages/control_room.py")
    assert "from app.logic.date_windows import is_prior_month_window" in cr
    assert "_bounds = _bounds if is_prior_month_window(_bounds) else None" in cr


def test_ops_query_drill_consumes_nav_context():
    src = _read("app/ui/pages/operations.py")
    # the never-cleared signature sentinel is gone; query_id is consumed from the nav context
    assert 'st.session_state["_ow_ops_context_applied"] = nav_signature' not in src
    assert 'k: v for k, v in _nav.items() if k != "query_id"' in src
