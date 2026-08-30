"""Operations-layer bug-hunt #2 locks (2026-08-30, v4.356.0).

Second, deeper adversarial pass (7 finders). Six confirmed findings; three refuted (pipeline-SLA
cadence misread TABLE_DML_HISTORY granularity, version-diff truncation unreachable under the
1000-task DAG cap, company-scoped cache-hit is the V044 UNKNOWN law).
  - [HIGH] V103: warehouse-efficiency mart ACTIVE_HOURS start-hour -> span-based (idle % overstated
    for multi-hour queries flipped busy warehouses KEEP->SUSPEND on the mart-first sizing panel).
  - [MED] blast-radius warning understated the affected user/query totals (LIMIT 25 display frame).
  - [MED] task-run 'dispatch delay' used the SUMMED per-task queue (over-fires on wide graphs); now
    the run-level MAX per-task queue.
  - [MED] release 'task regressions' greened on an empty AFTER window; now marked undecided.
  - [LOW] proc_regression LIMIT 200 by signed p95-delta dropped 'faster but failing' procs.
  - [LOW] clustering 'recoverable' figure covers >=30d but was labeled with the ambiguous '/window'.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import ops_sql
from app.logic.insights import task_release_deltas

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- HIGH / V103: warehouse-efficiency ACTIVE_HOURS is span-based ------------------
def test_v103_makes_wh_eff_active_hours_span_based_and_is_guarded():
    mig = _src("snowflake/migrations/V103__wh_efficiency_active_hours_span.sql")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in mig
    # span expansion (bounded to 25) replaces the start-hour count
    assert "qh AS (" in mig and "q_active AS (" in mig
    assert "COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS" in mig
    assert "GENERATOR(ROWCOUNT => 25)" in mig
    assert "COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) AS ACTIVE_HOURS" not in mig  # old form gone
    assert "COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS" in mig
    # upstream fixes preserved
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME)" in mig  # V095
    assert mig.count("QUALIFY ROW_NUMBER() OVER (") == 3                # V095 + V102's two
    assert "EXCEPTION (-20103" in mig and "IF (v < 102) THEN" in mig
    assert "SELECT 103 AS VERSION" in mig
    assert "CREATE TABLE" not in mig and "ALTER TABLE" not in mig
    sqlglot.parse(mig, dialect="snowflake")


# ---- MED: blast-radius counts the full radius, not the LIMIT 25 display frame ------
def test_blast_radius_carries_untruncated_window_totals():
    sql = ops_sql.warehouse_blast_radius("WH_TEST", 7)
    assert "COUNT(*) OVER ()" in sql and "SUM(COUNT(*)) OVER ()" in sql
    assert "AS TOTAL_USERS" in sql and "AS TOTAL_QUERIES" in sql
    assert "LIMIT 25" in sql
    sqlglot.parse(sql, dialect="snowflake")
    comp = _src("app/ui/components.py")
    assert 'int(df["TOTAL_USERS"].iloc[0])' in comp
    assert 'int(df["TOTAL_QUERIES"].iloc[0])' in comp
    # the headline no longer sums the truncated display frame
    assert "ran {int(df['QUERIES'].sum()):,} queries" not in comp


# ---- MED: task-run dispatch delay uses the run-level MAX per-task queue -------------
def test_task_graph_recent_runs_exposes_max_queue_and_ui_uses_it():
    sql = ops_sql.task_graph_recent_runs("0", 7, 40)
    assert "AS MAX_QUEUE_SEC" in sql
    assert "MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME," in sql
    sqlglot.parse(sql, dialect="snowflake")
    ops = _src("app/ui/pages/operations.py")
    assert 'run_row.get("MAX_QUEUE_SEC", run_row.get("QUEUE_SEC"))' in ops
    assert '"label": "Max task queue"' in ops


# ---- MED: release task-regressions marks empty-AFTER tasks undecided ----------------
def _rel_row(db, sch, task, period, runs, failed, avg):
    return {"DATABASE_NAME": db, "SCHEMA_NAME": sch, "TASK_NAME": task,
            "PERIOD": period, "RUNS": runs, "FAILED": failed, "AVG_SEC": avg}


def test_task_release_deltas_marks_no_after_runs_undecided():
    df = pd.DataFrame([
        # A: runs on both sides, stable -> decidable, not worse
        _rel_row("DB", "S", "A", "BEFORE", 10, 0, 100), _rel_row("DB", "S", "A", "AFTER", 10, 0, 100),
        # B: BEFORE only, no AFTER runs yet -> UNDECIDED, not a clean pass
        _rel_row("DB", "S", "B", "BEFORE", 10, 0, 100),
        # C: gained failures after -> decidable AND worse
        _rel_row("DB", "S", "C", "BEFORE", 10, 0, 100), _rel_row("DB", "S", "C", "AFTER", 10, 3, 100),
    ])
    out = task_release_deltas(df).set_index("TASK_NAME")
    assert bool(out.loc["A", "DECIDABLE"]) and not bool(out.loc["A", "GOT_WORSE"])
    assert not bool(out.loc["B", "DECIDABLE"]) and not bool(out.loc["B", "GOT_WORSE"])  # unknown != clean
    assert bool(out.loc["C", "DECIDABLE"]) and bool(out.loc["C", "GOT_WORSE"])


def test_task_release_deltas_all_before_only_is_not_decidable():
    # every task has only BEFORE runs -> the panel must show 'no data yet', not a green all-clear
    df = pd.DataFrame([
        _rel_row("DB", "S", "A", "BEFORE", 10, 0, 100),
        _rel_row("DB", "S", "B", "BEFORE", 5, 0, 50),
    ])
    out = task_release_deltas(df)
    assert not bool(out["DECIDABLE"].any())
    assert out["GOT_WORSE"].sum() == 0
    # the UI gates the clean banner on DECIDABLE.any()
    ops = _src("app/ui/pages/operations.py")
    assert 'bool(deltas["DECIDABLE"].any())' in ops
    assert '"no_data_yet"' in ops.split("def _release_compare_tab", 1)[1].split("\ndef ", 1)[0]


# ---- LOW: proc_regression keeps the 'faster but failing' class before the LIMIT -----
def test_proc_regression_ranks_by_stronger_of_slowdown_and_fail_jump():
    sql = ops_sql.proc_regression(14)
    assert ("ORDER BY GREATEST(COALESCE(P95_DELTA_PCT, 0),\n"
            "                  COALESCE(CUR_FAIL_PCT, 0) - COALESCE(PRIOR_FAIL_PCT, 0)) DESC NULLS LAST") in sql
    assert "ORDER BY P95_DELTA_PCT DESC NULLS LAST\nLIMIT 200" not in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- LOW: clustering 'recoverable' names its real span ------------------------------
def test_clustering_recoverable_discloses_its_span():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "recoverable over " in opt and "the last {max(days, 30)} days" in opt
    assert "/window recoverable" not in opt
