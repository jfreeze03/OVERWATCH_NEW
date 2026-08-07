"""V076 locks: the server SP_ANOMALY_SWEEP mirrors the app-side anomaly gate."""

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
_V076 = (_MIG / "V076__anomaly_materiality_gate.sql").read_text(encoding="utf-8")


def _proc(text: str) -> str:
    return re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_ANOMALY_SWEEP\(.*?\n\$\$;\n",
        text,
        re.S,
    ).group(0)


def test_v076_regenerates_byte_identical(tmp_path):
    # Derivation law: re-running the generator over V023 reproduces V076 exactly.
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v076.py")],
        env={**os.environ, "V076_OUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V076


def test_v076_is_one_guarded_proc_swap():
    assert "EXCEPTION (-20076" in _V076 and "IF (v < 75) THEN" in _V076
    assert "SELECT 76 AS VERSION" in _V076 and "WHERE VERSION = 76)" in _V076
    assert _V076.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V076 and "CREATE TASK" not in _V076
    # Re-derives the proc only: it must not CALL it (the alert task runs it on
    # schedule) and must not provision compute.
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" not in _V076
    assert "CREATE WAREHOUSE" not in _V076 and "RESOURCE_MONITOR" not in _V076


def test_v076_gate_mirrors_the_app_side_flag_anomalies():
    proc = _proc(_V076)
    # $-denominated floor via the configured credit price (app ANOMALY_MIN_USD=50).
    assert "CREDIT_PRICE_USD" in proc and ":credit_price" in proc
    # A spike gates on the day's own dollars; a collapse on the baseline (median).
    assert "(l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)" in proc
    assert "(l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)" in proc
    # Minimum active days for a real baseline (app ANOMALY_MIN_ACTIVE_DAYS=10).
    assert "l.ACTIVE_DAYS >= 10" in proc
    assert "COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS" in proc
    # The old trivial 1-credit floor is gone.
    assert "l.CREDITS >= 1" not in proc
    # Signed z distinguishes a collapse from a spike in the alert title.
    assert "IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ')" in proc


def test_v076_leaves_every_other_sweep_arm_untouched():
    # Only the COST_ANOMALY_SWEEP arm changes; each other rule arm survives in
    # both the base (V023) and the derived proc.
    previous = _proc((_MIG / "V023__prod_scoped_volume.sql").read_text(encoding="utf-8"))
    current = _proc(_V076)
    for untouched in (
        "PERF_FINGERPRINT_DRIFT",
        "COST_ORG_ACCOUNT_CREEP",
        "PIPE_VOLUME_DROP",
        "SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt)",
    ):
        assert untouched in previous and untouched in current


def test_v076_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements

    for statement in _plain_statements(_V076):
        sqlglot.parse(statement, dialect="snowflake")
