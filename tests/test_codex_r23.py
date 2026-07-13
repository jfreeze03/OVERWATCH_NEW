"""Locks for the r23 fix-batch (v4.38.0) — the fleet board's picks, app-only.

Targets came from the 2026-07-12 post-rebuild pain board: t_rca 32.9s,
c_pressure 17.8s, chg_hist 6.1s, and the Security changes tab's serial
singles. No migration in this round, by design (fresh rebuild settles).
"""

from __future__ import annotations

from pathlib import Path

from app.data import change_impact_sql, insights_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_pressure_panel_is_fact_first_with_the_live_contract():
    sql = mart_sql.fact_warehouse_pressure(7, "ALFA")
    for col in ("WAREHOUSE_NAME", "QUERY_COUNT", "QUEUED_SEC", "SPILL_REMOTE_GB",
                "P95_ELAPSED_SEC"):
        assert col in sql, col
    assert "FACT_QUERY_HOURLY" in sql
    assert "TOTAL_ELAPSED_TIME" not in sql                 # old source column absent
    assert "QUERY_HISTORY" not in sql
    assert "MAX(COALESCE(P95_ELAPSED_SEC, 0))" in sql      # peak hourly, labeled by caller
    # same visibility floor as live: only warehouses with pressure show
    assert "HAVING SUM(COALESCE(QUEUED_SEC_SUM, 0)) > 0" in sql
    assert "COMPANY = 'ALFA'" in sql
    assert "COMPANY" not in mart_sql.fact_warehouse_pressure(7, "ALL").split("FROM")[0]
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    body = ops.split("def _contention_tab", 1)[1].split("\ndef ", 1)[0]
    assert "mart_sql.fact_warehouse_pressure" in body
    assert "ops_sql.warehouse_pressure" in body            # live fallback stays
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart_sql.fact_warehouse_pressure" in canary


def test_change_drill_prefilters_before_normalizing():
    sql = change_impact_sql.object_run_history("PROCEDURE", "DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN", 28)
    # the V031 scan-v2 trick: cheap ILIKE rides in front of the POSITION pass
    assert sql.count("QUERY_TEXT ILIKE '%SP_ALERT_SCAN%'") == 1
    assert sql.count("POSITION('SP_ALERT_SCAN('") == 1     # exact match still decides
    assert sql.index("ILIKE") < sql.index("POSITION")
    # the TASK branch has an exact FQN equality — no text pass to prefilter
    tsql = change_impact_sql.object_run_history("TASK", "DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY", 28)
    assert "ILIKE" not in tsql


def test_task_failure_details_prunes_on_scheduled_time():
    sql = insights_sql.task_failure_details(7, "ALFA")
    # TASK_HISTORY prunes on SCHEDULED_TIME (V031 precedent) — the RCA read
    # sat at 33s scanning the whole view without it
    assert "SCHEDULED_TIME >= DATEADD('day', -8, CURRENT_DATE())" in sql
    assert "QUERY_START_TIME >= DATEADD('day', -7, CURRENT_DATE())" in sql
    assert "STATE = 'FAILED'" in sql                       # semantics unchanged


def test_security_changes_tab_batches_its_two_live_reads():
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    body = sec.split("def _changes_tab", 1)[1].split("\ndef ", 1)[0]
    assert 'run_batch([' in body
    assert '{"key": "ddl"' in body and '{"key": "login_reasons"' in body
    # serial fallbacks keep their own cache keys, per the Access-tab pattern
    assert '_cb.get("ddl") or run(' in body
    assert '_cb.get("login_reasons") or run(' in body
    assert ") or {}" not in body                           # the r8 trust lock holds
