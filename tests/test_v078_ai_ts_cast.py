"""V078 locks: the ai_code arm's TZ->NTZ casts, and nothing else, change.

Live failure this fixes (owner backfill 2026-08-13): the ai_code arm of
SP_LOAD_MARTS_V27 died on every run with "expecting TIMESTAMP_NTZ(9) but got
TIMESTAMP_TZ(9) for column FIRST_TS" because CORTEX_CODE_* views expose
USAGE_TIME as TIMESTAMP_TZ. FACT_AI_USAGE_DAILY therefore never gained
SOURCE<>'Functions' rows, the app's AI coverage gate never passed, and the
AI-users panel paid a ~20s live scan per render."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V078 = (_MIG / "V078__ai_usage_ts_cast.sql").read_text(encoding="utf-8")


def _proc(text: str) -> str:
    return re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_MARTS_V27\(.*?\n\$\$;\n",
        text,
        re.S,
    ).group(0)


def test_v078_regenerates_byte_identical(tmp_path):
    # Derivation law: re-running the generator over V066 reproduces V078 exactly.
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v078.py")],
        env={**os.environ, "V078_OUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V078


def test_v078_is_one_guarded_proc_swap_plus_first_fill():
    assert "EXCEPTION (-20078" in _V078 and "IF (v < 77) THEN" in _V078
    assert "SELECT 78 AS VERSION" in _V078 and "WHERE VERSION = 78)" in _V078
    assert _V078.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V078 and "CREATE TASK" not in _V078
    # No infra provisioning. (The proc legitimately READS the RESOURCE_MONITOR
    # column in its warehouse-config snapshot arm, so match the DDL forms.)
    assert "CREATE WAREHOUSE" not in _V078
    assert "CREATE RESOURCE MONITOR" not in _V078
    # The first-fill heals the mart depth in the same apply (the broken arm
    # never landed code-view rows, so the coverage gate needs the backfill).
    assert _V078.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);") == 1


def test_v078_casts_exactly_the_two_ai_code_timestamps():
    proc = _proc(_V078)
    assert "MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS" in proc
    assert "MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS" in proc
    # The uncast form must be gone...
    assert "MIN(c.USAGE_TIME) AS FIRST_TS" not in proc
    assert "MAX(c.USAGE_TIME) AS LAST_TS" not in proc
    # ...while the ai_functions arm (TIMESTAMP_LTZ source, coerces fine) keeps
    # its uncast aggregates.
    assert "MIN(START_TIME) AS FIRST_TS" in proc
    assert "MAX(START_TIME) AS LAST_TS" in proc


def test_v078_leaves_every_other_loader_arm_untouched():
    previous = _proc(
        (_MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql")
        .read_text(encoding="utf-8")
    )
    current = _proc(_V078)
    for untouched in (
        "MART_TASK_NODE_DAILY - other marts unaffected",
        "MART_SECURITY_POSTURE_DAILY - other marts unaffected",
        "FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected",
        "SP_LOAD_MARTS_V27: SCOPE must be HOURLY or DAILY",
    ):
        assert untouched in previous and untouched in current
    # Beyond the one edited block (comment + two casts), the derived proc is the
    # V066 proc: reversing the edit reproduces it byte-for-byte.
    reverted = current.replace(
        "                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact\n"
        "                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ\n"
        "                       -- (live 2026-08-13: \"expecting TIMESTAMP_NTZ(9) but got\n"
        "                       -- TIMESTAMP_TZ(9) for column FIRST_TS\" killed this arm on\n"
        "                       -- every run, starving the AI coverage gate).\n"
        "                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,\n"
        "                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,",
        "                       MIN(c.USAGE_TIME) AS FIRST_TS,\n"
        "                       MAX(c.USAGE_TIME) AS LAST_TS,",
    )
    assert reverted == previous


def test_v078_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements

    for statement in _plain_statements(_V078):
        sqlglot.parse(statement, dialect="snowflake")
