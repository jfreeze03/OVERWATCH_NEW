"""Prove-it scorecard: account-wide alert precision, action acceptance, ROI multiple,
and the earning-its-keep verdict — the numbers that gate autonomy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.logic.proof import (
    acceptance_summary,
    account_precision,
    proof_verdict,
    roi_multiple,
)

_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------- account precision ----

def test_account_precision_rolls_rules_into_one_number():
    df = pd.DataFrame([
        {"RULE_ID": "A", "ACTIONED": 8, "NOISE": 2, "EXPECTED": 1, "UNTAGGED": 0, "RESOLVED_EVENTS": 11},
        {"RULE_ID": "B", "ACTIONED": 2, "NOISE": 3, "EXPECTED": 0, "UNTAGGED": 5, "RESOLVED_EVENTS": 10},
    ])
    p = account_precision(df)
    assert p["ACTIONED"] == 10 and p["NOISE"] == 5
    assert p["PRECISION_PCT"] == round(100 * 10 / 15, 1)   # 66.7 — EXPECTED excluded
    assert p["UNTAGGED_SHARE_PCT"] == round(100 * 5 / 21, 1)  # 5 untagged / 21 resolved
    assert p["RULES"] == 2


def test_account_precision_no_data_is_none_not_zero():
    # Nothing resolved with a kind -> precision is None (unknown), never a misleading 0%.
    assert account_precision(None)["PRECISION_PCT"] is None
    assert account_precision(pd.DataFrame())["PRECISION_PCT"] is None
    allexpected = pd.DataFrame([{"ACTIONED": 0, "NOISE": 0, "EXPECTED": 4, "UNTAGGED": 0, "RESOLVED_EVENTS": 4}])
    assert account_precision(allexpected)["PRECISION_PCT"] is None  # no ACTIONED+NOISE denom


# --------------------------------------------------------- acceptance ----

def test_acceptance_is_done_over_decided():
    row = pd.DataFrame([{"DONE_N": 6, "DROPPED_N": 4, "OPEN_N": 12, "DONE_USD": 5000.0}])
    a = acceptance_summary(row)
    assert a["DECIDED"] == 10 and a["ACCEPTANCE_PCT"] == 60.0
    assert a["OPEN_N"] == 12 and a["DONE_USD"] == 5000.0


def test_acceptance_none_when_nothing_decided():
    a = acceptance_summary({"DONE_N": 0, "DROPPED_N": 0, "OPEN_N": 9})
    assert a["ACCEPTANCE_PCT"] is None and a["DECIDED"] == 0
    assert acceptance_summary(None)["ACCEPTANCE_PCT"] is None
    assert acceptance_summary(pd.DataFrame())["ACCEPTANCE_PCT"] is None


# --------------------------------------------------------- ROI multiple ----

def test_roi_multiple_pays_for_itself():
    r = roi_multiple(41000.0, 10000.0)
    assert r["RATIO"] == 4.1 and r["PAYS"] is True and r["NET_USD"] == 31000.0


def test_roi_multiple_under_one_does_not_pay_and_zero_cost_is_none():
    assert roi_multiple(500.0, 2000.0)["PAYS"] is False
    z = roi_multiple(500.0, 0.0)
    assert z["RATIO"] is None and z["PAYS"] is False    # can't divide by an unknown run cost


# --------------------------------------------------------- verdict ----

def test_verdict_good_when_all_signals_healthy():
    v = proof_verdict(roi_multiple(40000, 10000), realization_pct=85.0, acceptance_pct=70.0,
                      precision={"PRECISION_PCT": 88.0, "UNTAGGED_SHARE_PCT": 5.0})
    assert v["level"] == "good" and "earning its keep" in v["headline"]


def test_verdict_watch_lists_worst_first_reasons():
    v = proof_verdict(roi_multiple(4000, 10000), realization_pct=40.0, acceptance_pct=20.0,
                      precision={"PRECISION_PCT": 55.0, "UNTAGGED_SHARE_PCT": 10.0})
    assert v["level"] == "watch"
    assert "run cost not yet covered" in v["headline"] and "acts on only 20%" in v["headline"]


def test_verdict_unproven_when_no_labeled_outcomes():
    v = proof_verdict(roi_multiple(0, 0), realization_pct=None, acceptance_pct=None,
                      precision={"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0.0})
    assert v["level"] == "unproven" and "Not enough verified outcomes" in v["headline"]


# --------------------------------------------------------- mart read ----

def test_action_acceptance_sql_shape():
    sqlglot = pytest.importorskip("sqlglot")
    from app.data import mart_sql
    sql = mart_sql.action_acceptance(90)
    sqlglot.parse(sql, dialect="snowflake")
    for col in ("DONE_N", "DROPPED_N", "OPEN_N", "DONE_USD"):
        assert col in sql
    assert "ACTION_QUEUE" in sql and "UPDATED_AT >=" in sql


def test_proof_is_wired_into_decision_studio():
    ds = (_ROOT / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")
    page = (_ROOT / "app" / "ui" / "pages" / "decision_studio.py").read_text(encoding="utf-8")
    assert "account_precision(" in ds and "acceptance_summary(" in ds and "proof_verdict(" in ds
    assert "Scorecard" in page
