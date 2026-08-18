"""Locks for V057 — silent-0 failure-count fix (EXECUTION_STATUS 'FAILED' -> 'FAIL').

Four HOURLY arms of SP_LOAD_MARTS_V27 counted failures with the token 'FAILED',
which QUERY_HISTORY/OW_QH_EXTRACT never store (the value is 'FAIL'), so the FAILS
column was a constant 0. One proc re-derived from V056; the task-graph arm's
TASK_HISTORY STATE='FAILED' predicates (genuinely 'FAILED') are preserved.
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
_V57 = (_MIG / "V057__fail_status_token.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v057_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v057.py")],
                       env={**os.environ, "V057_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V57, (
        "V057 drifted from outputs/gen_v057.py — regenerate, never hand-edit.")


def test_v057_guard_and_shape():
    assert "EXCEPTION (-20057" in _V57 and "IF (v < 56) THEN" in _V57 and "SELECT 57 AS VERSION" in _V57
    assert _V57.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "-- >>> derived:SP_LOAD_MARTS_V27" in _V57
    assert "CREATE TABLE" not in _V57 and "CREATE TASK" not in _V57          # no new objects


def test_v057_fails_predicates_use_fail_not_failed():
    """The four query-failure arms now count EXECUTION_STATUS = 'FAIL'; the token
    'FAILED' never appears in an EXECUTION_STATUS predicate in the executable proc."""
    marts = _proc(_V57, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4
    assert "EXECUTION_STATUS = 'FAILED'" not in marts                        # the bug is gone


def test_v057_preserves_task_state_failed():
    """TASK_HISTORY.STATE genuinely takes 'FAILED' — the task-graph arm's four
    state predicates must be untouched by the EXECUTION_STATUS edit."""
    marts = _proc(_V57, "SP_LOAD_MARTS_V27")
    assert marts.count("STATE = 'FAILED'") == 2
    assert marts.count("STATE IN ('SUCCEEDED', 'FAILED')") == 2


def test_v057_only_execution_status_edit_vs_v056():
    """V057 is a pure token fix: the derived proc equals V056's proc with exactly
    the four 'FAILED'->'FAIL' EXECUTION_STATUS swaps and nothing else."""
    v56 = (_MIG / "V056__loader_reconcile_alert_fixes.sql").read_text(encoding="utf-8")
    marts56 = _proc(v56, "SP_LOAD_MARTS_V27")
    marts57 = _proc(_V57, "SP_LOAD_MARTS_V27")
    rederived = marts56.replace("COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,",
                                "COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,")
    assert marts57 == rederived


def test_v057_app_change_impact_no_longer_uses_failed():
    """The paired app instance (warehouse_daily_series, live QUERY_HISTORY) is
    fixed too; no app builder may compare EXECUTION_STATUS to the dead token."""
    ci = _read("app/data/change_impact_sql.py")
    assert "COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS" in ci
    assert "EXECUTION_STATUS = 'FAILED'" not in ci


def test_no_app_builder_uses_the_dead_failed_token():
    """Regression guard: EXECUTION_STATUS = 'FAILED' never matches Snowflake's
    stored value and silently reads 0 — it must not reappear anywhere in app/."""
    offenders = [f.relative_to(_ROOT).as_posix() for f in (_ROOT / "app").rglob("*.py")
                 if "EXECUTION_STATUS = 'FAILED'" in f.read_text(encoding="utf-8")]
    assert not offenders, f"dead 'FAILED' token in: {offenders} (use 'FAIL' or <> 'SUCCESS')"


def test_v057_no_new_teardown_needed():
    td = _read("snowflake/teardown.sql")
    assert "SP_LOAD_MARTS_V27" in td                                          # pre-exists


def test_v057_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V57):
        sqlglot.parse(stmt, dialect="snowflake")
