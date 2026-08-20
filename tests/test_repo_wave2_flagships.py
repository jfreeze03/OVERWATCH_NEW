"""Repo-review wave-2 flagship leftovers (owner 2026-08-20), mined from the
snowmonitor reference repo — the four genuine gaps a mature app hadn't adopted:

  A. Task actively-broken streak      (ops_sql.task_recent_states + logic.task_failure_streaks)
  B. Per-task freshness SLA           (ops_sql.task_freshness_sla + logic.task_freshness_status)
  C. Account-takeover detection       (security_sql.login_takeover_candidates + logic.takeover_severity)
  D. Heavy-query optimization triage  (ops_sql.query_optimization_triage)

Pure scorers are unit-tested on frames; SQL builders are shape-tested (the app's
convention — builders are pure strings, no Snowflake in tests)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.data import ops_sql, security_sql
from app.logic.insights import (
    takeover_severity,
    task_failure_streaks,
    task_freshness_status,
)

# ============================================================ A. streaks ====

def _states(states, task="T", db="D", sch="S"):
    """Newest-first run rows for one task; states[0] is the newest run."""
    base = datetime(2026, 8, 20, 6, 0, 0)
    return pd.DataFrame({
        "DATABASE_NAME": [db] * len(states),
        "SCHEMA_NAME": [sch] * len(states),
        "TASK_NAME": [task] * len(states),
        "SCHEDULED_TIME": [base - timedelta(hours=i) for i in range(len(states))],
        "STATE": states,
        "ERROR_MESSAGE": ["boom" if s == "FAILED" else "" for s in states],
    })


def test_streak_counts_leading_failures_until_last_success():
    out = task_failure_streaks(_states(["FAILED", "FAILED", "FAILED", "SUCCEEDED", "FAILED"]))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["FAIL_STREAK"] == 3          # stops at the SUCCEEDED, the 4th newest
    assert bool(r["ACTIVELY_BROKEN"]) is True
    assert r["SEVERITY"] == "High"        # >= 3
    assert r["LAST_ERROR"] == "boom"


def test_newest_success_means_no_streak_row():
    # newest run SUCCEEDED -> not currently broken -> dropped entirely.
    assert task_failure_streaks(_states(["SUCCEEDED", "FAILED", "FAILED"])).empty


def test_streak_severity_bands_and_actively_broken_threshold():
    one = task_failure_streaks(_states(["FAILED", "SUCCEEDED"])).iloc[0]
    assert one["FAIL_STREAK"] == 1 and one["SEVERITY"] == "Low"
    assert bool(one["ACTIVELY_BROKEN"]) is False   # a single failure is not "broken"
    two = task_failure_streaks(_states(["FAILED", "FAILED", "SUCCEEDED"])).iloc[0]
    assert two["SEVERITY"] == "Medium" and bool(two["ACTIVELY_BROKEN"]) is True


def test_streak_never_succeeded_counts_whole_window():
    out = task_failure_streaks(_states(["FAILED", "FAILED"]))
    assert out.iloc[0]["FAIL_STREAK"] == 2      # no SUCCEEDED to stop at


def test_streaks_rank_worst_first_and_empty_safe():
    a = _states(["FAILED", "SUCCEEDED"], task="A")
    b = _states(["FAILED", "FAILED", "FAILED", "SUCCEEDED"], task="B")
    out = task_failure_streaks(pd.concat([a, b], ignore_index=True))
    assert list(out["TASK_NAME"]) == ["B", "A"]  # 3-streak before 1-streak
    assert task_failure_streaks(pd.DataFrame()).empty
    assert task_failure_streaks(pd.DataFrame({"X": [1]})).empty


# ========================================================== B. freshness ====

def _fresh(gap, mins_since):
    return pd.DataFrame({
        "DATABASE_NAME": ["D"], "SCHEMA_NAME": ["S"], "TASK_NAME": ["T"],
        "MEDIAN_GAP_MIN": [gap], "MINS_SINCE_SUCCESS": [mins_since],
    })


def test_freshness_on_time_within_cadence_plus_lag():
    # 40 min overdue is inside the ~45-min ACCOUNT_USAGE lag -> not flagged.
    assert task_freshness_status(_fresh(60.0, 100.0)).iloc[0]["STATUS"] == "On-time"


def test_freshness_late_between_one_and_two_cadences():
    r = task_freshness_status(_fresh(60.0, 110.0)).iloc[0]   # overdue 50 (>=45), < 2x gap(120)
    assert r["STATUS"] == "Late" and r["SEVERITY"] == "Medium"


def test_freshness_stale_beyond_two_cadences():
    r = task_freshness_status(_fresh(60.0, 200.0)).iloc[0]   # >= 120 and overdue 140
    assert r["STATUS"] == "Stale" and r["SEVERITY"] == "High"
    assert r["OVERDUE_MIN"] == 140.0


def test_freshness_never_succeeded_is_stale():
    r = task_freshness_status(_fresh(30.0, float("nan"))).iloc[0]
    assert r["STATUS"] == "Stale" and r["SEVERITY"] == "High"


def test_freshness_empty_safe():
    assert task_freshness_status(pd.DataFrame()).empty
    assert task_freshness_status(pd.DataFrame({"X": [1]})).empty


# ========================================================== C. takeover =====

def test_takeover_breakthrough_with_volume_is_high():
    df = pd.DataFrame({"USER_NAME": ["u"], "FAILURES": [12], "FAIL_IPS": [4],
                       "SUCCEEDED_AFTER": [True]})
    assert takeover_severity(df).iloc[0]["SEVERITY"] == "High"


def test_takeover_burst_without_success_is_low():
    # a failure burst that never succeeds is a locked-out user, not a breach.
    df = pd.DataFrame({"USER_NAME": ["u"], "FAILURES": [50], "FAIL_IPS": [9],
                       "SUCCEEDED_AFTER": [False]})
    assert takeover_severity(df).iloc[0]["SEVERITY"] == "Low"


def test_takeover_ip_spread_escalates_to_high():
    # a breakthrough with < 10 failures but 3+ distinct source IPs is still High.
    df = pd.DataFrame({"USER_NAME": ["u"], "FAILURES": [6], "FAIL_IPS": [3],
                       "SUCCEEDED_AFTER": [True]})
    assert takeover_severity(df).iloc[0]["SEVERITY"] == "High"


def test_scorers_never_raise_on_absent_columns():
    # freshness: MEDIAN_GAP_MIN present but MINS_SINCE_SUCCESS absent -> Stale, no raise.
    fr = task_freshness_status(pd.DataFrame({"MEDIAN_GAP_MIN": [60.0]}))
    assert fr.iloc[0]["STATUS"] == "Stale"
    # takeover: SUCCEEDED_AFTER present but FAILURES/FAIL_IPS absent -> no raise, Low.
    tk = takeover_severity(pd.DataFrame({"USER_NAME": ["u"], "SUCCEEDED_AFTER": [True]}))
    assert tk.iloc[0]["SEVERITY"] == "Medium"   # breakthrough, but 0 failures/IPs -> not High


def test_takeover_small_breakthrough_is_medium_and_sorted_first():
    df = pd.DataFrame({
        "USER_NAME": ["low", "mid"],
        "FAILURES": [50, 6], "FAIL_IPS": [9, 1],
        "SUCCEEDED_AFTER": [False, True],
    })
    out = takeover_severity(df)
    assert out.iloc[0]["USER_NAME"] == "mid"          # Medium breakthrough outranks Low burst
    assert out.iloc[0]["SEVERITY"] == "Medium"
    assert takeover_severity(pd.DataFrame()).empty


# ===================================================== SQL builder shapes ====

def test_task_recent_states_shape():
    sql = ops_sql.task_recent_states(7, "ALFA", schema_contains="DW")
    assert "ROW_NUMBER() OVER (" in sql and "ORDER BY SCHEDULED_TIME DESC) <= 12" in sql
    assert "STATE IN ('SUCCEEDED', 'FAILED')" in sql
    assert "SCHEDULED_TIME >= DATEADD('day', -7, CURRENT_DATE())" in sql
    assert "ACCOUNT_USAGE.TASK_HISTORY" in sql
    # retries share SCHEDULED_TIME -> collapse each run to its terminal attempt
    assert "PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in sql
    assert "ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1" in sql


def test_task_freshness_sla_shape():
    sql = ops_sql.task_freshness_sla(14, "ALL")
    # median over POSITIVE gaps only (zero-minute retry gaps excluded, matching INTERVALS)
    assert "MEDIAN(IFF(GAP_MIN > 0, GAP_MIN, NULL))" in sql and "LAG(SCHEDULED_TIME) OVER (" in sql
    # silence measured to NOW from the last SUCCEEDED (LTZ, so DATEDIFF vs CURRENT_TIMESTAMP is correct)
    assert "MAX(IFF(STATE = 'SUCCEEDED', SCHEDULED_TIME, NULL)) AS LAST_SUCCESS" in sql
    assert "DATEDIFF('minute', l.LAST_SUCCESS, CURRENT_TIMESTAMP()) AS MINS_SINCE_SUCCESS" in sql
    assert "INTERVALS >= 3" in sql                 # only trustworthy cadences
    # the silent (NULL last-success) tasks must survive LIMIT, not be truncated first
    assert "ORDER BY MINS_SINCE_SUCCESS DESC NULLS FIRST" in sql


def test_query_optimization_triage_shape():
    sql = ops_sql.query_optimization_triage(7, "ALFA", user_contains="svc")
    # real-work filter: no CALL wrappers, no app queries, must have a real inefficiency
    assert "QUERY_TYPE <> 'CALL'" in sql
    assert "NOT LIKE 'EXECUTE STREAMLIT%'" in sql and "NOT LIKE '%OVERWATCH_APP%'" in sql
    assert "BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0 " in sql and "> 50 * POWER(1024, 3)" in sql
    # ranked worst-first: spill, then pruning ratio, then scan
    assert "ORDER BY\n    COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0) DESC" in sql
    # columns named for styled_table auto-humanize
    for col in ("SPILL_REMOTE_GB", "GB_SCANNED", "SCAN_PCT", "CACHE_PCT", "REASON"):
        assert col in sql, col


def test_login_takeover_candidates_shape():
    sql = security_sql.login_takeover_candidates(7, "ALL", min_failures=5)
    assert "IS_SUCCESS = 'NO'" in sql and "IS_SUCCESS = 'YES'" in sql
    assert "HAVING COUNT(*) >= 5" in sql
    # the breakthrough: earliest success AFTER the first failure
    assert "e.EVENT_TIMESTAMP > f.FIRST_FAILURE" in sql
    assert "IFF(sa.FIRST_SUCCESS_AFTER IS NOT NULL, TRUE, FALSE) AS SUCCEEDED_AFTER" in sql
    # event-grain live read, anchored on CURRENT_TIMESTAMP (hours matter), 30d cap
    assert "EVENT_TIMESTAMP >= DATEADD('day', -7, CURRENT_TIMESTAMP())" in sql
    assert "ACCOUNT_USAGE.LOGIN_HISTORY" in sql


def test_takeover_reads_are_company_scoped():
    # ALL -> no company predicate; a company -> COMPANY_FOR_USER filter folds in.
    assert "COMPANY_FOR_USER" not in security_sql.login_takeover_candidates(7, "ALL")
    assert "COMPANY_FOR_USER" in security_sql.login_takeover_candidates(7, "Trexis")


def test_triage_and_streak_wired_into_operations():
    from pathlib import Path
    ops = Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "operations.py"
    src = ops.read_text(encoding="utf-8")
    assert "query_optimization_triage(" in src and "Optimization triage" in src
    assert "_task_sla_view(" in src and 'nested_sections(\n        ["Health", "SLA"' in src


def test_takeover_wired_into_security():
    from pathlib import Path
    sec = Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "security.py"
    src = sec.read_text(encoding="utf-8")
    assert "login_takeover_candidates(" in src and "sec_takeover_toggle" in src
