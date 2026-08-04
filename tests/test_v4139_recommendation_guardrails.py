"""v4.139 recommendation-engine evidence and actionability locks."""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql, mart27_sql

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_sizing_readers_supply_active_day_evidence_on_both_paths():
    live = insights_sql.warehouse_sizing_profile(30, "ALFA")
    mart = mart27_sql.eff_sizing_profile(30, "ALFA")
    assert "ACTIVE_QUERY_DAYS" in live
    assert "COUNT(DISTINCT DATE(START_TIME))" in live
    assert "COUNT_IF(COALESCE(e.QUERIES, 0) > 0) AS ACTIVE_QUERY_DAYS" in mart


def test_idle_actionability_is_used_by_every_savings_consumer():
    optimize = _source("app/ui/pages/cost_parts/optimize.py")
    contract = _source("app/ui/pages/cost_parts/contract.py")
    assert optimize.count("with_auto_suspend_settings(") >= 2
    assert 'actionable = advisor[advisor["ACTIONABLE"]]' in optimize
    assert "ACTIONABLE_MONTHLY_USD" in optimize
    assert 'adv_st[adv_st["ACTIONABLE"]]' in contract
    steering = contract.split('key="steer_idle"', 1)[1]
    assert "PROJECTED_MONTHLY_IDLE_USD" not in steering[:2600]


def test_remediation_fails_closed_when_current_timer_is_unknown_or_tuned():
    optimize = _source("app/ui/pages/cost_parts/optimize.py")
    block = optimize.split('if fix_kind.startswith("Tighten"):', 1)[1]
    assert "if not _known:" in block[:1800]
    assert "elif 0 < _current <= IDLE_TARGET_SUSPEND_SEC:" in block[:1800]
    assert "stmt = remediation.auto_suspend_fix(wh_pick, _target)" in block[:1800]


def test_repeat_scan_and_capacity_ui_adopt_normalized_and_stale_contracts():
    optimize = _source("app/ui/pages/cost_parts/optimize.py")
    assert "repeat_min_runs(bounded_days(days, 400))" in optimize
    assert "repeat_min_runs(bounded_days(days))" in optimize
    assert "AVOIDABLE_USD_PER_30D" in optimize
    assert 'statuses.get("STALE", 0)' in optimize
    assert "movers[\"PROJECTABLE\"]" in optimize


def test_measured_pattern_readers_normalize_recurrence_by_window():
    assert "HAVING RUNS >= 2" in insights_sql.expensive_patterns_usd(7, "ALFA")
    assert "HAVING RUNS >= 15" in insights_sql.expensive_patterns_usd(90, "ALFA")
    assert "SUM(p.RUNS) >= 2" in mart27_sql.pattern_cost(7, "ALFA")
    assert "SUM(p.RUNS) >= 15" in mart27_sql.pattern_cost(90, "ALFA")


def test_retention_optimizer_defaults_to_no_change_and_requires_verified_reduction():
    optimize = _source("app/ui/pages/cost_parts/optimize.py")
    block = optimize.split('key="waste_days"', 1)[1]
    assert "_default_ret" in optimize
    assert "_ret_known and _cur_ret > 0 and int(keep_days) < _cur_ret" in block[:1200]
    assert "if not _ret_known:" in block[:1200]
    assert "stmt_w = remediation.retention_fix" in block[:1800]
