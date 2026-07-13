"""V045 locks — the owner's correction, both halves.

2026-07-13: "i messed up. i meant getting rid of resource monitor, not task
monitoring. we need to add that back. that's my fault."
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V045__task_monitoring_restored.sql").read_text(encoding="utf-8")


def test_v045_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v045.py")],
                       env={**os.environ, "V045_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _MIG, (
        "V045 drifted from its forward-generation — edit outputs/gen_v045.py, "
        "regenerate, never hand-edit the migration.")


def test_v045_restores_the_loader_and_keeps_the_keepers():
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY" in _MIG
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY" in _MIG
    scan = _MIG.split("-- >>> derived:SP_ALERT_SCAN", 1)[1].split("-- >>> derived:MART_SOURCE_FRESHNESS", 1)[0]
    assert "-- [06] PIPE_TASK_FAILURES" in scan                    # back
    assert "-- [18] SEC_NEW_ADMIN_NETWORK" in scan                 # kept (r25 teeth)
    assert "-- [19] COST_EGRESS_SPIKE" in scan                     # kept
    assert "(19 - :fails) || '/19 rule blocks ok'" in scan
    board = _MIG.split("-- >>> derived:SP_REFRESH_EXEC_BOARD", 1)[1].split("-- >>> derived:", 1)[0]
    assert "UNION ALL SELECT 'UNKNOWN'" in board                   # kept (V044)
    assert "SET ENABLED = TRUE\n WHERE RULE_ID = 'PIPE_TASK_FAILURES'" in _MIG
    assert "DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;" in _MIG # the actual target
    assert "SET RESOURCE_MONITOR = NULL" in _MIG
    assert "DATEADD('day', -120, CURRENT_DATE())" in _MIG          # refill window


def test_app_task_surfaces_are_back():
    from app.data import graph_sql, insights_sql, mart_sql, ops_sql
    assert "TASK_HISTORY" in ops_sql.task_runs(7, "ALFA")
    assert "SCHEDULED_TIME" in insights_sql.task_failure_details(7, "ALFA")
    assert "FACT_TASK_DAILY" in mart_sql.fact_task_daily(2, "ALFA")
    assert "SERVERLESS_TASK_HISTORY" in graph_sql.serverless_task_daily(7)
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert '"Tasks", "Task graphs ($)"' in ops
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    assert '"key": "tf"' in cr                                     # day-replay task frame
    import inspect

    from app.logic.actions import triage_queue
    assert "task_failures" in str(inspect.signature(triage_queue))


def test_resource_monitor_references_are_gone_from_the_app():
    gov = (_ROOT / "app" / "logic" / "governance.py").read_text(encoding="utf-8")
    assert "GOV_PTS_NO_MONITOR" not in gov
    src = (_ROOT / "app" / "logic" / "remediation.py").read_text(encoding="utf-8")
    assert "def attach_resource_monitor" not in src
    assert "def resource_monitor_quota" not in src
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "Attach resource monitor" not in adm
    assert "Budget ↔ resource-monitor sync" not in adm
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "warehouses_no_monitor" not in sec                      # autosuspend tracking stays
    assert "warehouses_no_autosuspend" in sec
