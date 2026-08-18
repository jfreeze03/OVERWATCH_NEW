"""rec#6: serverless ROI board (Query Acceleration arm) — pair QAS spend with the
eligible-workload benefit signal and classify each warehouse.
"""

from pathlib import Path

from app.data import cost_sql
from app.logic.serverless_roi import classify_qas_roi

_OPT = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages" / "cost_parts"
        / "optimize.py").read_text(encoding="utf-8")


# --- pure classifier -------------------------------------------------------
def test_paying_with_little_eligible_workload_is_a_drop_candidate():
    v = classify_qas_roi(100.0, 1)          # $100 QAS spend, 1 eligible query
    assert v.action == "drop" and "little benefit" in v.verdict


def test_eligible_workload_with_qas_off_is_an_enable_candidate():
    v = classify_qas_roi(0.0, 50)           # no QAS spend, 50 eligible queries
    assert v.action == "enable" and "opportunity" in v.verdict


def test_paying_and_eligible_is_keep():
    assert classify_qas_roi(100.0, 50).action == "keep"


def test_neither_material_is_minimal():
    v = classify_qas_roi(1.0, 1)
    assert v.action == "" and "Minimal" in v.verdict


def test_non_numeric_eligibility_is_treated_as_not_eligible():
    # a NULL eligible count must not crash and must not read as eligible
    assert classify_qas_roi(100.0, None).action == "drop"


# --- builder ---------------------------------------------------------------
def test_builder_pairs_spend_with_the_eligibility_benefit_signal():
    sql = cost_sql.qas_roi(30, "ALFA")
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_ACCELERATION_ELIGIBLE" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_ACCELERATION_HISTORY" in sql
    assert "FULL OUTER JOIN" in sql                       # both regimes surface
    assert "ELIGIBLE_QUERY_ACCELERATION_TIME" in sql      # the benefit signal
    assert "CREDITS_USED" in sql                          # the spend side


# --- panel -----------------------------------------------------------------
def test_qas_roi_panel_wired_on_optimize():
    assert "qas_roi(" in _OPT
    assert "opt_qas_roi_toggle" in _OPT
    assert "classify_qas_roi(" in _OPT
