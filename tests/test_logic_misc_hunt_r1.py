"""Locks for the R1 logic-misc bug hunt (app/logic modules outside the cost/ops/DS clusters).

Each block names the finding it locks. The behaviour locks failed on the pre-fix code; the guard
locks beside them (what a fix must NOT change) pass on both."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import sqlglot

from app.data import change_impact_sql
from app.logic import wh_change
from app.logic.forecast import month_end_projection
from tests._source import read

# ---- R1-065 / R1-121: the warehouse change tiles count the whole window, PENDING only ----------


def test_warehouse_change_registry_carries_untruncated_window_totals():
    sql = change_impact_sql.warehouse_change_registry(90, "ALFA")
    assert "COUNT(*) OVER () AS TOTAL_CHANGES" in sql
    assert "COUNT_IF(w.VERDICT = 'REGRESSED') OVER () AS TOTAL_REGRESSED" in sql
    assert "COUNT_IF(w.VERDICT = 'IMPROVED') OVER () AS TOTAL_IMPROVED" in sql
    assert "COUNT_IF(w.VERDICT = 'PENDING') OVER () AS TOTAL_PENDING" in sql
    assert "LIMIT 200" in sql            # the table is still the newest 200
    sqlglot.parse(sql, dialect="snowflake")


def test_registry_kpis_reads_the_window_totals_not_the_capped_frame():
    # 260 changes in the window, the newest 200 returned: 30 regressed among them, 90 overall
    df = pd.DataFrame({"VERDICT": ["REGRESSED"] * 30 + ["IMPROVED"] * 170,
                       "TOTAL_CHANGES": [260] * 200, "TOTAL_REGRESSED": [90] * 200,
                       "TOTAL_IMPROVED": [165] * 200, "TOTAL_PENDING": [5] * 200})
    assert wh_change.registry_kpis(df) == {
        "changes": 260, "regressed": 90, "improved": 165, "pending": 5}


def test_registry_kpis_never_counts_no_baseline_as_pending():
    df = pd.DataFrame({"VERDICT": ["NO_BASELINE", "NEUTRAL"]})
    assert wh_change.registry_kpis(df)["pending"] == 0


def test_wh_change_block_says_when_the_table_is_the_newest_slice():
    block = read("app/ui/pages/operations.py").split("def _wh_change_block", 1)[1].split("\ndef ", 1)[0]
    assert "if k[\"changes\"] > len(df):" in block
    assert "Table below shows the latest {len(df)} of {k['changes']:,} tracked" in block
    assert "f\"{k['changes']:,}\"" in block


# ---- R1-078: month-end gap fill also covers the lagged first days of a month -------------------


def _flat_days(start: date, end: date, usd: float = 100.0) -> pd.DataFrame:
    """$usd on every day from start to end, both inclusive."""
    return pd.DataFrame([{"DAY": start + timedelta(days=i), "USD": usd}
                         for i in range((end - start).days + 1)])


def test_month_end_fills_the_lagged_first_days_of_the_month():
    hist = _flat_days(date(2026, 6, 1), date(2026, 9, 30))       # Oct 1 (and Oct 2) not loaded yet
    for engine in ("linear", "seasonal"):
        # Oct 2, Oct 1 lagging: was 3000 (the lagged day counted nowhere); 31 x $100 = 3100
        assert month_end_projection(hist, date(2026, 10, 2), engine=engine).projected_usd == 3100.0
        # Oct 3, Oct 1 and Oct 2 lagging: was 2900
        assert month_end_projection(hist, date(2026, 10, 3), engine=engine).projected_usd == 3100.0


def test_month_start_density_guard_still_blocks_a_sparse_account():
    # every 3rd day only (5 of the trailing 14 present): the lagged Oct 1 is NOT filled
    sparse = pd.DataFrame([{"DAY": date(2026, 9, 30) - timedelta(days=i), "USD": 100.0}
                           for i in range(0, 120, 3)])
    p = month_end_projection(sparse, date(2026, 10, 2), engine="linear").projected_usd
    assert p == 3000.0      # 0 complete MTD + $100 x 30 (today + 29 remaining); no fabricated Oct 1
