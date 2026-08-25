"""Proven-fix transfer engine (P1 #34): pure match logic + builder shape + wiring.

Cross-references VERIFIED savings wins with the current idle/sizing profiles to
suggest a proven fix on other matching warehouses. App-only, no migration.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic import proven_fix_transfer as pft
from app.logic.sizing import RECOMMEND_DOWN

_ROOT = Path(__file__).resolve().parents[1]


def _verified(*triples) -> pd.DataFrame:
    return pd.DataFrame([{"FIX_TYPE": ft, "TARGET_WAREHOUSE": wh, "VERIFIED_USD": usd}
                         for ft, wh, usd in triples])


def _idle(*rows) -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": wh, "COMPANY": "ALFA",
                          "ACTION_STATUS": status, "ACTIONABLE_MONTHLY_USD": est}
                         for wh, status, est in rows])


def _sizing(*rows) -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": wh, "COMPANY": "ALFA",
                          "RECOMMENDATION": rec, "SAVING_LOW_USD": est}
                         for wh, rec, est in rows])


# ============================================ match logic =====================

def test_auto_suspend_win_transfers_to_matching_idle_wh():
    out = pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 412.0)),
                                   idle_profiles=_idle(("WH_B", "ACTIONABLE", 180.0)))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["FIX_TYPE"] == "AUTO_SUSPEND"
    assert r["CANDIDATE_WAREHOUSE"] == "WH_B" and r["EVIDENCE_WAREHOUSE"] == "WH_A"
    assert r["CANDIDATE_EST_MONTHLY_USD"] == 180.0     # candidate's OWN estimate, not 412
    assert r["EVIDENCE_VERIFIED_USD"] == 412.0 and "ESTIMATE" in r["RATIONALE"]


def test_already_fixed_warehouses_excluded():
    idle = _idle(("WH_C", "ALREADY TUNED", 50.0), ("WH_E", "WITHIN TOLERANCE", 0.0),
                 ("WH_F", "VERIFY SETTING", 40.0))
    assert pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 412.0)),
                                    idle_profiles=idle).empty   # only ACTIONABLE transfers


def test_open_experiment_excluded():
    exp = pd.DataFrame([{"ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_D", "STATUS": "RUNNING"}])
    assert pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 412.0)),
                                    idle_profiles=_idle(("WH_D", "ACTIONABLE", 200.0)),
                                    open_experiments=exp).empty


def test_proven_warehouse_never_its_own_candidate():
    idle = _idle(("WH_A", "ACTIONABLE", 300.0), ("WH_B", "ACTIONABLE", 100.0))
    out = pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 412.0)), idle_profiles=idle)
    assert list(out["CANDIDATE_WAREHOUSE"]) == ["WH_B"]   # WH_A (proven) excluded


def test_below_min_verified_usd_not_seeded():
    assert pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 3.0)),
                                    idle_profiles=_idle(("WH_B", "ACTIONABLE", 200.0))).empty


def test_resize_win_transfers_and_size_setting_aliases():
    # a ledger RESIZE win and a registry SETTING='SIZE' win both match RECOMMEND_DOWN
    for fix in ("RESIZE", "SIZE"):
        out = pft.transfer_suggestions(
            _verified((fix, "WH_X", 250.0)),
            sizing_profiles=_sizing(("WH_Y", RECOMMEND_DOWN, 90.0), ("WH_Z", "Keep", 0.0)))
        assert list(out["CANDIDATE_WAREHOUSE"]) == ["WH_Y"]
        assert out.iloc[0]["FIX_TYPE"] == "RESIZE" and out.iloc[0]["CANDIDATE_EST_MONTHLY_USD"] == 90.0


def test_empty_inputs_and_ranking_by_estimate():
    assert pft.transfer_suggestions(None).empty
    assert pft.transfer_suggestions(pd.DataFrame()).empty
    idle = _idle(("WH_B", "ACTIONABLE", 100.0), ("WH_C", "ACTIONABLE", 300.0))
    out = pft.transfer_suggestions(_verified(("AUTO_SUSPEND", "WH_A", 412.0)), idle_profiles=idle)
    assert list(out["CANDIDATE_WAREHOUSE"]) == ["WH_C", "WH_B"]   # candidate est desc


def test_one_warehouse_both_fixes_dedups_to_higher_no_double_count():
    # WH_B is flagged for BOTH idle-tune and size-down; the two estimates come from
    # the same idle credits, so it must appear ONCE (the higher), never summed.
    out = pft.transfer_suggestions(
        _verified(("AUTO_SUSPEND", "WH_A", 412.0), ("RESIZE", "WH_X", 300.0)),
        idle_profiles=_idle(("WH_B", "ACTIONABLE", 100.0)),
        sizing_profiles=_sizing(("WH_B", RECOMMEND_DOWN, 60.0)))
    assert list(out["CANDIDATE_WAREHOUSE"]) == ["WH_B"]
    assert out.iloc[0]["FIX_TYPE"] == "AUTO_SUSPEND"
    assert float(out["CANDIDATE_EST_MONTHLY_USD"].sum()) == 100.0   # not 100 + 60


def test_size_up_win_not_transferred_as_downsize():
    sizing = _sizing(("WH_Y", RECOMMEND_DOWN, 90.0))
    up = pd.DataFrame([{"FIX_TYPE": "SIZE", "TARGET_WAREHOUSE": "WH_X", "VERIFIED_USD": 300.0,
                        "OLD_VALUE": "Small", "NEW_VALUE": "Large"}])
    assert pft.transfer_suggestions(up, sizing_profiles=sizing).empty       # size-up: no transfer
    down = pd.DataFrame([{"FIX_TYPE": "SIZE", "TARGET_WAREHOUSE": "WH_X", "VERIFIED_USD": 300.0,
                          "OLD_VALUE": "Large", "NEW_VALUE": "Small"}])
    assert len(pft.transfer_suggestions(down, sizing_profiles=sizing)) == 1  # size-down: transfers


# ============================================ builder + wiring =================

def test_verified_wins_builder_recovers_typing():
    sql = mart_sql.verified_wins()
    assert "STATE = 'VERIFIED'" in sql and "COALESCE(l.VERIFIED_USD, 0) > 0" in sql
    assert "WAREHOUSE_CHANGE_REGISTRY" in sql and "l.SOURCE_CHANGE_ID = r.CHANGE_ID" in sql
    assert "COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''), r.SETTING" in sql
    assert "AS TARGET_WAREHOUSE" in sql


def test_verified_wins_builder_company_scoped():
    # evidence must not leak across a company boundary
    assert "COMPANY_FOR_WAREHOUSE" not in mart_sql.verified_wins("ALL")
    scoped = mart_sql.verified_wins("Trexis")
    assert "COMPANY_FOR_WAREHOUSE" in scoped and "'Trexis'" in scoped


def test_wired_into_optimize():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    assert "transfer_suggestions(" in src and "verified_wins(company)" in src
    assert "Replicate a proven fix" in src
