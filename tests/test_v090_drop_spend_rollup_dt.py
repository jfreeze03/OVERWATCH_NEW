"""V090 locks: retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot.

V015's Dynamic Table was a low-risk MERGE-vs-DT cost experiment that nothing ever read;
it kept auto-refreshing every ~6h on WH_ALFA_ADMIN for no consumer (2026-08-17 audit).
V090 drops it — a pure, guarded, idempotent retirement that creates nothing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V090 = (_MIG / "V090__drop_spend_rollup_dt_pilot.sql").read_text(encoding="utf-8")


def test_v090_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v090.py")],
        env={**os.environ, "V090_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V090


def test_v090_is_a_guarded_idempotent_drop():
    assert "EXCEPTION (-20090" in _V090 and "IF (v < 89) THEN" in _V090
    assert "SELECT 90 AS VERSION" in _V090 and "WHERE VERSION = 90)" in _V090
    assert _V090.count("DROP DYNAMIC TABLE IF EXISTS "
                       "DBA_MAINT_DB.OVERWATCH.MART_SPEND_ROLLUP_DT") == 1


def test_v090_creates_and_provisions_nothing():
    # A retirement migration must not create objects or touch compute/monitors.
    assert "CREATE " not in _V090 and "ALTER " not in _V090
    assert "CREATE TASK" not in _V090 and "CREATE WAREHOUSE" not in _V090
    assert "RESOURCE MONITOR" not in _V090
    # Snowflake supports only plain $$ dollar-quoting (owner hit a tagged quote on V089).
    assert "$_v090_$" not in _V090


def test_v090_target_is_the_orphaned_dt_only():
    # It drops the pilot DT and NOTHING else — no fact/mart/proc the app reads.
    drops = [ln for ln in _V090.splitlines() if ln.strip().upper().startswith("DROP")]
    assert len(drops) == 1
    assert "MART_SPEND_ROLLUP_DT" in drops[0]


def test_v090_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V090):
        sqlglot.parse(statement, dialect="snowflake")
