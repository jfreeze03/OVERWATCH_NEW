"""Locks for V150 — COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline (Phase 2).

SP_SCAN_CLOUD_SVC_ANOMALY books an alert per (warehouse, day) whose gross CS credits from
MART_CLOUD_SVC_DAILY are a robust-z outlier vs the warehouse's own 28d baseline — the per-entity
replacement for the fixed 10/20% CS-ratio threshold, so a chronically compile-heavy warehouse stays
in-baseline while a step-change fires. Rides TASK_ANOMALY_SWEEP via a CALL arm; SP_ANOMALY_SWEEP is
re-derived from V133 (its current definition). Byte-locked to outputs/gen_v150.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V150__cloud_svc_anomaly_baseline.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v150_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v150.py")],
        env={**os.environ, "V150_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _MIG, (
        "V150 drifted from its forward-generation — edit outputs/gen_v150.py, not the .sql."
    )


def test_v150_guarded_and_ordered():
    assert "EXCEPTION (-20150" in _MIG and "IF (v < 149) THEN" in _MIG
    assert "SELECT 150 AS VERSION" in _MIG and "WHERE VERSION = 150)" in _MIG


def test_v150_creates_scan_proc_over_the_mart_with_robust_z():
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY" in _MIG
    # the CS baseline sources the mart, NOT a live ACCOUNT_USAGE scan
    assert "FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY" in _MIG
    scan = _MIG.split("SP_SCAN_CLOUD_SVC_ANOMALY", 2)[2].split("$$;", 1)[0]
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in scan
    # same robust median/MAD modified-z engine as COST_ANOMALY_SWEEP + app anomaly.robust_zscores
    assert "0.6745" in scan and "0.7979" in scan
    assert "MEDIAN(ABS(s.CREDITS - m.MED))" in scan          # MAD
    assert "'CLOUD SVC ' || COALESCE(WAREHOUSE_NAME, 'NONE')" in scan  # per-warehouse series
    assert "l.ACTIVE_DAYS >= 10" in scan                     # needs a real baseline


def test_v150_gates_on_cs_volume_not_the_dollar_floor():
    scan = _MIG.split("SP_SCAN_CLOUD_SVC_ANOMALY", 2)[2].split("$$;", 1)[0]
    assert "cs_floor FLOAT DEFAULT 1.0" in scan
    assert "l.CREDITS >= :cs_floor" in scan and "l.MED >= :cs_floor" in scan
    # the CS scan must NOT reuse the $50 compute floor (cloud-services credits are tiny)
    assert "credit_price" not in scan and ">= 50" not in scan


def test_v150_seeds_the_rule_without_clobbering_operator_edits():
    assert "('COST_CLOUD_SVC_ANOMALY', 'COST'" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in _MIG
    assert "WHEN MATCHED THEN UPDATE" not in _MIG


def test_v150_rides_the_sweep_with_no_new_task():
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()" in _MIG
    assert "CREATE TASK" not in _MIG and "ALTER TASK" not in _MIG
    # re-derived sweep: the new scan proc + the sweep = 2 procs; nothing dropped from the sweep
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "RETURN 'anomaly sweep v3 complete'" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()" in _MIG   # V133 arm survives
    assert "-- DQ_BREACH (R24)" in _MIG and "PIPE_VOLUME_DROP" in _MIG
    assert "'COST_CLOUD_SVC_ANOMALY check skipped'" in _MIG               # arm is exception-guarded


def test_validate_docs_and_teardown_track_v150():
    val = _read("snowflake/validate.sql")
    assert "V001..V155 applied" in val and "VERSION BETWEEN 1 AND 155) = 155" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V150__cloud_svc_anomaly_baseline.sql" in _read(rel)
    assert "SP_SCAN_CLOUD_SVC_ANOMALY" in _read("snowflake/teardown.sql")


def test_v150_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 150 in _EXPECTED_MIGRATIONS


def test_v150_rule_has_a_playbook():
    from app.logic.playbooks import playbook_for
    pb = playbook_for("COST_CLOUD_SVC_ANOMALY")
    assert "cloud-services" in pb.lower() and "baseline" in pb.lower()


def test_v150_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
