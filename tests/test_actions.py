import pandas as pd

from app.logic.actions import (
    LEDGER_ESTIMATED,
    LEDGER_VERIFIED,
    can_verify,
    ledger_totals,
    rank_actions,
    triage_queue,
)


def test_rank_actions_severity_then_overdue():
    df = pd.DataFrame([
        {"SEVERITY": "LOW", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01", "TITLE": "low"},
        {"SEVERITY": "CRITICAL", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-06", "TITLE": "crit"},
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": "2026-01-01", "CREATED_AT": "2026-07-05", "TITLE": "high-overdue"},
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": "2099-01-01", "CREATED_AT": "2026-07-05", "TITLE": "high-future"},
        {"SEVERITY": "CRITICAL", "STATUS": "DONE", "DUE_DATE": None, "CREATED_AT": "2026-07-01", "TITLE": "closed"},
    ])
    ranked = rank_actions(df)
    titles = ranked["TITLE"].tolist()
    assert titles[0] == "crit"
    assert titles[1] == "high-overdue"
    assert "closed" not in titles


def test_rank_actions_empty():
    assert rank_actions(pd.DataFrame()).empty


def test_verify_requires_proof_and_amount():
    ok, why = can_verify({"STATE": LEDGER_ESTIMATED, "PROOF_SQL": "", "VERIFIED_USD": 10})
    assert not ok and "proof" in why.lower()
    ok, why = can_verify({"STATE": LEDGER_ESTIMATED, "PROOF_SQL": "select 1", "VERIFIED_USD": None})
    assert not ok and "numeric" in why.lower()
    ok, why = can_verify({"STATE": LEDGER_VERIFIED, "PROOF_SQL": "select 1", "VERIFIED_USD": 10})
    assert not ok
    ok, why = can_verify({"STATE": LEDGER_ESTIMATED, "PROOF_SQL": "select 1", "VERIFIED_USD": 10})
    assert ok and why == ""


def test_ledger_totals_never_mix_states():
    df = pd.DataFrame([
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 100, "VERIFIED_USD": None},
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 50, "VERIFIED_USD": None},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 80, "VERIFIED_USD": 60},
        {"STATE": "REJECTED", "ESTIMATED_USD": 999, "VERIFIED_USD": 999},
    ])
    totals = ledger_totals(df)
    assert totals["estimated_usd"] == 150.0
    assert totals["verified_usd"] == 60.0
    assert totals["estimated_count"] == 2
    assert totals["verified_count"] == 1


def test_triage_queue_merges_and_ranks():
    alerts = pd.DataFrame([{"SEVERITY": "CRITICAL", "TITLE": "spend spike", "DETAIL": "x", "RAISED_AT": "2026-07-07"}])
    tasks = pd.DataFrame([
        {"TASK_NAME": "LOAD_A", "FAILED": 4, "LAST_ERROR": "boom", "DAY": "2026-07-06"},
        {"TASK_NAME": "LOAD_B", "FAILED": 0, "LAST_ERROR": "", "DAY": "2026-07-06"},
    ])
    anomalies = [{"label": "WH_X", "value": 900.0, "z": 6.2}]
    queue = triage_queue(alerts, tasks, anomalies)
    assert queue.iloc[0]["KIND"] == "Alert"
    kinds = set(queue["KIND"])
    assert kinds == {"Alert", "Task failure", "Spend anomaly"}
    assert len(queue) == 3  # zero-failure task excluded


def test_triage_queue_empty_inputs():
    assert triage_queue(None, None, None).empty
