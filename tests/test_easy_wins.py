"""Repo-review EASY-win leftovers (owner 2026-08-20), mined from the snowmonitor
reference repo — five genuine gaps, all read-only / no migration:

  #14 Systemic error roll-up      logic.insights.cluster_failures_by_family
  #15 Task duration drift         logic.insights.task_duration_anomalies
  #6  Auto-clustering churn        logic.insights.flag_clustering_churn
  #16 MFA-bypass (single-factor)   data.security_sql.single_factor_logins
  #11 Budget pace-variance         logic.formulas.budget_pace_variance
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from app.data import security_sql
from app.logic.formulas import budget_burndown, budget_pace_variance
from app.logic.insights import (
    cluster_failures_by_family,
    flag_clustering_churn,
    suspend_recluster_sql,
    task_duration_anomalies,
)

_ROOT = Path(__file__).resolve().parents[1]


# ================================================= #14 systemic errors ======

def _timeline(families_tasks):
    rows = []
    for fam, task in families_tasks:
        rows.append({"ERROR_FAMILY": fam, "DATABASE_NAME": "D", "SCHEMA_NAME": "S",
                     "TASK_NAME": task, "ERROR_MESSAGE": f"{fam} on {task}"})
    return pd.DataFrame(rows)


def test_systemic_flags_one_family_across_many_tasks():
    tl = _timeline([("Permission", "T1"), ("Permission", "T2"), ("Permission", "T3"),
                    ("Timeout", "T4")])
    out = cluster_failures_by_family(tl)
    perm = out[out["ERROR_FAMILY"] == "Permission"].iloc[0]
    assert perm["DISTINCT_TASKS"] == 3 and bool(perm["SYSTEMIC"]) is True
    timeout = out[out["ERROR_FAMILY"] == "Timeout"].iloc[0]
    assert bool(timeout["SYSTEMIC"]) is False           # only 1 task


def test_systemic_counts_distinct_tasks_not_rows():
    # same task failing 5x with one family is NOT systemic (1 distinct task).
    tl = _timeline([("Permission", "T1")] * 5)
    assert bool(cluster_failures_by_family(tl).iloc[0]["SYSTEMIC"]) is False


def test_no_error_text_is_never_systemic():
    tl = _timeline([("No error text", f"T{i}") for i in range(6)])
    out = cluster_failures_by_family(tl)
    assert out.iloc[0]["DISTINCT_TASKS"] == 6 and bool(out.iloc[0]["SYSTEMIC"]) is False


def test_systemic_composite_key_across_schemas():
    # same TASK_NAME in two schemas = two distinct tasks.
    tl = pd.DataFrame([
        {"ERROR_FAMILY": "Perm", "DATABASE_NAME": "D", "SCHEMA_NAME": "A", "TASK_NAME": "T",
         "ERROR_MESSAGE": "x"},
        {"ERROR_FAMILY": "Perm", "DATABASE_NAME": "D", "SCHEMA_NAME": "B", "TASK_NAME": "T",
         "ERROR_MESSAGE": "x"},
        {"ERROR_FAMILY": "Perm", "DATABASE_NAME": "D", "SCHEMA_NAME": "C", "TASK_NAME": "T",
         "ERROR_MESSAGE": "x"},
    ])
    assert cluster_failures_by_family(tl).iloc[0]["DISTINCT_TASKS"] == 3
    assert cluster_failures_by_family(pd.DataFrame()).empty


# ================================================== #15 duration drift ======

def test_duration_drift_flags_a_task_3x_slower():
    rows = [{"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D", "TASK_NAME": "LOAD",
             "AVG_SEC": 60.0, "RUNS": 5} for d in range(1, 13)]
    rows.append({"DAY": dt.date(2026, 8, 13), "DATABASE_NAME": "D", "TASK_NAME": "LOAD",
                 "AVG_SEC": 300.0, "RUNS": 5})
    out = task_duration_anomalies(pd.DataFrame(rows))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["TASK_NAME"] == "LOAD" and r["SLOWER_X"] == 5.0     # 300 / 60
    assert r["BASELINE_SEC"] == 60.0 and r["Z_SCORE"] > 0


def test_duration_drift_ignores_trivially_short_tasks():
    # median ~2s (< DURATION_MIN_SEC) -> no flag however large the ratio.
    rows = [{"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D", "TASK_NAME": "TINY",
             "AVG_SEC": 2.0, "RUNS": 1} for d in range(1, 13)]
    rows.append({"DAY": dt.date(2026, 8, 13), "DATABASE_NAME": "D", "TASK_NAME": "TINY",
                 "AVG_SEC": 20.0, "RUNS": 1})
    assert task_duration_anomalies(pd.DataFrame(rows)).empty


def test_duration_drift_empty_and_missing_columns():
    assert task_duration_anomalies(pd.DataFrame()).empty
    assert task_duration_anomalies(pd.DataFrame({"X": [1]})).empty
    # steady task -> no drift
    steady = pd.DataFrame([{"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D",
                            "TASK_NAME": "S", "AVG_SEC": 100.0, "RUNS": 3} for d in range(1, 13)])
    assert task_duration_anomalies(steady).empty


def test_duration_drift_separates_same_task_across_schemas():
    # a task name reused across schemas must NOT interleave into one series: only the
    # PROD copy drifts; STG stays steady and is keyed separately despite the same name.
    rows = []
    for d in range(1, 13):
        rows.append({"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D", "SCHEMA_NAME": "PROD",
                     "TASK_NAME": "LOAD", "AVG_SEC": 60.0, "RUNS": 5})
        rows.append({"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D", "SCHEMA_NAME": "STG",
                     "TASK_NAME": "LOAD", "AVG_SEC": 60.0, "RUNS": 5})
    rows.append({"DAY": dt.date(2026, 8, 13), "DATABASE_NAME": "D", "SCHEMA_NAME": "PROD",
                 "TASK_NAME": "LOAD", "AVG_SEC": 300.0, "RUNS": 5})
    rows.append({"DAY": dt.date(2026, 8, 13), "DATABASE_NAME": "D", "SCHEMA_NAME": "STG",
                 "TASK_NAME": "LOAD", "AVG_SEC": 60.0, "RUNS": 5})
    out = task_duration_anomalies(pd.DataFrame(rows))
    assert list(out["SCHEMA_NAME"]) == ["PROD"] and out.iloc[0]["SLOWER_X"] == 5.0


def test_duration_drift_dedupes_duplicate_day_rows():
    # 6 distinct days duplicated into 12 rows must NOT reach the >=7-active-day gate
    base = [{"DAY": dt.date(2026, 8, d), "DATABASE_NAME": "D", "TASK_NAME": "LOAD",
             "AVG_SEC": (60.0 if d < 6 else 300.0), "RUNS": 5} for d in range(1, 7)]
    assert task_duration_anomalies(pd.DataFrame(base + base)).empty


# ================================================== #6 clustering churn ======

def test_zero_tb_churn_sorts_first_and_flags():
    clu = pd.DataFrame({"TABLE_FQN": ["GOOD", "CHURN"], "CREDITS": [10.0, 50.0],
                        "TB_RECLUSTERED": [5.0, 0.0], "RECLUSTER_RUNS": [2, 3]})
    out = flag_clustering_churn(clu, rate=3.0)
    assert out.iloc[0]["TABLE_FQN"] == "CHURN"          # paying 50 credits, reclustering 0 TB
    assert bool(out.iloc[0]["CHURNY"]) is True
    assert out.iloc[0]["SPEND_USD"] == 150.0            # 50 * 3
    assert out.iloc[0]["RECOVERABLE_USD"] == 150.0      # #31: churny spend is recoverable
    good = out[out["TABLE_FQN"] == "GOOD"].iloc[0]
    assert bool(good["CHURNY"]) is False and good["CREDITS_PER_TB_RECLUSTERED"] == 2.0
    assert good["RECOVERABLE_USD"] == 0.0               # non-churny -> nothing to recover


def test_suspend_recluster_candidate_and_wiring():
    # #31: generated (not executed) ALTER for a churny table, qualified name safe
    sql = suspend_recluster_sql("DB.SCH.MY_TABLE")
    assert sql.startswith("ALTER TABLE ") and sql.endswith("SUSPEND RECLUSTER;")
    assert "MY_TABLE" in sql
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "cost_parts"
           / "optimize.py").read_text(encoding="utf-8")
    assert "suspend_recluster_sql(" in src and "RECOVERABLE_USD" in src


def test_clustering_churn_min_credits_gate():
    # a tiny-credit zero-TB table is immaterial -> not churny.
    clu = pd.DataFrame({"TABLE_FQN": ["TINY"], "CREDITS": [0.2], "TB_RECLUSTERED": [0.0],
                        "RECLUSTER_RUNS": [1]})
    assert bool(flag_clustering_churn(clu, rate=3.0).iloc[0]["CHURNY"]) is False
    assert flag_clustering_churn(pd.DataFrame()).empty


# ================================================== #11 budget pace ==========

def test_budget_pace_variance_signed():
    # day 10 of 31: target measured over COMPLETED days (day-1=9) to match the
    # today-excluded MTD actual -> expected = 3000*9/31 = 870.97 (not 10/31).
    var, exp = budget_pace_variance(600.0, 3000.0, dt.date(2026, 8, 10))
    assert round(exp, 2) == 870.97
    assert round(var, 2) == round(600.0 - 870.97, 2)      # behind pace (negative)
    ahead, _ = budget_pace_variance(1500.0, 3000.0, dt.date(2026, 8, 10))
    assert ahead > 0                                       # ahead of the flat target


def test_budget_pace_no_budget_is_zero():
    assert budget_pace_variance(500.0, 0.0, dt.date(2026, 8, 10)) == (0.0, 0.0)
    assert budget_pace_variance(500.0, None, dt.date(2026, 8, 10)) == (0.0, 0.0)


def test_budget_burndown_cumulative_vs_straight_line():
    # August has 31 days; budget 3100 -> 100/day straight line. July rows excluded.
    daily = pd.DataFrame({"DAY": [dt.date(2026, 7, 31), dt.date(2026, 8, 1), dt.date(2026, 8, 2)],
                          "USD": [999.0, 100.0, 150.0]})
    out = budget_burndown(daily, 3100.0, dt.date(2026, 8, 10))
    assert list(out["DAY"].dt.date) == [dt.date(2026, 8, 1), dt.date(2026, 8, 2)]  # this month only
    assert list(out["CUM_ACTUAL_USD"]) == [100.0, 250.0]        # cumulative actual
    assert list(out["BUDGET_LINE_USD"]) == [100.0, 200.0]       # 3100 * day / 31
    assert budget_burndown(daily, 0.0, dt.date(2026, 8, 10)).empty          # no budget
    assert budget_burndown(pd.DataFrame(), 3100.0, dt.date(2026, 8, 10)).empty


# ================================================== #16 single-factor SQL ====

def test_single_factor_logins_shape():
    sql = security_sql.single_factor_logins(30, "ALFA")
    assert "FIRST_AUTHENTICATION_FACTOR = 'PASSWORD'" in sql
    assert "COALESCE(TO_VARCHAR(L.SECOND_AUTHENTICATION_FACTOR), '') = ''" in sql
    assert "L.IS_SUCCESS = 'YES'" in sql
    assert "COALESCE(U.HAS_MFA, FALSE) AS HAS_MFA" in sql        # separates enrolled bypass
    assert "COUNT(DISTINCT L.CLIENT_IP)" in sql
    assert "ORDER BY HAS_MFA DESC" in sql                        # enrolled-bypass floats up
    assert "ACCOUNT_USAGE.LOGIN_HISTORY" in sql and "ACCOUNT_USAGE.USERS" in sql
    assert "DATEADD('day', -30," in security_sql.single_factor_logins(days=9999)  # reader cap


def test_single_factor_company_scoped():
    # PERF #15: company scope is now the per-distinct-user membership subquery (UDF once per user).
    _sfa = security_sql.single_factor_logins(30, "ALFA")
    assert "L.USER_NAME IN (SELECT USER_NAME FROM" in _sfa
    assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in _sfa          # UDF once per distinct user
    assert "COMPANY_FOR_USER" not in security_sql.single_factor_logins(30, "ALL")


# ================================================== wiring locks =============

def test_easy_wins_wired():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "cluster_failures_by_family(timeline)" in ops and "Systemic errors" in ops
    assert "task_duration_anomalies(res.df)" in ops and "Duration drift" in ops
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    assert "flag_clustering_churn(clu.df, rate=rate)" in opt
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "single_factor_logins(" in sec and "Single-factor logins (MFA-bypassed" in sec
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    # PACE-MTD (round-16): the pace numerator is the COMPLETE-days MTD (today excluded),
    # not the today-inclusive mtd_spend, to match budget_pace_variance's completed denominator
    assert "budget_pace_variance(_mtd_complete, budget, account_today())" in ov
    assert "Pace vs budget calendar" in ov
    assert "budget_burndown(" in ov and "Budget burndown" in ov   # #57
