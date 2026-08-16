import pandas as pd

from app.logic.actions import (
    ANOMALY_HIGH_EXCESS_USD,
    ANOMALY_HIGH_Z,
    LEDGER_ESTIMATED,
    LEDGER_VERIFIED,
    can_verify,
    ledger_totals,
    rank_actions,
    since_last_visit_summary,
    triage_queue,
)
from app.logic.formulas import account_today


def test_since_last_visit_summary_quiet_and_severity():
    # Cost3: nothing new -> quiet/ok note.
    quiet = since_last_visit_summary(0, 0, 0, 0)
    assert quiet["quiet"] and quiet["severity"] == "ok" and "nothing new" in quiet["text"]
    # a new critical escalates to bad and names the critical count.
    bad = since_last_visit_summary(3, 1, 0, 2)
    assert bad["severity"] == "bad" and "3 new alerts (1 critical)" in bad["text"]
    assert "2 new actions" in bad["text"]
    # highs (no crit) -> warn, and singular/plural render correctly.
    warn = since_last_visit_summary(1, 0, 1, 0)
    assert warn["severity"] == "warn" and warn["text"] == "1 new alert (1 high)"
    # actions only (no alerts) -> warn on new actions is off; alerts drive it.
    acts = since_last_visit_summary(0, 0, 0, 1)
    assert acts["severity"] == "ok" and acts["text"] == "1 new action" and not acts["quiet"]


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
    assert totals["verified_estimated_usd"] == 80.0
    assert totals["realization_pct"] == 75.0        # verified 60 of an 80 estimate


def test_ledger_totals_realization_none_without_verified():
    df = pd.DataFrame([{"STATE": "ESTIMATED", "ESTIMATED_USD": 100, "VERIFIED_USD": None}])
    totals = ledger_totals(df)
    assert totals["realization_pct"] is None and totals["verified_estimated_usd"] == 0.0


def test_triage_queue_merges_and_ranks():
    alerts = pd.DataFrame([{"SEVERITY": "CRITICAL", "TITLE": "spend spike", "DETAIL": "x", "RAISED_AT": "2026-07-07"}])
    tasks = pd.DataFrame([
        {"TASK_NAME": "LOAD_A", "DATABASE_NAME": "ALFA_EDW_PROD", "SCHEMA_NAME": "DW",
         "FAILED": 4, "LAST_ERROR": "boom", "DAY": "2026-07-06"},
        {"TASK_NAME": "LOAD_B", "DATABASE_NAME": "ALFA_EDW_DEV", "SCHEMA_NAME": "DW",
         "FAILED": 0, "LAST_ERROR": "", "DAY": "2026-07-06"},
    ])
    anomalies = [{"label": "WH_X", "value": 900.0, "z": 6.2}]
    queue = triage_queue(alerts, tasks, anomalies)
    assert queue.iloc[0]["KIND"] == "Alert"
    kinds = set(queue["KIND"])
    assert kinds == {"Alert", "Task failure", "Spend anomaly"}
    assert len(queue) == 3  # zero-failure task excluded
    # Owner requirement: failed tasks must show their database.
    task_row = queue[queue["KIND"] == "Task failure"].iloc[0]
    assert task_row["DATABASE"] == "ALFA_EDW_PROD"
    assert task_row["TITLE"].startswith("ALFA_EDW_PROD.DW.LOAD_A")
    assert "DATABASE" in queue.columns


def test_triage_queue_raised_at_is_arrow_safe_text():
    """Regression: mixed timestamp/date/None RAISED_AT crashed st.dataframe
    ('Conversion failed for column RAISED_AT with type object')."""
    from datetime import date, datetime

    alerts = pd.DataFrame([{"SEVERITY": "HIGH", "TITLE": "a", "DETAIL": "",
                            "RAISED_AT": datetime(2026, 7, 7, 2, 0)}])
    tasks = pd.DataFrame([{"TASK_NAME": "T", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S",
                           "FAILED": 1, "LAST_ERROR": "", "DAY": date(2026, 7, 6)}])
    anomalies = [{"label": "WH", "value": 1.0, "z": 6.0}]
    queue = triage_queue(alerts, tasks, anomalies)
    assert all(isinstance(v, str) for v in queue["RAISED_AT"])
    assert "" in set(queue["RAISED_AT"])  # anomaly row normalized to empty text


def test_triage_queue_empty_inputs():
    assert triage_queue(None, None, None).empty


# --- D1: $-aware, day-aligned, unknown-severity-visible action ranking ---------

def test_rank_actions_dollars_break_ties_within_severity():
    df = pd.DataFrame([
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-01-01",
         "ESTIMATED_USD": 40, "TITLE": "old-cheap"},
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 40000, "TITLE": "new-expensive"},
        {"SEVERITY": "CRITICAL", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 1, "TITLE": "crit"},
    ])
    titles = rank_actions(df)["TITLE"].tolist()
    # severity still dominates; dollars order the tie inside the HIGH band
    assert titles == ["crit", "new-expensive", "old-cheap"]


def test_rank_actions_due_today_is_not_overdue():
    """Regression: DUE_DATE lands at midnight, so `due < now` called an action
    overdue from 00:00:01 of the day it was due."""
    today = account_today().isoformat()
    df = pd.DataFrame([
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": today, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 0, "TITLE": "due-today"},
        {"SEVERITY": "HIGH", "STATUS": "OPEN", "DUE_DATE": "2020-01-01", "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 0, "TITLE": "genuinely-overdue"},
    ])
    assert rank_actions(df)["TITLE"].tolist() == ["genuinely-overdue", "due-today"]


def test_rank_actions_unknown_severity_is_flagged_not_buried():
    df = pd.DataFrame([
        {"SEVERITY": "URGENT", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 0, "TITLE": "mislabeled"},
        {"SEVERITY": "", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 0, "TITLE": "blank"},
        {"SEVERITY": "INFO", "STATUS": "OPEN", "DUE_DATE": None, "CREATED_AT": "2026-07-01",
         "ESTIMATED_USD": 0, "TITLE": "info"},
    ])
    ranked = rank_actions(df)
    # both unknowns rank at the MEDIUM tier, i.e. ABOVE INFO — not silently last
    assert ranked["TITLE"].tolist()[-1] == "info"
    flags = set(ranked[ranked["TITLE"] != "info"]["SEVERITY"])
    assert flags == {"URGENT?", "UNSET?"}


def test_rank_actions_survives_missing_optional_columns():
    df = pd.DataFrame([{"SEVERITY": "HIGH", "STATUS": "OPEN", "TITLE": "bare"}])
    assert rank_actions(df)["TITLE"].tolist() == ["bare"]


# --- C2 / D6: triage feed dedupe, $-aware escalation, per-task collapse --------

def test_triage_queue_drops_the_server_sweep_twin():
    alerts = pd.DataFrame([
        {"RULE_ID": "COST_ANOMALY_SWEEP", "SEVERITY": "MEDIUM", "TITLE": "WAREHOUSE X spent...",
         "DETAIL": "", "RAISED_AT": "2026-07-07"},
        {"RULE_ID": "TASK_FAILURE_BURST", "SEVERITY": "HIGH", "TITLE": "real alert",
         "DETAIL": "", "RAISED_AT": "2026-07-07"},
    ])
    queue = triage_queue(alerts, None, [])
    assert queue["TITLE"].tolist() == ["real alert"]


def test_triage_queue_high_on_dollars_even_at_modest_z():
    """D6: z below the (server-aligned) escalation bar but real money -> HIGH."""
    tame_z = ANOMALY_HIGH_Z - 2
    anomalies = [
        {"label": "PROD_WH", "value": 9000.0, "z": tame_z,
         "excess_usd": ANOMALY_HIGH_EXCESS_USD * 10, "day": "2026-07-07"},
        {"label": "SANDBOX_WH", "value": 14.0, "z": tame_z, "excess_usd": 9.0, "day": "2026-07-07"},
    ]
    queue = triage_queue(None, None, anomalies)
    by_title = dict(zip(queue["TITLE"], queue["SEVERITY"], strict=True))
    assert [s for t, s in by_title.items() if t.startswith("PROD_WH")] == ["HIGH"]
    assert [s for t, s in by_title.items() if t.startswith("SANDBOX_WH")] == ["MEDIUM"]
    # and the expensive one leads the list
    assert queue.iloc[0]["TITLE"].startswith("PROD_WH")


def test_triage_queue_z_gate_matches_the_server_sweep():
    """C2: the app escalated at z>=5 while SP_ANOMALY_SWEEP escalates at 2*3.5."""
    assert ANOMALY_HIGH_Z == 7.0
    mid = triage_queue(None, None, [{"label": "W", "value": 1.0, "z": 6.0, "day": "d"}])
    assert mid.iloc[0]["SEVERITY"] == "MEDIUM"
    hot = triage_queue(None, None, [{"label": "W", "value": 1.0, "z": 7.5, "day": "d"}])
    assert hot.iloc[0]["SEVERITY"] == "HIGH"


def test_triage_queue_collapses_per_day_task_rows():
    """D6: FACT_TASK_DAILY is DAY grain — two days of the same task was two rows,
    each severity-scored on one day's slice."""
    tasks = pd.DataFrame([
        {"TASK_NAME": "LOAD_A", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "FAILED": 2,
         "LAST_ERROR": "older", "DAY": "2026-07-06"},
        {"TASK_NAME": "LOAD_A", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "FAILED": 2,
         "LAST_ERROR": "newest", "DAY": "2026-07-07"},
    ])
    queue = triage_queue(None, tasks, [])
    assert len(queue) == 1
    row = queue.iloc[0]
    assert "failed 4x" in row["TITLE"]      # summed, not one day's slice
    assert row["SEVERITY"] == "HIGH"        # 4 >= 3, which neither day reached alone
    assert row["DETAIL"] == "newest"        # newest day's error survives
