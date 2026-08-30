"""Cost-layer bug-hunt #5 locks (2026-08-30, v4.363.0).

Fifth adversarial pass (6 finders: credit/USD math, chargeback/budget, warehouse/serverless/storage,
savings/ROI ledger, cortex/AI cost, cost-UI consistency). Six confirmed, two refuted (Overview
vs-prior delta is documented + per-window-labeled; action-queue CPR de-overlap preserves its
documented invariant). Three app-side, two in one owner-gated migration (V107), one deferred.
  - [LOW] Operations wasted-spend KPI summed a per-row cent-rounded USD column (round-then-sum).
  - [MED] Spend exact-usage caption disclosed the mart cap (182d) when the 90d live fallback served.
  - [MED] savings_ledger LIMIT 500 truncated the ROI economics (all-time/QTD/realization/run-rate).
  - [MED/V107] COST_DEPT_BUDGET_PACE department join was case-sensitive.
  - [LOW/V107] COST_DEPT_BUDGET_PACE pace window counted today full on one side, partial on the other.
  - [LOW, DEFERRED] cortex 30d projection over-projects short windows (today-partial); see CHANGELOG.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Finding #1 -- wasted-spend total rounds once, not per row (source lock)
# --------------------------------------------------------------------------- #
def test_wasted_spend_sums_unrounded_then_rounds_once() -> None:
    src = _src("app/ui/pages/operations.py")
    # the raw (unrounded) per-row USD is what the KPIs sum
    assert '_wasted_raw = wdf["WASTED_CREDITS"].map(' in src
    assert "_wasted_total = float(_wasted_raw.sum())" in src
    assert 'wdf["WASTED_USD"] = _wasted_raw.round(2)' in src
    assert 'format_usd(_wasted_total)' in src
    assert "monthly = _wasted_total / max(days, 1) * 30.0" in src
    # the old round-then-sum construction is gone
    assert "round(credits_to_usd(safe_float(c), rate, round_cents=False), 2)" not in src


# --------------------------------------------------------------------------- #
# Finding #4 -- exact-usage caption discloses the actual served cap
# --------------------------------------------------------------------------- #
def test_exact_usage_caption_uses_the_served_cap() -> None:
    src = _src("app/ui/pages/cost_parts/spend.py")
    # the live fallback is tracked
    assert "_wh_live = False" in src
    assert "_wh_live = True" in src
    # the caption window resolves at the live cap when the live fallback served, else the mart cap
    assert ("resolve_effective_window(days, max_days=MAX_LIVE_WINDOW_DAYS)\n"
            "                      if _wh_live else resolve_effective_window(days)") in src


# --------------------------------------------------------------------------- #
# Finding #5 -- ROI economics read the whole ledger, not the newest 500 rows
# --------------------------------------------------------------------------- #
def test_savings_ledger_limit_is_optional() -> None:
    full = mart_sql.savings_ledger(limit=None)
    default = mart_sql.savings_ledger()
    explicit = mart_sql.savings_ledger(500)
    assert "LIMIT" not in full
    assert "ORDER BY CREATED_AT DESC" in full           # ordering preserved for the capped reads
    assert "LIMIT 500" in default and "LIMIT 500" in explicit
    # no float literal ever reaches the LIMIT clause
    assert "LIMIT 500.0" not in default


def test_roi_and_scorecard_read_the_full_ledger() -> None:
    src = _src("app/ui/decision_studio.py")
    # both economics surfaces read the uncapped ledger under a distinct cache key
    assert src.count("mart_sql.savings_ledger(limit=None)") == 2
    assert src.count('key="decision_roi_ledger_full"') == 2
    # the old capped read is gone from the economics surfaces
    assert "mart_sql.savings_ledger(), page=_PAGE, key=\"decision_roi_ledger\"" not in src


def test_browsable_detail_table_keeps_its_cap() -> None:
    # the operational VERIFY workflow table is a recent-items browser, not an economics total
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert "mart_sql.savings_ledger()" in src  # default 500


# --------------------------------------------------------------------------- #
# Findings #2 + #3 / V107 -- department join case-fold + completed-days pace window
# --------------------------------------------------------------------------- #
def test_v107_dept_join_and_pace_window() -> None:
    mig = _src("snowflake/migrations/V107__cost_dept_budget_pace_dept_join_and_pace_window.sql")
    # (1) department join case-folded both sides; the case-sensitive form is gone
    assert "UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT)" in mig
    assert "AND m.DEPARTMENT = b.DEPARTMENT" not in mig
    # (2) completed-days pace window: today excluded from MTD + completed-days TIME_SHARE
    assert "AND f.DAY < CURRENT_DATE()" in mig
    assert "(DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE" in mig
    assert "DAY(CURRENT_DATE()) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE" not in mig
    # the V106 warehouse-name case-fold we build on top of survives
    assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in mig
    # re-derives SP_ALERT_SCAN only; no schema change
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in mig
    assert "CREATE OR REPLACE VIEW" not in mig and "CREATE TABLE " not in mig
    assert "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    # ordered-apply guard + version stamp
    assert "EXCEPTION (-20107" in mig and "IF (v < 106) THEN" in mig
    assert "SELECT 107 AS VERSION" in mig and "WHERE VERSION = 107)" in mig
