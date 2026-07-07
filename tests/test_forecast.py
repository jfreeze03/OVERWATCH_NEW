from datetime import date, timedelta

import pandas as pd

from app.logic.forecast import contract_pace, month_end_projection


def _daily(days: int, usd: float, end: date) -> pd.DataFrame:
    rows = [{"DAY": end - timedelta(days=i), "USD": usd} for i in range(days)]
    return pd.DataFrame(rows)


def test_projection_flat_rate():
    today = date(2026, 7, 10)
    fc = month_end_projection(_daily(20, 100.0, today), today)
    assert fc.ok
    assert fc.mtd_usd == 1000.0            # 10 days in July so far
    assert fc.projected_usd == 3100.0      # 1000 + 100 * 21 remaining
    assert fc.low_usd <= fc.projected_usd <= fc.high_usd
    assert fc.days_remaining == 21


def test_projection_declines_with_sparse_history():
    today = date(2026, 7, 10)
    fc = month_end_projection(_daily(2, 100.0, today), today)
    assert not fc.ok
    assert "history" in fc.basis or "Needs" in fc.basis


def test_projection_empty_frame():
    assert not month_end_projection(pd.DataFrame(), date(2026, 7, 10)).ok


def test_contract_pace_over_and_under():
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    mid = date(2026, 7, 2)  # ~half the term
    hot = contract_pace(consumed_credits=6000, contract_credits=10000,
                        contract_start=start, contract_end=end, today=mid)
    assert hot["ok"] and hot["pace_ratio"] > 1.0
    assert hot["projected_overage_credits"] > 0
    cool = contract_pace(3000, 10000, start, end, mid)
    assert cool["ok"] and cool["pace_ratio"] < 1.0
    assert cool["projected_overage_credits"] == 0


def test_contract_pace_unconfigured():
    assert not contract_pace(0, 0, date(2026, 1, 1), date(2026, 12, 31), date(2026, 6, 1))["ok"]
