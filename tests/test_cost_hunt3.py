"""Cost-layer bug-hunt #3 locks (2026-08-30, v4.359.0). App-side fixes.

Third adversarial pass over the cost layer (6 finders). Five distinct confirmed findings; two
(family_repeat_fingerprints mart-vs-live cache-basis + candidate-population) are coordinated
loader+reader migration-bearing changes deferred to a scoped follow-up. One refuted (linear vs
seasonal month-end projection -- both engines drop one gap-day symmetrically). Fixed here:
  - [MED] resize-saving booking scaled the WHOLE monthly bill by 2^steps (~12x too high for a busy,
    low-idle warehouse); now scales IDLE only, matching the tab's conservative model.
  - [MED] compare-tab "Warehouse spend" LEVEL KPI summed the delta-ranked top-100 movers frame,
    dropping the largest STEADY warehouse; now reads account-wide totals off the cov CROSS JOIN.
  - [LOW] "Attributed (warehouse)" help claimed it included reader metering; reworded to match the
    computation (reader is in the unattributed gap).
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- MED: resize-saving booking scales IDLE only, not the whole bill ---------------
def test_resize_booking_scales_idle_not_whole_bill():
    body = _src("app/ui/pages/cost_parts/optimize.py")
    assert "_idle = safe_float(srow.get(\"IDLE_MONTHLY_USD\"))" in body
    assert "est_sz = round(max(0.0, _idle * (1.0 - 2.0 ** _steps)), 2)" in body
    # the whole-bill rate-scaling that overstated the booked saving is gone
    assert "_monthly * (1.0 - 2.0 ** _steps)" not in body


# ---- MED: compare warehouse-spend KPI reads unbounded totals, not the top-100 frame -
def test_compare_warehouse_credits_carries_unbounded_totals():
    sql = mart27_sql.compare_warehouse_credits("2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30")
    assert "AS TOTAL_A_CREDITS" in sql and "AS TOTAL_B_CREDITS" in sql
    assert "cov.TOTAL_A_CREDITS" in sql and "cov.TOTAL_B_CREDITS" in sql
    assert "ORDER BY ABS(m.A_CREDITS - m.B_CREDITS) DESC\nLIMIT 100" in sql  # movers list unchanged
    sqlglot.parse(sql, dialect="snowflake")
    comp = _src("app/ui/pages/cost_parts/compare.py")
    assert 'safe_float(wh.df["TOTAL_A_CREDITS"].iloc[0]) * rate' in comp
    assert 'safe_float(wh.df["TOTAL_B_CREDITS"].iloc[0]) * rate' in comp


# ---- LOW: "Attributed (warehouse)" help matches the computation ---------------------
def test_attributed_warehouse_help_excludes_reader():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    # the stale claim that the figure includes reader metering is gone
    assert "Exact warehouse + reader metering, company-scopable." not in spend
    assert "Own-account warehouse metering only" in spend
    assert "unattributed gap" in spend
