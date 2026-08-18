"""Locks for V058 — per-node loader-timing observability (perf T3).

Additive: a new MART_TASK_NODE_DAILY table + a new contained arm [6b] in
SP_LOAD_MARTS_V27 (re-derived from V057). No task-graph change. The arm captures
the SCHEDULED_TIME->QUERY_START_TIME queue delay that the pipeline-grain arm [6]
discards, so the deferred reconcile-scheduling work can be sized from data.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V58 = (_MIG / "V058__task_node_timing.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v058_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v058.py")],
                       env={**os.environ, "V058_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V58, (
        "V058 drifted from outputs/gen_v058.py — regenerate, never hand-edit.")


def test_v058_guard_table_and_shape():
    assert "EXCEPTION (-20058" in _V58 and "IF (v < 57) THEN" in _V58 and "SELECT 58 AS VERSION" in _V58
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY" in _V58
    # exactly one procedure STATEMENT is created (header prose may mention the phrase)
    assert _V58.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27") == 1
    assert "-- >>> derived:SP_LOAD_MARTS_V27" in _V58


def test_v058_makes_no_task_graph_change():
    """The whole safety premise: additive/observability only — no SCHEDULE,
    no AFTER edge, no SUSPEND/RESUME, no CREATE/ALTER TASK anywhere."""
    for banned in ("CREATE TASK", "ALTER TASK", "CREATE OR REPLACE TASK",
                   "EXECUTE TASK", "TASK_DEPENDENTS_ENABLE", "SET SCHEDULE"):
        assert banned not in _V58, banned
    # 'SUSPEND' must appear only as the warehouse column AUTO_SUSPEND (posture arm)
    # and in the header comment prose — never as a task SUSPEND statement.
    assert "SUSPEND TASK" not in _V58 and "SUSPEND DBA_MAINT" not in _V58


def test_v058_preserves_v057_fail_fix():
    """CRITICAL: V058 re-derives SP_LOAD_MARTS_V27 from V057, so the four
    EXECUTION_STATUS='FAIL' tokens survive and the dead 'FAILED' never returns."""
    marts = _proc(_V58, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4
    assert "EXECUTION_STATUS = 'FAILED'" not in marts
    assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts   # signature pinned


def test_v058_new_arm_is_contained_and_idempotent():
    """The per-node write is its OWN guarded arm, keyed for idempotency, and does
    not disturb the existing task-graph arm [6]."""
    marts = _proc(_V58, "SP_LOAD_MARTS_V27")
    assert marts.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY") == 1
    assert marts.count("loaded := loaded || 'task_node ';") == 1
    assert marts.count("MART_TASK_NODE_DAILY - other marts unaffected") == 1   # own EXCEPTION
    # idempotent MERGE key = (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME)
    assert "ON t.DAY = s.DAY AND t.TASK_NAME = s.TASK_NAME" in marts
    # the SCHEDULED_TIME queue delay — the signal arm [6] discards
    assert "DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME)" in marts
    # arm [6] (task-graph) is untouched
    assert marts.count("loaded := loaded || 'graphs ';") == 1


def test_v058_is_derived_v057_plus_only_the_new_arm():
    """V058's proc == V057's proc with EXACTLY the new arm inserted and nothing
    else changed (no existing statement edited)."""
    v57 = _proc((_MIG / "V057__fail_status_token.sql").read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27")
    v58 = _proc(_V58, "SP_LOAD_MARTS_V27")
    # removing the new arm block must recover V057 byte-for-byte
    stripped = re.sub(r"\n        -- \[6b\] per-node task timing.*?loaded := loaded \|\| 'task_node ';\n"
                      r"        EXCEPTION\n            WHEN OTHER THEN\n                emsg := SQLERRM;\n"
                      r"                INSERT INTO DBA_MAINT_DB\.OVERWATCH\.APP_ERROR_LOG[^\n]*\n"
                      r"                SELECT 'MartLoader', 'mart_load_failed', :emsg, "
                      r"'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE\(\);\n        END;\n",
                      "", v58, count=1, flags=re.S)
    assert stripped == v57, "V058 changed something other than inserting the new arm"


def test_v058_teardown_and_admin_entry():
    """V058's own durable lockstep artifacts (the moving validate.sql floor is
    asserted generically in test_v451_trust, not pinned per-migration)."""
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY;" in _read("snowflake/teardown.sql")
    assert "58:" in _read("app/ui/pages/admin.py")


def test_v058_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V58):
        sqlglot.parse(stmt, dialect="snowflake")
