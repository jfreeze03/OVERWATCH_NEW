"""Locks for bug-hunt round 7 (wf_a1e5de10-831): uncapped-aggregate sweep + export fidelity.

Six defects — task-health live-fallback fail-rate, auditor-pack clock, Control Room replay
counts (the deferred item), unmapped billed-blind $0, Overview export sparkline coverage, and
the wasted-spend repeat-offenders count — all pinned so a regression re-fails here.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql, mart_sql, ops_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_task_runs_carries_uncapped_window_totals():
    # The live fallback is LIMIT 200 by FAILED DESC; the KPI must read pre-LIMIT window sums,
    # not sum the display frame (which evicts healthy high-volume tasks -> inflated fail rate).
    sql = ops_sql.task_runs(7, "ALFA")
    assert "SUM(COUNT(*)) OVER () AS TOTAL_RUNS_WIN" in sql
    assert "SUM(SUM(IFF(STATE = 'FAILED', 1, 0))) OVER () AS TOTAL_FAILED_WIN" in sql
    src = _read("app/ui/pages/operations.py")
    assert 'df["TOTAL_RUNS_WIN"].iloc[0]' in src
    # helper columns dropped before the task-health table renders
    assert '"TOTAL_RUNS_WIN", "TOTAL_FAILED_WIN"' in src


def test_auditor_pack_stamps_account_time_not_wall_clock():
    src = _read("app/ui/pages/security.py")
    assert "from app.logic.formulas import account_now" in src
    assert 'stamp = account_now().strftime("%Y%m%d_%H%M")' in src
    # the UTC wall clock is gone from the pack
    assert "datetime.now()" not in src
    assert "from datetime import datetime" not in src


def test_control_room_replay_counts_use_uncapped_totals():
    # the deferred item: task-failures / DDL / grants headlines must read pre-LIMIT window
    # totals, not len()/sum() over the LIMIT-capped display feeds.
    assert "SUM(FAILED) OVER () AS TOTAL_FAILED_WIN" in mart_sql.day_task_failures("2026-01-02")
    assert "COUNT(*) OVER () AS TOTAL_DDL_WIN" in security_sql.day_ddl("2026-01-02")
    assert "COUNT(*) OVER () AS TOTAL_GRANTS_WIN" in security_sql.day_grants("2026-01-02")
    src = _read("app/ui/pages/control_room.py")
    assert '_replay_total(ddl, "TOTAL_DDL_WIN"' in src
    assert '_replay_total(grants, "TOTAL_GRANTS_WIN"' in src
    assert '_replay_total(tasks, "TOTAL_FAILED_WIN"' in src
    # detail tables render the helper-free copies
    assert "styled_table(_tasks_disp" in src
    assert "with_user_names(_ddl_disp" in src
    assert "with_user_names(_grants_disp" in src


def test_unmapped_entities_billed_blind_reads_uncapped_total():
    # WAREHOUSE (the only credit grain) sorts last, so >300 DB+USER rows evict it past LIMIT 300;
    # the "$0 billed blind" false-negative is fixed by reading a pre-LIMIT credit total.
    sql = mart_sql.unmapped_entities(7)
    assert "COUNT(*) OVER () AS TOTAL_ENTITIES_WIN" in sql
    assert "SUM(CASE WHEN MEASURE = 'credits' THEN VALUE ELSE 0 END) OVER () AS TOTAL_CREDITS_WIN" in sql
    assert "COMPANY = 'UNKNOWN'" in sql and "ACCOUNT_USAGE" not in sql  # still mart-only
    src = _read("app/ui/pages/cost.py")
    assert 'safe_float(_disp["TOTAL_CREDITS_WIN"].iloc[0]) * float(rate)' in src
    assert '_disp["TOTAL_ENTITIES_WIN"].iloc[0]' in src


def test_overview_export_sparkline_spans_labeled_window():
    # the exec-summary HTML labels "last {days}d" — the series must span the window, not tail(30).
    src = _read("app/ui/pages/overview.py")
    assert "daily[_uc].tail(30)" not in src
    assert "_export_spark = [safe_float(v) for v in daily[_uc].tolist()]" in src


def test_repeat_offenders_count_is_uncapped():
    # count fingerprints failed 5+ times over the whole window, not the top-50-by-$ display frame.
    sql = insights_sql.wasted_query_spend_usd(30, "ALFA")
    assert "SUM(IFF(COUNT(DISTINCT a.QUERY_ID) >= 5, 1, 0)) OVER () AS REPEAT_OFFENDERS_WIN" in sql
    src = _read("app/ui/pages/operations.py")
    assert 'wdf["REPEAT_OFFENDERS_WIN"].iloc[0]' in src
