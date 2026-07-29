"""Locks for the metrics-triage HIGH fixes (docs/reviews/METRICS_TRIAGE_2026-07-29.md)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_triage1_month_end_projection_uses_full_month_account_frame():
    """HIGH #1: the month-end projection must be fed the account-wide 150d frame
    (proj_daily, built from the already-loaded _bt_hist), not the 7d company-scoped
    exec-board `daily` frame that truncated MTD for most of the month and mismatched
    the account-wide Projected-month-end / MTD KPIs."""
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert 'proj_daily = _proj[["DAY", "USD"]]' in ov              # built from _bt_hist
    assert '_proj["USD"] = _proj["CREDITS_BILLED"].map' in ov      # account-wide billed frame
    assert "month_end_projection(proj_daily," in ov               # projection uses it
    assert "month_end_projection(daily," not in ov                # never the windowed board frame
    # the ml_forecast branch's MTD also derives from the full-month frame
    assert "proj_daily[pd.to_datetime(proj_daily.iloc[:, 0])" in ov
    assert "else:\n        proj_daily = daily" in ov              # graceful fallback when mart down
