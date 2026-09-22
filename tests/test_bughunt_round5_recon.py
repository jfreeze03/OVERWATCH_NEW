"""Locks for bug-hunt round 5 (wf_878c18dc): cross-page reconciliation + logic fixes —
forecast per-day slope, Brief oldest-critical uncapped, Brief account-time footer, and the
last two served-window siblings (Overview fallback tile, Spend CoCo/Egress labels).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from app.logic.forecast import month_end_projection

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_linear_forecast_slope_is_per_calendar_day_not_per_row():
    # A $10/day series projects the same month-end whether or not the baseline has a gap: the
    # Theil-Sen fit is on calendar-day offsets, so a missing (ingest-lagged/idle) day can't
    # inflate the forward slope (bug-hunt we2ahd4d0).
    today = dt.date(2026, 7, 21)
    base = [dt.date(2026, 7, d) for d in range(7, 21)]                 # 14 dense days
    dense = pd.DataFrame([{"DAY": d, "USD": 100.0 + 10.0 * (d.day)} for d in base])
    gapped = pd.DataFrame([{"DAY": d, "USD": 100.0 + 10.0 * (d.day)}
                           for d in base if d.day not in (11, 12, 13, 14)])  # 4-day hole
    r_dense = month_end_projection(dense, today=today)
    r_gap = month_end_projection(gapped, today=today)
    # per-day slope -> the gap barely moves the projection (row-index slope inflated it ~30%)
    assert abs(r_gap.daily_rate_usd - r_dense.daily_rate_usd) / r_dense.daily_rate_usd < 0.05
    # and the fit is genuinely calendar-based in the source
    assert "(_d - _origin).days" in _read("app/logic/forecast.py")


def test_brief_oldest_critical_uses_uncapped_aggregate():
    src = _read("app/ui/pages/brief.py")
    # age comes from the uncapped OLDEST_CRIT_MIN aggregate, not oldest_open_hours over the feed
    assert 'alert_counts.df.iloc[0].get("OLDEST_CRIT_MIN")' in src
    assert "oldest_open_hours(" not in src


def test_brief_footer_uses_account_time_not_server_clock():
    src = _read("app/ui/pages/brief.py")
    assert "pd.Timestamp.now()" not in src
    assert 'account_now().strftime("Generated' in src


def test_overview_spend_tile_labels_served_window_on_mart_offline_fallback():
    src = _read("app/ui/pages/overview.py")
    assert '(not using_mart) and _ov_bounds is None and int(days) > 90' in src
    assert 'f"Spend, {_ov_spend_lbl} ({company})"' in src


def test_spend_coco_and_egress_use_the_full_window_label_not_metering_served():
    src = _read("app/ui/pages/cost_parts/spend.py")
    assert "_wlab_full = window_label(bounds, int(days))" in src
    assert "f\"— of which CoCo, {_wlab_full}\"" in src
    assert "f\"Total transferred ({_wlab_full})\"" in src
    assert "f\"Estimated egress ({_wlab_full})\"" in src
    # the metering-derived Total-credits tile keeps the metering served label
    assert "f\"Total credits, {_wlab}\"" in src
