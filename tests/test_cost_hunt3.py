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

from app.data import insights_sql, mart27_sql

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


# ---- MED x2 (deferred #A/#B): repeat-query panel resolved via the LIVE route --------
def test_repeat_query_panel_is_live_only_not_mart():
    # The family mart (family_repeat_fingerprints) diverged from the live twin two ways it cannot
    # fix -- it averaged result-cache runs in at 0% cache (biasing well-cached families onto the
    # <=25% materialization gate) and carried a broader population than the live SUCCESS/SELECT
    # filter -- and only the live path prices the Avoidable $ column. The toggle-gated panel now
    # calls the live builder directly instead of mart-first (cost-hunt3 -> live route, 2026-08-30).
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    panel = opt.split("Repeat-query candidates", 1)[1].split("elif opt_section", 1)[0]
    assert "run_mart_first(" not in panel
    assert "insights_sql.repeat_query_fingerprints(" in panel
    assert "mart27_sql.family_repeat_fingerprints" not in panel


def test_live_repeat_query_builder_is_the_correct_basis():
    # the live twin (now the sole source for the panel) filters SUCCESS/SELECT/non-OVERWATCH and
    # bytes-weights the cache metric (excluding zero-scan result-cache runs) -- the reference basis.
    sql = insights_sql.repeat_query_fingerprints(30, "ALFA", 10)
    assert "EXECUTION_STATUS = 'SUCCESS'" in sql
    assert "QUERY_TYPE = 'SELECT'" in sql
    assert "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'" in sql
    assert "IFF(COALESCE(BYTES_SCANNED, 0) > 0" in sql   # bytes-weighted, zero-scan excluded
    assert "AS EST_CREDITS" in sql                        # the priced column the family mart lacks
