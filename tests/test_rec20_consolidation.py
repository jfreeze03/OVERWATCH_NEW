"""rec#20: warehouse-fleet consolidation recommender — pair same-size, same-owner
warehouses whose active hours don't overlap, retiring the mostly-idle one.
"""

from app.logic.consolidation import WarehouseProfile, consolidation_candidates


def _wh(name, hours, *, size="SMALL", owner="ALFA", idle=100.0):
    return WarehouseProfile(name, size, owner, frozenset(hours), idle)


def test_non_overlapping_same_size_same_owner_is_a_candidate():
    prof = [_wh("WH_DAY", range(8, 17), idle=40.0),      # business hours
            _wh("WH_NIGHT", range(6), idle=120.0)]     # overnight, mostly idle
    out = consolidation_candidates(prof)
    assert len(out) == 1
    cand = out[0]
    # the fewer-active-hours warehouse (WH_NIGHT, 6h) is retired; its idle is the saving
    assert cand.retire == "WH_NIGHT" and cand.keep == "WH_DAY"
    assert cand.shared_hours == 0 and cand.est_monthly_saving_usd == 120.0


def test_overlapping_hours_disqualify_the_pair():
    prof = [_wh("WH_A", range(8, 17)), _wh("WH_B", range(12, 20))]   # overlap 12..16
    assert consolidation_candidates(prof) == []


def test_different_size_class_never_merges():
    prof = [_wh("WH_A", range(4), size="SMALL"),
            _wh("WH_B", range(8, 12), size="LARGE")]
    assert consolidation_candidates(prof) == []


def test_different_owner_never_merges():
    prof = [_wh("WH_A", range(4), owner="ALFA"),
            _wh("WH_B", range(8, 12), owner="TREXIS")]
    assert consolidation_candidates(prof) == []


def test_saving_below_floor_is_dropped():
    prof = [_wh("WH_A", range(8, 17), idle=1.0), _wh("WH_B", range(4), idle=2.0)]
    assert consolidation_candidates(prof, min_saving_usd=5.0) == []


def test_greedy_assignment_uses_each_warehouse_once():
    # A, B, C all mutually non-overlapping and same class/owner; only disjoint pairs kept
    prof = [_wh("A", [1], idle=300.0), _wh("B", [5], idle=200.0), _wh("C", [9], idle=100.0)]
    out = consolidation_candidates(prof)
    # all three pairs qualify, but every pair reuses A, B, or C — after the greedy pass
    # (highest saving first) only ONE non-conflicting merge survives.
    assert len(out) == 1
    assert len({out[0].keep, out[0].retire}) == 2


def test_consolidation_panel_wired_on_optimize():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "cost_parts"
           / "optimize.py").read_text(encoding="utf-8")
    assert "consolidation_candidates(" in src
    assert "opt_consolidation_toggle" in src
    assert "Fleet consolidation candidates" in src
