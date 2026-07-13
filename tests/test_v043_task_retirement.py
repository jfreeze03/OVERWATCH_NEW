"""V043 locks — task retirement finished loader-side + r25 alert teeth.

The derivation law, enforced: re-running the forward-generator must
reproduce the shipped migration byte-for-byte (origins: V041/V042/V023).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V043__task_retirement_alert_teeth.sql").read_text(encoding="utf-8")


def test_v043_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v043.py")],
                       env={**os.environ, "V043_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _MIG, (
        "V043 drifted from its forward-generation — edit outputs/gen_v043.py, "
        "regenerate, never hand-edit the migration.")


def test_v043_retires_every_loader_side_task_surface():
    # the ONLY task-table mentions left are the retire section itself
    body = _MIG.split("-- >>> retire", 1)[0]
    assert "FACT_TASK_DAILY" not in body
    assert "MART_TASK_GRAPH_DAILY" not in body
    assert "PIPE_TASK_FAILURES" not in body.split("-- >>> rules", 1)[0].replace(
        "PIPE_TASK_FAILURES (HIGH)", "")            # header prose only
    tail = _MIG.split("-- >>> retire", 1)[1]
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY;" in tail
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY;" in tail
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE WHERE KIND = 'TASK_FAIL';" in tail
    # drops come AFTER every proc swap and BEFORE the first fills
    assert _MIG.index("-- >>> derived:SP_ALERT_SCAN") < _MIG.index("-- >>> retire") < _MIG.index("-- >>> first fills")


def test_v043_alert_scan_is_18_arms_with_the_new_teeth():
    scan = _MIG.split("-- >>> derived:SP_ALERT_SCAN", 1)[1].split("-- >>> derived:MART_SOURCE_FRESHNESS", 1)[0]
    assert "-- [06] PIPE_TASK_FAILURES" not in scan
    assert "-- [18] SEC_NEW_ADMIN_NETWORK" in scan
    assert "-- [19] COST_EGRESS_SPIKE" in scan
    assert "(18 - :fails) || '/18 rule blocks ok'" in scan
    # both new arms carry the house dedupe shape
    assert scan.count("WHERE e.DEDUPE_KEY = b.DEDUPE_KEY") >= 2
    assert "'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'" in scan       # watches the real roles
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in scan     # 90d baseline
    assert "DATA_TRANSFER_HISTORY" in scan


def test_v043_rules_seeded_and_task_rule_disabled():
    rules = _MIG.split("-- >>> rules", 1)[1]
    assert "SET ENABLED = FALSE\n WHERE RULE_ID = 'PIPE_TASK_FAILURES'" in rules
    assert "'SEC_NEW_ADMIN_NETWORK', 'SECURITY'" in rules
    assert "'COST_EGRESS_SPIKE',     'COST'" in rules


def test_v043_board_and_score_keep_their_shapes_zero_filled():
    board = _MIG.split("-- >>> derived:SP_REFRESH_EXEC_BOARD", 1)[1].split("-- >>> derived:", 1)[0]
    assert "WHERE FALSE" in board and "'TASK_RUNS'" in board       # KPI arm intact, source empty
    score = _MIG.split("-- >>> derived:SP_LOAD_PLATFORM_SCORE", 1)[1].split("-- >>> derived:", 1)[0]
    assert "WHERE FALSE" in score and "TASK_RUNS" in score
