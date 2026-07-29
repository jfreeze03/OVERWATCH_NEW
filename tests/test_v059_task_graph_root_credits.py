"""Locks for V059 — task-graph pipeline credits fix (metrics-triage HIGH #2).

Arm [6] of SP_LOAD_MARTS_V27 now rolls QUERY_ATTRIBUTION_HISTORY up by
COALESCE(ROOT_QUERY_ID, QUERY_ID) and joins a.ROOT_ID = h.QUERY_ID, mirroring
app/data/graph_sql.graph_daily_costs (audit #10), so MART_TASK_GRAPH_DAILY.WH_CREDITS
captures proc-body child compute instead of reading ~0 for CALL-driven tasks.
Re-derived from V058; V057's FAIL fixes and V058's per-node arm [6b] preserved.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V59 = (_MIG / "V059__task_graph_root_credits.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v059_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v059.py")],
                       env={**os.environ, "V059_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V59, (
        "V059 drifted from outputs/gen_v059.py — regenerate, never hand-edit.")


def test_v059_guard_and_shape():
    assert "EXCEPTION (-20059" in _V59 and "IF (v < 58) THEN" in _V59 and "SELECT 59 AS VERSION" in _V59
    assert _V59.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27") == 1
    assert "-- >>> derived:SP_LOAD_MARTS_V27" in _V59
    assert "CREATE TABLE" not in _V59 and "CREATE TASK" not in _V59        # no new object


def test_v059_arm6_rolls_credits_by_root_query_id():
    """The task-graph credit rollup uses COALESCE(ROOT_QUERY_ID, QUERY_ID) on the
    SELECT, the prune filter, and the GROUP BY, and joins on ROOT_ID — matching
    graph_sql.graph_daily_costs so proc-body child compute is captured."""
    marts = _proc(_V59, "SP_LOAD_MARTS_V27")
    assert "SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE)" in marts
    assert "AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (" in marts
    assert "GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)" in marts
    assert "a ON a.ROOT_ID = h.QUERY_ID" in marts
    # the bug is gone
    assert "a ON a.QUERY_ID = h.QUERY_ID" not in marts
    assert "SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE)" not in marts


def test_v059_matches_live_graph_sql_rollup():
    """The mart arm and the live twin must use the SAME ROOT rollup + join so the
    default (mart) and fallback (live) paths agree on pipeline credits."""
    marts = _proc(_V59, "SP_LOAD_MARTS_V27")
    gsql = (_ROOT / "app" / "data" / "graph_sql.py").read_text(encoding="utf-8")
    for token in ("COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID",
                  "GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)",
                  "a ON a.ROOT_ID = h.QUERY_ID"):
        assert token in marts and token in gsql, token


def test_v059_preserves_v057_and_v058():
    """Re-derived from V058: V057's four FAIL fixes and V058's per-node arm [6b]
    (MART_TASK_NODE_DAILY) both survive; nothing else changed."""
    marts = _proc(_V59, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4
    assert "EXECUTION_STATUS = 'FAILED'" not in marts
    assert marts.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY") == 1
    assert marts.count("loaded := loaded || 'task_node ';") == 1
    assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts


def test_v059_is_derived_v058_plus_only_the_four_edits():
    """V059's proc == V058's proc with EXACTLY the four ROOT_QUERY_ID token edits
    and nothing else."""
    v58 = _proc((_MIG / "V058__task_node_timing.sql").read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27")
    v59 = _proc(_V59, "SP_LOAD_MARTS_V27")
    rederived = (v58
                 .replace("SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS",
                          "SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS")
                 .replace("AND QUERY_ID IN (", "AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (")
                 .replace("GROUP BY QUERY_ID", "GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)")
                 .replace("a ON a.QUERY_ID = h.QUERY_ID", "a ON a.ROOT_ID = h.QUERY_ID"))
    assert v59 == rederived, "V059 changed something beyond the four ROOT_QUERY_ID edits"


def test_v059_no_new_teardown_needed():
    assert "SP_LOAD_MARTS_V27" in (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")


def test_v059_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V59):
        sqlglot.parse(stmt, dialect="snowflake")
