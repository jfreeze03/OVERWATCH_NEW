"""Locks for the V024 feature-depth batch: threshold suggestions, live
re-checks, forecast backtesting, retro score history, recurring patterns."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.data import insights_sql, mart_sql, recheck_sql
from app.logic.forecast import backtest_forecasts
from app.logic.scoring import score_history
from app.logic.tuning import suggest_threshold, suggestions_by_rule

# ---------------------------------------------------------------------------
# Threshold suggestions
# ---------------------------------------------------------------------------


def _events(noise_vals, actioned_vals):
    rows = [{"RULE_ID": "R", "METRIC_VALUE": v, "RESOLUTION_KIND": "NOISE"} for v in noise_vals]
    rows += [{"RULE_ID": "R", "METRIC_VALUE": v, "RESOLUTION_KIND": "ACTIONED"} for v in actioned_vals]
    return pd.DataFrame(rows)


def test_suggests_between_separable_clusters():
    out = suggest_threshold(_events([10, 11, 12, 11, 10, 12], [40, 45, 50]), current_threshold=8)
    assert out["ok"] and 12 < out["suggested"] < 40
    assert "keeps" in out["basis"]


def test_pure_noise_raises_threshold():
    out = suggest_threshold(_events([10, 12, 11, 13, 12, 14], []), current_threshold=8)
    assert out["ok"] and out["suggested"] > 13
    assert "disabling" in out["basis"]


def test_thin_evidence_declines():
    out = suggest_threshold(_events([10, 11], [40]), current_threshold=8)
    assert not out["ok"] and "need" in out["basis"]


def test_overlapping_clusters_decline():
    out = suggest_threshold(_events([10, 20, 30, 40, 50], [12, 22, 35]), current_threshold=8)
    assert not out["ok"] and "redesign" in out["basis"]


def test_close_to_current_declines():
    # separable, but the midpoint lands within 5% of the current threshold
    out = suggest_threshold(_events([9, 10, 10, 11, 10], [31, 33, 35]), current_threshold=21)
    assert not out["ok"] and "current threshold" in out["basis"]


def test_suggestions_by_rule_shapes():
    df = suggestions_by_rule(_events([10] * 6, [40] * 3), {"R": 8.0})
    assert list(df.columns) == ["RULE_ID", "CURRENT_THRESHOLD", "SUGGESTED_THRESHOLD",
                                "NOISE_N", "ACTIONED_N", "BASIS"]
    assert df.iloc[0]["NOISE_N"] == 6


# ---------------------------------------------------------------------------
# Forecast backtest
# ---------------------------------------------------------------------------


def test_backtest_flat_series_is_accurate():
    days = pd.date_range("2026-01-01", "2026-07-06", freq="D")
    daily = pd.DataFrame({"DAY": days, "USD": [100.0] * len(days)})
    out = backtest_forecasts(daily, months=3)
    assert not out.empty
    assert set(out["ENGINE"]) == {"linear", "seasonal"}
    assert out["ERROR_PCT"].abs().max() < 2.0  # flat spend: projections near-perfect
    assert set(out["CHECKPOINT_DAY"]) == {7, 14, 21}


def test_backtest_declines_without_history():
    out = backtest_forecasts(pd.DataFrame({"DAY": [], "USD": []}))
    assert out.empty


# ---------------------------------------------------------------------------
# Retro score history
# ---------------------------------------------------------------------------


def _inputs(days=10, **overrides):
    base = {
        "DAY": pd.date_range("2026-06-01", periods=days, freq="D"),
        "CREDITS_BILLED": [10.0] * days, "CRIT_RAISED": [0] * days,
        "HIGH_RAISED": [0] * days, "QUERY_COUNT": [1000] * days,
        "FAILED_COUNT": [0] * days, "QUEUED_SEC": [0.0] * days,
        "SPILL_GB": [0.0] * days,
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_clean_days_score_100():
    out = score_history(_inputs(), monthly_budget_usd=0.0)
    assert (out["SCORE"] == 100).all()


def test_bad_day_dips():
    crit = [0] * 10
    crit[4] = 3
    out = score_history(_inputs(CRIT_RAISED=crit), monthly_budget_usd=0.0)
    assert out.iloc[4]["SCORE"] < 100
    assert (out.drop(index=4)["SCORE"] == 100).all()


def test_budget_pct_is_month_cumulative():
    out = score_history(_inputs(days=30), monthly_budget_usd=100.0, rate_usd=1.0)
    # 10 credits/day at $1: day 10 = $100 = 100% of budget -> penalties grow
    assert out.iloc[-1]["SCORE"] < out.iloc[0]["SCORE"]


# ---------------------------------------------------------------------------
# Recurring patterns + re-check builders
# ---------------------------------------------------------------------------


def test_patterns_sql_shape():
    sql = insights_sql.expensive_patterns_usd(7, "Trexis", 30)
    assert "QUERY_PARAMETERIZED_HASH" in sql and "HAVING RUNS >= 5" in sql
    assert "CREDITS_PER_DAY" in sql and "WH_TRXS_LOAD" in sql
    assert "LIMIT 100" in insights_sql.expensive_patterns_usd(7, "ALL", 9999)


def test_recheck_covers_only_seeded_rules():
    seeded = set()
    for sql_file in (Path(__file__).resolve().parents[1] / "snowflake" / "migrations").glob("*.sql"):
        seeded.update(re.findall(r"'((?:COST|PERF|PIPE|SEC|OPS)_[A-Z_0-9]+)'",
                                 sql_file.read_text(encoding="utf-8")))
    assert set(recheck_sql.RECHECKABLE) <= seeded


def test_recheck_needs_warehouse_where_declared():
    assert recheck_sql.recheck_sql("COST_WH_DAILY_CREDITS", "") is None
    sql = recheck_sql.recheck_sql("COST_WH_DAILY_CREDITS", "WH_TRXS_LOAD")
    assert sql and "CURRENT_VALUE" in sql and "'WH_TRXS_LOAD'" in sql


def test_recheck_rejects_hostile_targets():
    assert recheck_sql.recheck_sql("COST_WH_DAILY_CREDITS", "WH; DROP TABLE X") is None
    assert recheck_sql.recheck_sql("NOT_A_RULE", "WH_X") is None


def test_recheck_account_rules_ignore_warehouse():
    sql = recheck_sql.recheck_sql("COST_CLOUD_SVC_RATIO")
    assert sql and "METERING_HISTORY" in sql


def test_score_inputs_and_metric_kinds_sql():
    assert "RESOLUTION_KIND IN ('ACTIONED', 'NOISE')" in mart_sql.rule_metric_kinds(90)
    sql = mart_sql.score_inputs_daily(30)
    for col in ("CRIT_RAISED", "QUEUED_SEC", "CREDITS_BILLED"):
        assert col in sql
