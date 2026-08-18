"""rec#16: aggregate the addressable-savings backlog into one de-duplicated total.
The critical rule: idle-tune and size-down on the SAME warehouse recover the same
idle credits, so they must never both count.
"""

from app.logic.savings_rollup import (
    SavingsOpportunity,
    confidence_weight,
    effort_tier,
    rollup_savings,
)


def _opp(source, target, usd, conf=0.6):
    return SavingsOpportunity(source, target, usd, conf)


def test_effort_tier_flags_quick_wins():
    assert effort_tier("IDLE") == "LOW" and effort_tier("RESIZE") == "LOW"
    assert effort_tier("CLUSTERING") == "HIGH"
    assert effort_tier("RETENTION") == "MEDIUM"
    assert effort_tier("something_new") == "MEDIUM"   # unknown -> MEDIUM


def test_idle_and_resize_on_the_same_warehouse_do_not_double_count():
    roll = rollup_savings([_opp("IDLE", "WH_A", 100.0), _opp("RESIZE", "WH_A", 80.0)])
    assert roll.total_monthly_usd == 100.0            # the larger, NOT 180
    assert len(roll.items) == 1 and roll.items[0].source == "IDLE"
    assert len(roll.dropped) == 1 and roll.dropped[0].source == "RESIZE"


def test_idle_and_resize_on_different_warehouses_both_count():
    roll = rollup_savings([_opp("IDLE", "WH_A", 100.0), _opp("RESIZE", "WH_B", 80.0)])
    assert roll.total_monthly_usd == 180.0
    assert len(roll.items) == 2 and not roll.dropped


def test_resize_wins_when_it_is_the_larger_estimate():
    roll = rollup_savings([_opp("IDLE", "WH_A", 40.0), _opp("RESIZE", "WH_A", 90.0)])
    assert roll.total_monthly_usd == 90.0
    assert roll.items[0].source == "RESIZE"


def test_non_overlapping_sources_are_always_kept():
    roll = rollup_savings([_opp("IDLE", "WH_A", 100.0), _opp("STORAGE", "WH_A", 50.0),
                           _opp("WASTE", "WH_A", 30.0)])
    assert roll.total_monthly_usd == 180.0            # STORAGE/WASTE don't overlap IDLE
    assert len(roll.items) == 3 and not roll.dropped


def test_ranking_is_confidence_times_dollars():
    # $60 at conf 1.0 (=60) outranks $100 at conf 0.3 (=30)
    roll = rollup_savings([_opp("STORAGE", "DB1", 100.0, 0.3), _opp("STORAGE", "DB2", 60.0, 1.0)])
    assert roll.items[0].target == "DB2"


def test_non_positive_estimates_are_ignored():
    roll = rollup_savings([_opp("IDLE", "WH_A", 0.0), _opp("RESIZE", "WH_B", -5.0)])
    assert roll.total_monthly_usd == 0.0 and not roll.items


def test_confidence_label_weights():
    assert confidence_weight("HIGH") == 1.0
    assert confidence_weight("MEDIUM") == 0.6
    assert confidence_weight("LOW") == 0.3
    assert confidence_weight("nonsense") == 0.3      # unknown -> low


def test_rollup_panel_collects_idle_and_resize_on_optimize():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages" / "cost_parts"
           / "optimize.py").read_text(encoding="utf-8")
    assert "rollup_savings(" in src
    assert "Total addressable savings" in src
    assert 'SavingsOpportunity("IDLE"' in src
    assert 'SavingsOpportunity("RESIZE"' in src
