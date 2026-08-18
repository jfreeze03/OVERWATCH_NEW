"""rec#28: when a refresh lacks the AI/OTHER credit split, the MTD KPI prices
every credit at the compute rate — that fallback is now disclosed with a
'flat-rate est.' badge instead of silently overstating AI/Cortex spend.
"""

import pandas as pd

from app.core.query import QueryResult
from app.ui.pages.overview import (
    _billed_split_available,
    _billed_usd_series,
    _mtd_pace_kpi,
)


def test_billed_split_available_detects_the_columns():
    assert _billed_split_available(
        pd.DataFrame({"CREDITS_BILLED_OTHER": [1.0], "CREDITS_BILLED_AI": [1.0]}))
    assert not _billed_split_available(pd.DataFrame({"CREDITS_BILLED": [1.0]}))


def test_billed_usd_series_uses_ai_rate_only_when_split_present():
    split = pd.DataFrame({"CREDITS_BILLED": [100.0],
                          "CREDITS_BILLED_OTHER": [60.0], "CREDITS_BILLED_AI": [40.0]})
    flat = pd.DataFrame({"CREDITS_BILLED": [100.0]})
    assert _billed_usd_series(split, 3.68, 2.20).iloc[0] == 60.0 * 3.68 + 40.0 * 2.20
    # the disclosed fallback: no split -> everything at the compute rate
    assert _billed_usd_series(flat, 3.68, 2.20).iloc[0] == 100.0 * 3.68


def _hist(*, with_split: bool) -> QueryResult:
    days = pd.date_range("2026-06-01", periods=75, freq="D")
    data: dict[str, object] = {"DAY": [d.date() for d in days], "CREDITS_BILLED": [100.0] * 75}
    if with_split:
        data["CREDITS_BILLED_OTHER"] = [60.0] * 75
        data["CREDITS_BILLED_AI"] = [40.0] * 75
    return QueryResult(df=pd.DataFrame(data), ok=True)


def test_mtd_kpi_badges_flat_rate_and_discloses_when_split_missing():
    kpi = _mtd_pace_kpi(0.0, _hist(with_split=False), 3.68, 2.20, 0.0)
    assert kpi["method"] == "flat-rate est."
    assert "compute rate" in kpi["help"] and "may read high" in kpi["help"]


def test_mtd_kpi_reads_billed_when_split_present():
    kpi = _mtd_pace_kpi(0.0, _hist(with_split=True), 3.68, 2.20, 0.0)
    assert kpi["method"] == "billed"
    assert "may read high" not in kpi["help"]
